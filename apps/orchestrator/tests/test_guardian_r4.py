"""R4 Guardian nag + dead-man (booklet 14, sections 17-18; D80-D83).

Covers: pure core (D61/D62/D83 filters, duplicate monitor), is_us_session DST,
dead-man findings + grace (D65/D80), state-layer uniqueness, nag_tick SQL
shape (heartbeat never touches payload), the bots callback registry (D81),
failed-run markers (section 14.3), loop wiring (D82 fixed 300s), and the
zero-migration boundary (055 reserved for R7).
"""

from __future__ import annotations

import unittest
from datetime import UTC, date, datetime, time, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from athena_bots import telegram as tg
from athena_orchestrator import guardian_nag as gn
from athena_orchestrator import guardian_runner as gr
from athena_orchestrator import guardian_schedule as gs
from athena_orchestrator import guardian_view as gv

UTC = timezone.utc
ROOT = Path(__file__).resolve().parents[3]
DAY = date(2026, 7, 1)  # EDT, a Wednesday


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _Cursor:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class _FakeConn:
    """Routes by SQL fragment so tests assert the exact statement shape."""

    def __init__(self, *, runs=None, due=None, live=None, lookup=None):
        self.calls: list[tuple[str, tuple]] = []
        self.runs = runs or []
        self.due = due or []
        self.live = live or []
        self.lookup = lookup or []
        self.commits = 0

    def execute(self, sql, params=None):
        flat = " ".join(sql.split())
        self.calls.append((flat, params))
        if "UPDATE cycle_progress" in flat:
            return _Cursor([])
        if "FROM guardian_runs" in flat:
            return _Cursor(self.runs)
        if "WHERE state = 'open'" in flat:
            return _Cursor(self.due)
        if "WHERE state IN ('open', 'skipped')" in flat:
            return _Cursor(self.live)
        if "SELECT account_id FROM guardian_alerts WHERE id" in flat:
            return _Cursor(self.lookup)
        return _Cursor([])

    def commit(self):
        self.commits += 1


class _FakeClient:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def call(self, method, payload=None, timeout=35):
        self.calls.append((method, payload or {}))
        return {"ok": True}


def _slot(run="A", day=DAY):
    return gs.RunSlot(run=run, trade_day=day, moment=gs.run_moments(day)[run])


def _alert(alert_id=1, symbol="NVDA", breach="uncovered_call",
           tier="danger", state="open", account="U1"):
    return {
        "id": alert_id, "account_id": account, "symbol": symbol,
        "breach": breach, "tier": tier, "state": state,
    }


def _due_row(a):
    """DB row shape for guardian_alerts (id, account, symbol, breach, tier, state)."""
    return (a["id"], a["account_id"], a["symbol"], a["breach"], a["tier"], a["state"])


def _moment(hhmm, day=DAY):
    """UTC instant for an EDT wall clock on ``day`` (EDT = UTC-4)."""
    h, m = map(int, hhmm.split(":"))
    return datetime(day.year, day.month, day.day, h, m, tzinfo=UTC) + timedelta(hours=4)


# ---------------------------------------------------------------------------
# Pure core: D61 / D62 / D83
# ---------------------------------------------------------------------------


