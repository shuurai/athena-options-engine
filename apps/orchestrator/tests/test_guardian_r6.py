"""R6 outbound dead-man watchdog (booklet 14, sections 22-23; D95-D101).

Covers the D99 parity contract (stdlib copy of ``is_us_session`` vs the
orchestrator's canonical pure function), the pure decision core (10-minute
grace, US-session gate, no out-of-hours accumulation, down/recovered de-dup),
thin IO (check-in URL, var/ state, env-only one-shot Telegram send), and the
deploy artifacts (plist validity, placeholder-only paths, no committed token,
stdlib-only imports, zero migration / zero runtime-core edits).
"""

from __future__ import annotations

import ast
import importlib.util
import io
import json
import plistlib
import sys
import unittest
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from athena_orchestrator import guardian_schedule as gs

UTC = timezone.utc
ET = ZoneInfo("America/New_York")
HKT = ZoneInfo("Asia/Hong_Kong")
ROOT = Path(__file__).resolve().parents[3]
MACOS = ROOT / "deploy" / "macos"
WATCHDOG_PATH = MACOS / "athena_watchdog.py"

_spec = importlib.util.spec_from_file_location("athena_watchdog_r6", WATCHDOG_PATH)
wd = importlib.util.module_from_spec(_spec)
# dataclasses resolves annotations via sys.modules[cls.__module__]; register
# before exec_module so the dynamically loaded module is importable.
sys.modules[_spec.name] = wd
_spec.loader.exec_module(wd)


def _utc(y, m, d, hh=0, mm=0, ss=0):
    return datetime(y, m, d, hh, mm, ss, tzinfo=UTC)


def _load_plist(name):
    with (MACOS / name).open("rb") as handle:
        return plistlib.load(handle)


# ---------------------------------------------------------------------------
# D99: parity with guardian_schedule.is_us_session
# ---------------------------------------------------------------------------
class TestSessionParity(unittest.TestCase):
    def test_constants_match_the_canonical_module(self):
        self.assertEqual(str(wd.ET), str(gs.ET))
        self.assertEqual(wd.US_OPEN, gs.US_OPEN)
        self.assertEqual(wd.US_CLOSE, gs.US_CLOSE)
        self.assertEqual(wd.GRACE, timedelta(minutes=10))
        self.assertEqual(wd.CHECKIN_INTERVAL, 300)

    def test_parity_over_dst_weekend_and_boundary_matrix(self):
        # Deliberately wide: weekdays/weekends, both DST sides, session edges,
        # US holiday treated as an ordinary weekday (section 14.5 honest gap),
        # and the same instant expressed in UTC / ET / HKT.
        days = [
            date(2026, 1, 1),    # holiday Thursday -> ordinary weekday
            date(2026, 3, 6),    # Fri before spring forward
            date(2026, 3, 8),    # Sun, DST spring forward
            date(2026, 3, 9),    # Mon right after spring forward
            date(2026, 7, 1),    # Wed, EDT
            date(2026, 11, 1),   # Sun, DST fall back
            date(2026, 11, 2),   # Mon right after fall back
            date(2026, 11, 22),  # Sun
            date(2026, 11, 23),  # Mon
            date(2026, 11, 27),  # Fri, Thanksgiving -> ordinary weekday
            date(2026, 12, 25),  # Fri, Christmas -> ordinary weekday
        ]
        clocks = [
            time(0, 0), time(9, 29, 59), time(9, 30, 0), time(9, 30, 1),
            time(12, 30), time(15, 59, 59), time(16, 0, 0), time(16, 0, 1),
            time(23, 59),
        ]
        checked = 0
        for day in days:
            for clock in clocks:
                et_moment = datetime.combine(day, clock, tzinfo=ET)
                for moment in (et_moment, et_moment.astimezone(UTC),
                               et_moment.astimezone(HKT)):
                    with self.subTest(day=day, clock=clock, tz=moment.tzinfo):
                        self.assertEqual(
                            wd.is_us_session(moment), gs.is_us_session(moment))
                    checked += 1
        self.assertGreaterEqual(checked, 250)

    def test_boundaries_are_exact(self):
        tuesday = date(2026, 11, 24)
        matrix = {
            time(9, 29, 59): False,
            time(9, 30, 0): True,
            time(12, 30): True,
            time(15, 59, 59): True,
            time(16, 0, 0): False,
        }
        for clock, expected in matrix.items():
            moment = datetime.combine(tuesday, clock, tzinfo=ET)
            self.assertEqual(wd.is_us_session(moment), expected)
            self.assertEqual(gs.is_us_session(moment), expected)

    def test_weekend_never_a_session(self):
        for day in (date(2026, 11, 22), date(2026, 11, 21)):  # Sun, Sat
            moment = datetime.combine(day, time(12, 0), tzinfo=ET)
            self.assertFalse(wd.is_us_session(moment))
            self.assertFalse(gs.is_us_session(moment))

    def test_naive_datetime_rejected(self):
        naive = datetime(2026, 11, 24, 14, 35)
        with self.assertRaises(ValueError):
            wd.is_us_session(naive)
        with self.assertRaises(ValueError):
            gs.is_us_session(naive)

    def test_session_open_is_dst_safe(self):
        # 09:30 ET = 14:30 UTC under EDT, 14:30 UTC under EST is 09:30 too,
        # but 13:30 UTC in winter / 13:30 UTC in summer differ. Assert both
        # UTC offsets of the wall clock rather than a hard-coded switch date.
        summer = wd._session_open_utc(_utc(2026, 7, 1, 13, 0))
        winter = wd._session_open_utc(_utc(2026, 11, 24, 14, 0))
        self.assertEqual(summer.astimezone(ET).time(), time(9, 30))
        self.assertEqual(winter.astimezone(ET).time(), time(9, 30))
        self.assertEqual(summer.hour, 13)   # EDT = UTC-4
        self.assertEqual(winter.hour, 14)   # EST = UTC-5