class TestDueAlerts(unittest.TestCase):
    def test_only_open_red_and_black_are_due(self):
        rows = [
            _alert(1, tier="danger", state="open"),
            _alert(2, tier="blowup", state="open"),
            _alert(3, tier="balanced", state="open"),
            _alert(4, tier="healthy", state="open"),
            _alert(5, tier="profit", state="open"),
        ]
        self.assertEqual([r.id for r in gn.due_alerts(rows)], [1, 2])

    def test_d62_states_skipped_resolved_auto_cleared_never_nag(self):
        rows = [_alert(1, state=s) for s in ("skipped", "resolved", "auto_cleared")]
        self.assertEqual(gn.due_alerts(rows), [])

    def test_empty_input(self):
        self.assertEqual(gn.due_alerts([]), [])

    def test_account_scope_is_a_portfolio_state_not_a_ticker(self):
        row = _alert(7, symbol=gv.ACCOUNT_SCOPE, breach="el_thin")
        self.assertIn("PORTFOLIO", gn.nag_text(gn.due_alerts([row])[0]))

    def test_nag_text_is_deterministic_and_names_the_breach(self):
        text = gn.nag_text(gn.due_alerts([_alert(9, breach="advisor_close_pending")])[0])
        self.assertIn("advisor_close_pending", text)
        self.assertIn("U1", text)
        self.assertEqual(text, gn.nag_text(gn.due_alerts([_alert(9, breach="advisor_close_pending")])[0]))

    def test_per_alert_text_is_not_a_blanket_summary(self):
        a = gn.nag_text(gn.due_alerts([_alert(1, symbol="NVDA")])[0])
        b = gn.nag_text(gn.due_alerts([_alert(2, symbol="MRVL")])[0])
        self.assertNotIn("MRVL", a)
        self.assertNotIn("NVDA", b)


class TestDuplicateMonitor(unittest.TestCase):
    def test_no_duplicates_returns_empty(self):
        self.assertEqual(gn.duplicate_live_keys([_alert(1), _alert(2, symbol="MRVL")]), ())

    def test_duplicate_live_rows_are_detected(self):
        rows = [_alert(1), _alert(2), _alert(3, state="resolved"), _alert(4, state="auto_cleared")]
        self.assertEqual(gn.duplicate_live_keys(rows), (("U1", "NVDA", "uncovered_call", 2),))

    def test_resolved_and_auto_cleared_rows_do_not_count(self):
        rows = [_alert(1, state="resolved"), _alert(2, state="resolved")]
        self.assertEqual(gn.duplicate_live_keys(rows), ())

    def test_duplicates_are_grouped_by_full_state_key(self):
        rows = [_alert(1), _alert(2, symbol="MRVL"), _alert(3, symbol="MRVL")]
        self.assertEqual(gn.duplicate_live_keys(rows), (("U1", "MRVL", "uncovered_call", 2),))

    def test_report_is_empty_when_clean_and_names_key_when_dirty(self):
        self.assertEqual(gn.duplicate_report(()), "")
        self.assertIn("NVDA", gn.duplicate_report((("U1", "NVDA", "x", 2),)))
        self.assertIn("guardian_alerts_live", gn.duplicate_report((("U1", "NVDA", "x", 2),)))


# ---------------------------------------------------------------------------
# D65 / is_us_session (DST-safe, pure)
# ---------------------------------------------------------------------------


class TestIsUsSession(unittest.TestCase):
    def test_edt_10am_is_in_session(self):
        self.assertTrue(gs.is_us_session(_moment("10:00")))

    def test_est_10am_is_in_session(self):
        # 10:00 EST = 15:00 UTC
        self.assertTrue(gs.is_us_session(datetime(2026, 12, 1, 15, 0, tzinfo=UTC)))

    def test_edt_open_and_close_boundaries(self):
        self.assertTrue(gs.is_us_session(_moment("09:30")))
        self.assertFalse(gs.is_us_session(_moment("09:29")))
        self.assertFalse(gs.is_us_session(_moment("16:00")))
        self.assertTrue(gs.is_us_session(_moment("15:59")))

    def test_pre_open_and_after_hours(self):
        self.assertFalse(gs.is_us_session(_moment("08:30")))
        self.assertFalse(gs.is_us_session(_moment("17:30")))

    def test_weekend_is_never_in_session(self):
        saturday = date(2026, 7, 4)
        self.assertFalse(gs.is_us_session(datetime(2026, 7, 4, 14, 0, tzinfo=UTC)))
        self.assertFalse(gs.is_us_weekday(saturday))

    def test_naive_datetime_is_rejected(self):
        with self.assertRaises(ValueError):
            gs.is_us_session(datetime(2026, 7, 1, 14, 0))

    def test_us_open_and_close_constants(self):
        self.assertEqual(gs.US_OPEN, time(9, 30))
        self.assertEqual(gs.US_CLOSE, time(16, 0))


# ---------------------------------------------------------------------------
# D80 dead-man findings
# ---------------------------------------------------------------------------


class TestExpectedSlots(unittest.TestCase):
    def test_today_weekday_has_a_and_b_only(self):
        slots = gn.expected_slots(_moment("10:00"))
        self.assertEqual([s.run for s in slots], ["A", "B"])
        self.assertTrue(all(s.trade_day == DAY for s in slots))

    def test_weekend_has_no_expected_slots(self):
        self.assertEqual(gn.expected_slots(datetime(2026, 7, 4, 14, 0, tzinfo=UTC)), [])

    def test_slots_are_dst_anchored(self):
        winter = date(2026, 12, 1)
        slots = gn.expected_slots(datetime(2026, 12, 1, 15, 0, tzinfo=UTC))
        self.assertEqual(slots[0].moment.strftime("%H:%M"), "13:30")
        self.assertEqual(slots[0].trade_day, winter)


class TestDeadmanFindings(unittest.TestCase):
    def test_a_successful_row_clears_the_finding(self):
        slots = [_slot("A")]
        runs = [{"account_id": "U1", "trade_day": str(DAY), "run": "A",
                 "status": "ok", "detail": ""}]
        self.assertEqual(gn.deadman_findings(["U1"], slots, runs, _moment("10:00")), [])

    def test_a_failed_row_is_reported_without_grace(self):
        slots = [_slot("A")]
        runs = [{"account_id": "U1", "trade_day": str(DAY), "run": "A",
                 "status": "failed", "detail": "RuntimeError"}]
        now = slots[0].moment + timedelta(minutes=1)
        found = gn.deadman_findings(["U1"], slots, runs, now)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["kind"], "failed")
        self.assertEqual(found[0]["detail"], "RuntimeError")

    def test_missing_row_needs_the_10_minute_d65_grace(self):
        slots = [_slot("A")]
        just_after = slots[0].moment + timedelta(minutes=9)
        self.assertEqual(gn.deadman_findings(["U1"], slots, [], just_after), [])
        after_grace = slots[0].moment + timedelta(minutes=11)
        found = gn.deadman_findings(["U1"], slots, [], after_grace)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["kind"], "missed")

    def test_future_slot_is_never_a_finding(self):
        slots = [_slot("B")]  # 09:35 ET
        now = _moment("09:00")
        self.assertEqual(gn.deadman_findings(["U1"], slots, [], now), [])

    def test_each_account_gets_its_own_finding(self):
        slots = [_slot("A")]
        found = gn.deadman_findings(["U1", "U2"], slots, [], _moment("10:00"))
        self.assertEqual([f["account_id"] for f in found], ["U1", "U2"])

    def test_breach_key_is_one_per_slot_not_per_kind(self):
        missed = {"trade_day": DAY, "run": "A", "kind": "missed", "detail": ""}
        failed = {"trade_day": DAY, "run": "A", "kind": "failed", "detail": "RuntimeError"}
        self.assertEqual(gn.deadman_breach(missed), gn.deadman_breach(failed))
        self.assertIn(":A", gn.deadman_breach(missed))
        self.assertNotIn("missed", gn.deadman_breach(missed))

    def test_deadman_text_names_day_run_and_detail(self):
        text = gn.deadman_text({"account_id": "U1", "trade_day": DAY,
                               "run": "A", "kind": "failed", "detail": "RuntimeError"})
        self.assertIn("RuntimeError", text)
        self.assertIn("Run A", text)
        self.assertIn("PORTFOLIO", text)


# ---------------------------------------------------------------------------
# nag_tick: SQL shape + pure decisions together
# ---------------------------------------------------------------------------