# ---------------------------------------------------------------------------
# Pure decision core (D65 / D97)
# ---------------------------------------------------------------------------
class TestDecide(unittest.TestCase):
    # 2026-11-24 Tue, 15:35 UTC = 10:35 ET, in session (open is 14:30 UTC).
    # Late enough that a 10-min in-session gap is representable.
    NOW = _utc(2026, 11, 24, 15, 35)

    def test_first_run_is_baseline_only(self):
        plan = wd.decide(wd.WatchState(), self.NOW, True)
        self.assertEqual(plan.messages, ())
        self.assertEqual(plan.reason, "baseline")
        self.assertEqual(plan.state.last_checkin, wd._iso(self.NOW))

    def test_naive_now_rejected(self):
        with self.assertRaises(ValueError):
            wd.decide(wd.WatchState(), datetime(2026, 11, 24, 14, 35), True)

    def test_outside_us_session_no_alert_and_no_accumulation(self):
        state = wd.WatchState(last_checkin=wd._iso(_utc(2026, 11, 20, 20, 0)),
                              down_sent=False)
        for moment in (_utc(2026, 11, 21, 15, 0),   # Saturday
                       _utc(2026, 11, 22, 15, 0),   # Sunday
                       _utc(2026, 11, 23, 3, 0)):   # Mon 03:00 UTC = Sun 22:00 ET
            plan = wd.decide(state, moment, True)
            self.assertEqual(plan.messages, ())
            self.assertEqual(plan.reason, "outside_us_session_no_accumulation")
            self.assertFalse(plan.state.down_sent)
            state = plan.state
        # Monday 09:31 ET must NOT inherit the weekend gap.
        monday = _utc(2026, 11, 23, 14, 31)
        plan = wd.decide(state, monday, True)
        self.assertEqual(plan.messages, ())
        self.assertEqual(plan.reason, "healthy")

    def test_within_grace_is_healthy(self):
        last = wd._iso(self.NOW - timedelta(minutes=9))
        plan = wd.decide(wd.WatchState(last_checkin=last), self.NOW, True)
        self.assertEqual(plan.messages, ())
        self.assertEqual(plan.reason, "healthy")

    def test_silence_ge_ten_minutes_emits_exactly_one_down(self):
        last = wd._iso(self.NOW - timedelta(minutes=12))
        plan = wd.decide(wd.WatchState(last_checkin=last), self.NOW, True)
        self.assertEqual(len(plan.messages), 1)
        self.assertIn(wd.DOWN_TAG, plan.messages[0])
        self.assertIn("12 min", plan.messages[0])  # clamped to session open
        self.assertTrue(plan.state.down_sent)
        self.assertFalse(plan.state.recovered_sent)
        self.assertEqual(plan.reason, "silence_down")

    def test_down_is_deduped_across_restarts(self):
        # Box is back but the external check-in service is still unreachable:
        # one down only, never a repeat, even as the gap keeps growing.
        last = wd._iso(self.NOW - timedelta(minutes=20))
        state = wd.WatchState(last_checkin=last, down_sent=True)
        for shift in (0, 5, 10, 25):
            plan = wd.decide(state, self.NOW + timedelta(minutes=shift), False)
            self.assertEqual(plan.messages, ())
            self.assertEqual(plan.reason, "silence_dedup")
            self.assertTrue(plan.state.down_sent)
            state = plan.state

    def test_recovered_emitted_once_then_rearmed(self):
        last = wd._iso(self.NOW - timedelta(minutes=5))
        state = wd.WatchState(last_checkin=last, down_sent=True)
        first = wd.decide(state, self.NOW, True)
        self.assertEqual(len(first.messages), 1)
        self.assertIn(wd.RECOVERED_TAG, first.messages[0])
        self.assertEqual(first.reason, "recovered")
        self.assertFalse(first.state.down_sent)
        second = wd.decide(first.state, self.NOW + timedelta(minutes=5), True)
        self.assertEqual(second.messages, ())
        self.assertEqual(second.reason, "healthy")

    def test_no_recovered_while_checkin_still_failing(self):
        last = wd._iso(self.NOW - timedelta(minutes=5))
        state = wd.WatchState(last_checkin=last, down_sent=True)
        plan = wd.decide(state, self.NOW, False)
        self.assertEqual(plan.messages, ())
        self.assertTrue(plan.state.down_sent)
        self.assertEqual(plan.state.last_checkin, last)

    def test_full_cycle_down_dedup_recovered(self):
        # Simulate: box dies during US session, restarts, recovers.
        last = wd._iso(self.NOW - timedelta(minutes=30))
        state = wd.WatchState(last_checkin=last)
        down = wd.decide(state, self.NOW, False)
        self.assertEqual(len(down.messages), 1)
        self.assertIn(wd.DOWN_TAG, down.messages[0])
        dedup = wd.decide(down.state, self.NOW + timedelta(minutes=5), False)
        self.assertEqual(dedup.messages, ())
        resumed = wd.decide(dedup.state, self.NOW + timedelta(minutes=10), True)
        self.assertEqual(len(resumed.messages), 1)
        self.assertIn(wd.RECOVERED_TAG, resumed.messages[0])

    def test_messages_never_carry_ack_controls(self):
        text = wd.down_text(self.NOW, 900) + wd.recovered_text(self.NOW)
        for forbidden in ("Resolve", "guardres", "reply_markup", "/guard ack",
                          "nag"):
            self.assertNotIn(forbidden, text)

    def test_weekend_gap_does_not_fire_monday_down(self):
        # Friday 15:00 ET last check-in, Monday 09:31 ET first tick.
        last = wd._iso(_utc(2026, 11, 20, 20, 0))
        state = wd.WatchState(last_checkin=last)
        plan = wd.decide(state, _utc(2026, 11, 23, 14, 31), True)
        self.assertEqual(plan.messages, ())