class TestNagTick(unittest.TestCase):
    def test_heartbeat_touches_only_updated_at_not_payload(self):
        conn = _FakeConn()
        gn.nag_tick(conn, now=_moment("10:00"), accounts=[], slots=[], session=False)
        sql = conn.calls[0][0]
        self.assertIn("UPDATE cycle_progress SET updated_at = now()", sql)
        self.assertNotIn("payload", sql)
        self.assertNotIn("INSERT INTO cycle_progress", sql)

    def test_due_rows_become_one_nag_each(self):
        conn = _FakeConn(due=[_due_row(_alert(1)), _due_row(_alert(2, symbol="MRVL"))])
        plan = gn.nag_tick(conn, now=_moment("10:00"), accounts=[], slots=[], session=False)
        self.assertEqual([a for a, _ in plan.nags], [1, 2])

    def test_no_deadman_insert_outside_us_session(self):
        conn = _FakeConn()
        gn.nag_tick(conn, now=_moment("08:00"), accounts=["U1"], slots=[_slot("A")], session=True)
        # session=True is passed by the caller; the caller computes it. Guard the
        # contract: nag_tick itself must not re-derive or override `session`.
        conn2 = _FakeConn()
        plan = gn.nag_tick(conn2, now=_moment("08:00"), accounts=["U1"], slots=[_slot("A")], session=False)
        self.assertEqual(plan.deadman, 0)
        self.assertFalse(any("INSERT INTO guardian_alerts" in c[0] for c in conn2.calls))

    def test_deadman_insert_uses_state_layer_not_exists_guard(self):
        conn = _FakeConn()
        gn.nag_tick(conn, now=_moment("10:00"), accounts=["U1"],
                     slots=[_slot("A")], session=True)
        inserts = [c for c in conn.calls if "INSERT INTO guardian_alerts" in c[0]]
        self.assertEqual(len(inserts), 1)
        sql = inserts[0][0]
        self.assertIn("WHERE NOT EXISTS", sql)
        self.assertIn("ON CONFLICT DO NOTHING", sql)
        self.assertIn("'deadman'", sql)
        self.assertIn("'danger'", sql)
        self.assertEqual(inserts[0][1][0], "U1")
        self.assertEqual(inserts[0][1][1], gv.ACCOUNT_SCOPE)

    def test_duplicate_live_rows_raise_one_monitor_message(self):
        rows = [_alert(1), _alert(2)]
        conn = _FakeConn(live=[(r["account_id"], r["symbol"], r["breach"], r["state"]) for r in rows])
        plan = gn.nag_tick(conn, now=_moment("10:00"), accounts=[], slots=[], session=False)
        self.assertIn("duplicate live alert rows", plan.monitor)
        self.assertEqual(len(plan.signature), 1)

    def test_same_duplicate_signature_does_not_repeat_the_monitor(self):
        rows = [("U1", "NVDA", "x", "open"), ("U1", "NVDA", "x", "open")]
        sig = (("U1", "NVDA", "x", 2),)
        conn = _FakeConn(live=rows)
        plan = gn.nag_tick(conn, now=_moment("10:00"), accounts=[], slots=[],
                           session=False, previous_signature=sig)
        self.assertEqual(plan.monitor, "")

    def test_monitor_is_not_a_nag(self):
        rows = [("U1", "NVDA", "x", "open"), ("U1", "NVDA", "x", "open")]
        conn = _FakeConn(live=rows)
        plan = gn.nag_tick(conn, now=_moment("10:00"), accounts=[], slots=[], session=False)
        self.assertEqual(plan.nags, ())
        self.assertTrue(plan.monitor)

    def test_zero_migration_no_new_table_in_any_statement(self):
        conn = _FakeConn()
        gn.nag_tick(conn, now=_moment("10:00"), accounts=[], slots=[], session=False)
        for sql, _ in conn.calls:
            self.assertNotIn("CREATE TABLE", sql)
            self.assertNotIn("ALTER TABLE", sql)


# ---------------------------------------------------------------------------
# Bots: D81 callback registry + D71 single-push interface
# ---------------------------------------------------------------------------


class TestGuardianMarkup(unittest.TestCase):
    def test_one_resolve_button_per_alert(self):
        markup = tg.guardian_markup(42)
        buttons = markup["inline_keyboard"][0]
        self.assertEqual(len(buttons), 1)
        self.assertEqual(buttons[0]["callback_data"], "guardres:42")

    def test_each_alert_gets_its_own_button(self):
        self.assertNotEqual(tg.guardian_markup(1), tg.guardian_markup(2))


class TestCallbackRegistry(unittest.TestCase):
    def setUp(self):
        self._saved = dict(tg._CALLBACKS)
        tg._CALLBACKS.clear()

    def tearDown(self):
        tg._CALLBACKS.clear()
        tg._CALLBACKS.update(self._saved)

    def test_registered_handler_receives_db_actor_and_payload(self):
        seen = []
        tg.register_callback("guardres", lambda db, actor, payload: seen.append((db, actor, payload)) or "ok")
        client = _FakeClient()
        query = {"id": "q1", "from": {"id": "op1"}, "data": "guardres:7",
                 "message": {"chat": {"id": 9}, "message_id": 3, "text": "ALERT"}}
        with patch.object(tg, "_fresh_allowlist", return_value={"op1"}):
            tg._handle_callback(client, "db-url", set(), query)
        self.assertEqual(seen, [("db-url", "op1", "7")])
        self.assertEqual(client.calls[0][1]["text"], "ok")

    def test_non_allowlisted_actor_is_rejected_before_dispatch(self):
        seen = []
        tg.register_callback("guardres", lambda *a: seen.append(a) or "ok")
        client = _FakeClient()
        query = {"id": "q1", "from": {"id": "intruder"}, "data": "guardres:7", "message": {}}
        with patch.object(tg, "_fresh_allowlist", return_value={"op1"}):
            tg._handle_callback(client, "db-url", set(), query)
        self.assertEqual(seen, [])
        self.assertIn("Not allowlisted", client.calls[0][1]["text"])

    def test_approve_and_reject_still_route_to_drafts(self):
        client = _FakeClient()
        query = {"id": "q1", "from": {"id": "op1"}, "data": "approve:d1", "message": {}}
        with patch.object(tg, "_fresh_allowlist", return_value={"op1"}), \
             patch.object(tg, "_apply", return_value="Approved d1.") as apply_mock:
            tg._handle_callback(client, "db-url", set(), query)
        apply_mock.assert_called_once()
        self.assertEqual(apply_mock.call_args[0][2], "d1")
        self.assertEqual(apply_mock.call_args[0][3], "approve")

    def test_reserved_prefixes_cannot_be_hijacked(self):
        for prefix in ("approve", "reject", "", "bad:prefix", "has space"):
            with self.assertRaises(ValueError):
                tg.register_callback(prefix, lambda *a: "x")

    def test_unknown_prefix_answers_unknown_button(self):
        client = _FakeClient()
        query = {"id": "q1", "from": {"id": "op1"}, "data": "wut:7", "message": {}}
        with patch.object(tg, "_fresh_allowlist", return_value={"op1"}):
            tg._handle_callback(client, "db-url", set(), query)
        self.assertEqual(client.calls[0][1]["text"], "Unknown button")

    def test_re_registering_replaces_the_handler(self):
        tg.register_callback("guardres", lambda *a: "first")
        tg.register_callback("guardres", lambda *a: "second")
        self.assertEqual(tg._CALLBACKS["guardres"]("db", "a", "p"), "second")

    def test_handler_exception_is_reported_not_raised(self):
        def boom(*_a):
            raise RuntimeError("db down")
        tg.register_callback("guardres", boom)
        client = _FakeClient()
        query = {"id": "q1", "from": {"id": "op1"}, "data": "guardres:7", "message": {}}
        with patch.object(tg, "_fresh_allowlist", return_value={"op1"}):
            tg._handle_callback(client, "db-url", set(), query)
        self.assertIn("RuntimeError", client.calls[0][1]["text"])