# ---------------------------------------------------------------------------
# Thin IO
# ---------------------------------------------------------------------------
class _FakeResponse(io.BytesIO):
    def __init__(self, payload, status=200):
        super().__init__(payload)
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


class TestStateIO(unittest.TestCase):
    def test_round_trip(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "nested" / "state.json"
            state = wd.WatchState(last_checkin="2026-11-24T14:35:00+00:00",
                                  down_sent=True, recovered_sent=False)
            wd.save_state(target, state)
            self.assertTrue(target.is_file())
            self.assertEqual(wd.load_state(target), state)
            self.assertFalse((target.parent / (target.name + ".tmp")).exists())

    def test_missing_and_corrupt_state_degrade_to_baseline(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(wd.load_state(Path(folder) / "nope.json"),
                             wd.WatchState())
            bad = Path(folder) / "bad.json"
            bad.write_text("{not json", encoding="utf-8")
            self.assertEqual(wd.load_state(bad), wd.WatchState())
            bad.write_text('["a", "list"]', encoding="utf-8")
            self.assertEqual(wd.load_state(bad), wd.WatchState())
            bad.write_text('{"last_checkin": "naive-no-offset", "down_sent": 1}',
                           encoding="utf-8")
            loaded = wd.load_state(bad)
            self.assertIsNone(loaded.last_checkin)
            self.assertTrue(loaded.down_sent)


class TestCheckIn(unittest.TestCase):
    def test_empty_url_is_noop_without_network(self):
        def boom(*_a, **_kw):
            raise AssertionError("must not touch the network")
        self.assertFalse(wd.check_in("", opener=boom))
        self.assertFalse(wd.check_in("   ", opener=boom))

    def test_outbound_get_and_status(self):
        seen = {}

        def fake(request, timeout=0):
            seen["url"] = request.full_url
            seen["timeout"] = timeout
            seen["method"] = request.get_method()
            return _FakeResponse(b"ok")
        self.assertTrue(wd.check_in("https://hc-ping.example/uuid", opener=fake))
        self.assertEqual(seen["url"], "https://hc-ping.example/uuid")
        self.assertEqual(seen["method"], "GET")
        self.assertEqual(seen["timeout"], wd.CHECKIN_TIMEOUT)

    def test_failure_is_a_silence_signal_not_an_exception(self):
        def fail(*_a, **_kw):
            raise OSError("offline")
        self.assertFalse(wd.check_in("https://hc-ping.example/uuid",
                                     opener=fail))
        def not_ok(*_a, **_kw):
            return _FakeResponse(b"down", status=503)
        self.assertFalse(wd.check_in("https://hc-ping.example/uuid",
                                     opener=not_ok))


class TestSendOnce(unittest.TestCase):
    def test_no_token_or_no_allowlist_is_noop(self):
        def boom(*_a, **_kw):
            raise AssertionError("must not touch the network")
        self.assertEqual(wd.send_once("x", env={"TELEGRAM_BOT_TOKEN": ""},
                                      opener=boom), 0)
        # Token present but no allowlist: the loop is empty, still a no-op.
        self.assertEqual(wd.send_once("x", env={
            "TELEGRAM_BOT_TOKEN": "abc",
            "TELEGRAM_ALLOWLIST_USER_IDS": "  ,  ",
        }, opener=boom), 0)
        # Empty token WITH a non-empty allowlist: the guard is what prevents a
        # credential-less sendMessage. This is the load-bearing case. Record
        # calls instead of raising -- send_once deliberately swallows per-chat
        # exceptions, so a raising opener would be caught and hide the bug.
        recorder = []

        def recording(request, timeout=0):
            recorder.append(request.full_url)
            return _FakeResponse(b'{"ok": true}')
        self.assertEqual(wd.send_once("x", env={
            "TELEGRAM_BOT_TOKEN": "",
            "TELEGRAM_ALLOWLIST_USER_IDS": "111,222",
        }, opener=recording), 0)
        self.assertEqual(recorder, [])

    def test_one_push_per_allowlisted_chat_no_markup(self):
        calls = []

        def fake(request, timeout=0):
            calls.append((request.full_url,
                          json.loads(request.data.decode("utf-8")), timeout))
            return _FakeResponse(b'{"ok": true}')
        env = {"TELEGRAM_BOT_TOKEN": "secret-token",
               "TELEGRAM_ALLOWLIST_USER_IDS": "111, 222,111"}
        self.assertEqual(wd.send_once("hello", env=env, opener=fake), 2)
        self.assertEqual(len(calls), 2)
        self.assertEqual({c[1]["chat_id"] for c in calls}, {"111", "222"})
        for url, payload, timeout in calls:
            self.assertEqual(url,
                             "https://api.telegram.org/botsecret-token/sendMessage")
            self.assertEqual(payload["text"], "hello")
            self.assertNotIn("reply_markup", payload)
            self.assertEqual(timeout, wd.TELEGRAM_TIMEOUT)

    def test_proxy_is_honoured(self):
        captured = {}

        class _Opener:
            def open(self, request, timeout=0):
                captured["url"] = request.full_url
                return _FakeResponse(b'{"ok": true}')

        def fake_build_opener(handler):
            captured["proxy"] = handler.proxies
            return _Opener()

        env = {"TELEGRAM_BOT_TOKEN": "t",
               "TELEGRAM_ALLOWLIST_USER_IDS": "1",
               "TELEGRAM_HTTPS_PROXY": "http://proxy.local:8080"}
        with patch.object(wd, "build_opener", fake_build_opener):
            self.assertEqual(wd.send_once("x", env=env), 1)
        self.assertEqual(captured["proxy"]["https"], "http://proxy.local:8080")

    def test_send_failure_does_not_raise_and_counts_only_successes(self):
        state = {"n": 0}

        def flaky(request, timeout=0):
            state["n"] += 1
            if state["n"] == 1:
                raise OSError("network down")
            return _FakeResponse(b'{"ok": true}')
        env = {"TELEGRAM_BOT_TOKEN": "t",
               "TELEGRAM_ALLOWLIST_USER_IDS": "1,2"}
        self.assertEqual(wd.send_once("x", env=env, opener=flaky), 1)


class TestEntrypoint(unittest.TestCase):
    def test_dry_run_does_not_write_or_send(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "state.json"
            out = io.StringIO()
            with patch.object(wd, "check_in", side_effect=AssertionError("net")), \
                 patch.object(wd, "send_once", side_effect=AssertionError("send")), \
                 patch("sys.stdout", out):
                code = wd.main(["--dry-run", "--state", str(target),
                                "--env-file", str(Path(folder) / "no.env"),
                                "--now", "2026-11-24T14:35:00+00:00"])
            self.assertEqual(code, 0)
            self.assertFalse(target.exists())
            self.assertIn('"reason": "baseline"', out.getvalue())

    def test_local_only_mode_still_evaluates(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "state.json"
            target.write_text(json.dumps({
                "last_checkin": "2026-11-24T14:20:00+00:00",
                "down_sent": False, "recovered_sent": False}), encoding="utf-8")
            out = io.StringIO()
            with patch.object(wd, "check_in", side_effect=AssertionError("net")), \
                 patch.object(wd, "send_once", return_value=1), \
                 patch("sys.stdout", out):
                code = wd.main(["--state", str(target),
                                "--env-file", str(Path(folder) / "no.env"),
                                "--now", "2026-11-24T14:45:00+00:00"])
            self.assertEqual(code, 0)
            self.assertIn('"reason": "silence_down"', out.getvalue())
            self.assertTrue(wd.load_state(target).down_sent)


# ---------------------------------------------------------------------------
# Deploy artifacts & boundaries (D98/D99/D100/D101)
# ---------------------------------------------------------------------------
class TestDeployArtifacts(unittest.TestCase):
    def test_all_plists_are_valid_xml_with_expected_shape(self):
        expected = {
            "com.zeptun.athena.watchdog.plist": ("StartInterval", 300),
            "com.zeptun.athena.orchestrator.plist": ("KeepAlive", True),
            "com.zeptun.athena.postgres.plist": ("RunAtLoad", True),
        }
        for name, (key, value) in expected.items():
            data = _load_plist(name)
            self.assertEqual(data["Label"], name[:-6])
            self.assertEqual(data.get(key), value)
            self.assertIn("ProgramArguments", data)
        cloudflared = _load_plist("com.zeptun.athena.cloudflared.plist")
        self.assertFalse(cloudflared["RunAtLoad"])
        self.assertFalse(cloudflared["KeepAlive"])
        self.assertTrue(cloudflared["Disabled"])

    def test_watchdog_plist_is_independent_300s_outbound_job(self):
        data = _load_plist("com.zeptun.athena.watchdog.plist")
        self.assertEqual(data["StartInterval"], 300)
        self.assertTrue(data["RunAtLoad"])
        self.assertNotIn("KeepAlive", data)
        args = data["ProgramArguments"]
        self.assertEqual(args[0], "/usr/bin/python3")
        self.assertTrue(args[1].endswith("deploy/macos/athena_watchdog.py"))

    def test_no_double_hyphen_inside_xml_comments(self):
        import re
        for path in sorted(MACOS.glob("*.plist")):
            for comment in re.findall(r"<!--(.*?)-->", path.read_text(), re.S):
                self.assertNotIn("--", comment)

    def test_paths_are_placeholders_not_machine_specific(self):
        for path in sorted(MACOS.glob("*.plist")):
            text = path.read_text(encoding="utf-8")
            self.assertIn("__ATHENA_ROOT__", text)
            self.assertNotIn("/Users/", text)
            self.assertNotIn("/home/", text)

    def test_no_credential_is_committed(self):
        for path in sorted(MACOS.iterdir()):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
            for banned in ("bot123456:", "TELEGRAM_BOT_TOKEN=abc",
                           "TELEGRAM_BOT_TOKEN=<real", "hc-ping.com/",
                           "BEGIN PRIVATE KEY"):
                self.assertNotIn(banned, text)
        # the placeholder token in README is explicitly empty / angle-bracketed
        readme = (MACOS / "README.md").read_text(encoding="utf-8")
        self.assertIn("ATHENA_WATCHDOG_CHECKIN_URL=", readme)
        self.assertIn("env-only", readme)

    def test_watchdog_script_is_stdlib_only(self):
        tree = ast.parse(WATCHDOG_PATH.read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                imported.add((node.module or "").split(".")[0])
        self.assertNotIn("athena_orchestrator", imported)
        self.assertNotIn("athena_bots", imported)
        self.assertNotIn("athena_store", imported)
        for name in imported:
            self.assertIn(name, {
                "__future__", "argparse", "json", "os", "sys", "dataclasses",
                "datetime", "pathlib", "urllib", "zoneinfo",
            }, f"non-stdlib or unexpected import: {name}")

    def test_watchdog_never_touches_db_guardian_alerts_or_tunnel(self):
        # Check EXECUTABLE code (AST), not the prose docstring that legitimately
        # documents what the watchdog deliberately does NOT do.
        tree = ast.parse(WATCHDOG_PATH.read_text(encoding="utf-8"))
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)):
                if (node.body and isinstance(node.body[0], ast.Expr)
                        and isinstance(node.body[0].value, ast.Constant)
                        and isinstance(node.body[0].value.value, str)):
                    docstrings.add(id(node.body[0].value))
        code_tokens = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                    and id(node) not in docstrings:
                code_tokens.append(node.value)
            elif isinstance(node, ast.Name):
                code_tokens.append(node.id)
            elif isinstance(node, ast.Attribute):
                code_tokens.append(node.attr)
        code = "\n".join(code_tokens)
        for banned in ("guardian_alerts", "cycle_progress", "tunnel.py",
                       "start_tunnel", "reqMktData", "INSERT INTO",
                       "UPDATE ", "CREATE TABLE", "ib_insync", "connect"):
            self.assertNotIn(banned, code, f"executable reference: {banned}")

    def test_no_migration_and_no_new_package_under_deploy(self):
        self.assertEqual(sorted(p.suffix for p in MACOS.iterdir() if p.is_file()),
                         sorted([".py", ".md", ".plist", ".plist", ".plist", ".plist"]))
        self.assertFalse(list(MACOS.glob("*.sql")))
        self.assertFalse(list(MACOS.glob("pyproject.toml")))
        self.assertFalse(list(MACOS.glob("requirements*.txt")))
        # R6 adds no migration; 055 is R7's regime_labels (D69 / D104).
        migrations = ROOT / "packages" / "store" / "src" / "athena_store" / "migrations"
        self.assertEqual(len(list(migrations.glob("0*.sql"))), 51)
        self.assertTrue((migrations / "055_regime_labels.sql").exists())

    def test_readme_documents_every_decision_and_the_2fa_gap(self):
        text = (MACOS / "README.md").read_text(encoding="utf-8")
        for token in ("D95", "D96", "D97", "D98", "D99", "D100", "D101",
                      "IB_CLIENT_ID", "2FA", "KeepAlive", "StartInterval",
                      "is_us_session", "var/", "10-minute grace",
                      "outbound", "no second"):
            self.assertIn(token, text)

    def test_runtime_core_is_untouched_by_r6(self):
        # R6 adds no runtime-core edits. Assert the canonical seams still have
        # exactly the shape R4/R5 left them in.
        nag = (ROOT / "apps" / "orchestrator" / "src" /
               "athena_orchestrator" / "guardian_nag.py").read_text()
        self.assertIn("UPDATE cycle_progress SET updated_at = now() WHERE id = 1",
                      nag)
        schedule = (ROOT / "apps" / "orchestrator" / "src" /
                    "athena_orchestrator" / "guardian_schedule.py").read_text()
        self.assertIn("def is_us_session(now: datetime) -> bool:", schedule)
        clients = (ROOT / "apps" / "exec-ib" / "src" / "athena_exec" /
                   "clients.py").read_text()
        self.assertIn('"telegram": 20', clients)


if __name__ == "__main__":
    unittest.main()