class TestNotifyGuardian(unittest.TestCase):
    def test_notify_guardian_sends_its_own_inline_resolve(self):
        client = _FakeClient()
        with patch.object(tg, "_live_send", return_value=("tok", {"c1"})), \
             patch.object(tg, "TelegramClient", return_value=client):
            tg.notify_guardian(7, "ALERT")
        method, payload = client.calls[0]
        self.assertEqual(method, "sendMessage")
        self.assertEqual(payload["text"], "ALERT")
        self.assertEqual(payload["reply_markup"]["inline_keyboard"][0][0]["callback_data"], "guardres:7")

    def test_single_push_has_no_markup(self):
        client = _FakeClient()
        with patch.object(tg, "_live_send", return_value=("tok", {"c1"})), \
             patch.object(tg, "TelegramClient", return_value=client):
            tg.notify_guardian_once("Run C label")
        self.assertNotIn("reply_markup", client.calls[0][1])

    def test_empty_token_is_a_noop(self):
        client = _FakeClient()
        with patch.object(tg, "_live_send", return_value=("", {"c1"})), \
             patch.object(tg, "TelegramClient", return_value=client):
            tg.notify_guardian(1, "x")
        self.assertEqual(client.calls, [])

    def test_one_message_per_allowlisted_chat(self):
        client = _FakeClient()
        with patch.object(tg, "_live_send", return_value=("tok", {"c1", "c2"})), \
             patch.object(tg, "TelegramClient", return_value=client):
            tg.notify_guardian(1, "x")
        self.assertEqual(len(client.calls), 2)


# ---------------------------------------------------------------------------
# D80 failed-run markers (section 14.3 frozen seam)
# ---------------------------------------------------------------------------


class TestFailedRunMarker(unittest.TestCase):
    def test_failed_grade_is_degraded_and_names_the_exception(self):
        grade = gr.failed_grade("U1", _slot("A"), RuntimeError("boom"), datetime.now(UTC))
        self.assertEqual(grade.status, "failed")
        self.assertEqual(grade.detail, "RuntimeError")
        self.assertEqual(grade.payload, {})

    def test_write_failed_run_omits_all_grade_columns(self):
        conn = _FakeConn()
        grade = gr.failed_grade("U1", _slot("A"), KeyError("x"), datetime.now(UTC))
        gr.write_failed_run(conn, grade)
        sql = conn.calls[0][0]
        for forbidden in ("breach_count", "el_nl_pct", "maint_nl_pct", "account_flags", "positions"):
            self.assertNotIn(forbidden, sql)
        self.assertIn("'unknown', 'unknown', 'failed'", sql)
        self.assertIn("ON CONFLICT (account_id, trade_day, run) DO NOTHING", sql)
        self.assertEqual(conn.calls[0][1][-1], "KeyError")

    def test_write_run_success_path_still_writes_full_payload(self):
        conn = _FakeConn()
        payload = {"portfolio_tier": "healthy", "account_tier": "healthy",
                   "breach_count": 0, "proximity_count": 0, "unmanaged_count": 0,
                   "el_nl_pct": None, "maint_nl_pct": None,
                   "account_flags": [], "positions": []}
        grade = gr.RunGrade("U1", DAY, "A", _slot("A").moment,
                           datetime.now(UTC), payload)
        gr.write_run(conn, grade)
        self.assertIn("breach_count", conn.calls[0][0])


class TestInstallCallbacks(unittest.TestCase):
    def setUp(self):
        self._saved = dict(tg._CALLBACKS)
        tg._CALLBACKS.clear()

    def tearDown(self):
        tg._CALLBACKS.clear()
        tg._CALLBACKS.update(self._saved)

    def test_install_registers_guardres_prefix(self):
        gn.install_guardian_callbacks("db-url")
        self.assertIn("guardres", tg._CALLBACKS)

    def test_handler_resolves_the_alert_and_commits(self):
        from unittest.mock import MagicMock
        gn.install_guardian_callbacks("db-url")
        handler = tg._CALLBACKS["guardres"]
        conn = _FakeConn(lookup=[("U1",)])
        fake_connect = MagicMock()
        fake_connect.return_value.__enter__.return_value = conn
        with patch("athena_store.connect", fake_connect), \
             patch.object(gv, "resolve", return_value=None) as resolve_mock:
            result = handler("db-url", "op1", "7")
        self.assertEqual(result, "Resolved alert 7.")
        self.assertEqual(resolve_mock.call_args[0][1], "U1")
        self.assertEqual(resolve_mock.call_args[0][2], 7)
        self.assertEqual(conn.commits, 1)

    def test_handler_unknown_alert_is_rejected(self):
        from unittest.mock import MagicMock
        gn.install_guardian_callbacks("db-url")
        handler = tg._CALLBACKS["guardres"]
        conn = _FakeConn(lookup=[])
        fake_connect = MagicMock()
        fake_connect.return_value.__enter__.return_value = conn
        with patch("athena_store.connect", fake_connect):
            self.assertEqual(handler("db-url", "op1", "7"), "Unknown alert.")

    def test_handler_bad_payload_is_rejected(self):
        gn.install_guardian_callbacks("db-url")
        self.assertEqual(tg._CALLBACKS["guardres"]("db-url", "op1", "not-an-int"), "Unknown alert.")


# ---------------------------------------------------------------------------
# D82 fixed cadence + wiring + zero-migration boundary (static)
# ---------------------------------------------------------------------------


class TestWiring(unittest.TestCase):
    def test_cadence_is_300_seconds(self):
        self.assertEqual(gn.NAG_INTERVAL, 300)

    def test_nag_loop_waits_on_fixed_interval_not_settings(self):
        src = (ROOT / "apps/orchestrator/src/athena_orchestrator/guardian_nag.py").read_text(encoding="utf-8")
        self.assertIn("_STOP.wait(NAG_INTERVAL)", src)
        self.assertNotIn("cycle_minutes", src)

    def test_loop_starts_and_stops_the_nag_daemon(self):
        src = (ROOT / "apps/orchestrator/src/athena_orchestrator/loop.py").read_text(encoding="utf-8")
        self.assertIn("start_guardian_nag(database_url)", src)
        self.assertIn("install_guardian_callbacks(database_url)", src)
        self.assertEqual(src.count("stop_guardian_nag()"), 2)
        # registry must be installed before polling starts
        self.assertLess(src.index("install_guardian_callbacks(database_url)"),
                        src.index("start_telegram_polling(database_url)"))

    def test_bots_never_imports_orchestrator(self):
        src = (ROOT / "apps/bots/src/athena_bots/telegram.py").read_text(encoding="utf-8")
        self.assertNotIn("athena_orchestrator", src)

    def test_055_belongs_to_r7_not_r4(self):
        # R4 never took 055; R7 consumes it for regime_labels (D69 / D104).
        folder = ROOT / "packages/store/src/athena_store/migrations"
        self.assertTrue((folder / "055_regime_labels.sql").exists())
        self.assertEqual(sorted(p.name for p in folder.glob("05*.sql")),
                         ["050_cycle_progress.sql", "051_advisor_signals.sql",
                          "052_order_events.sql", "053_guardian_alerts.sql",
                          "054_guardian_runs.sql", "055_regime_labels.sql"])

    def test_no_new_settings_field(self):
        src = (ROOT / "apps/orchestrator/src/athena_orchestrator/guardian_nag.py").read_text(encoding="utf-8")
        self.assertNotIn("set_knob", src)
        self.assertNotIn("SettingsPayload", src)

    def test_d83_no_in_memory_dedup_table(self):
        src = (ROOT / "apps/orchestrator/src/athena_orchestrator/guardian_nag.py").read_text(encoding="utf-8")
        self.assertNotIn("_seen_alerts", src)
        self.assertNotIn("_sent_ids", src)
        self.assertIn("WHERE NOT EXISTS", src)


if __name__ == "__main__":
    unittest.main()
