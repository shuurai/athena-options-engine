"""§20.2 / §21.2 / V2-P2: strategy_events, truthful SSE and post-commit emit.

Contract pinned here:

* append-only narrow read model with a deterministic ``event_id``;
* structured payload allowlist only — free-text ``reason`` never in a frame;
* Q15: emit AFTER the domain commit, best effort, idempotent; a failed
  observation write records ``observation_gap`` and never touches the domain;
* operator-direct ``EXTERNAL`` / ``MANUAL`` drafts are not a strategy chain;
* cursor replay is ordered and an unknown / cross-account cursor is an
  explicit ``observation_gap``, never a silently empty interval;
* reconciler backfills only derivable transitions from persisted domain rows;
* no event => no animation; stale graph_version / reduced motion / wrong
  account / symbol / edge all suppress the pulse.

No engine restart, no live console, no PostgreSQL writes in this module.
"""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from pathlib import Path

from athena_orchestrator import console, iris_events, iris_reconcile, iris_view, pages
from athena_store import strategy_events as se

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "apps/orchestrator/src/athena_orchestrator"
STORE_SRC = ROOT / "packages/store/src/athena_store"
CONSOLE_SRC = SRC / "console.py"
IRIS_SRC = SRC / "iris_view.py"
PAGES_SRC = SRC / "pages.py"
DRAFTS_SRC = ROOT / "packages/strategy-nfr/src/athena_strategy/drafts.py"
LOOP_SRC = SRC / "loop.py"
SE_SRC = STORE_SRC / "strategy_events.py"
MIGRATION = STORE_SRC / "migrations/056_strategy_events.sql"
PYPROJECT = ROOT / "packages/store/pyproject.toml"


class _Cur:
    def __init__(self, rows):
        self.rows = list(rows)

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)


class _Conn:
    """Fake conn that routes SELECTs and can inject an observation write failure."""

    def __init__(self, *, rows=None, cursor_rows=None, fail_sql=None):
        self.calls: list[tuple] = []
        self.commits = 0
        self.rollbacks = 0
        self.rows = rows or []
        self.cursor_rows = cursor_rows or []
        self.fail_sql = fail_sql

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        if self.fail_sql and self.fail_sql in sql:
            raise RuntimeError("injected observation write failure")
        if "SELECT occurred_at, event_id FROM strategy_events" in sql:
            return _Cur(self.cursor_rows)
        if sql.strip().startswith("SELECT"):
            return _Cur(self.rows)
        return _Cur([])

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def sqls(self):
        return [sql for sql, _ in self.calls]


def _row(event_id="sev_a", occurred_at=None, *, status="draft_created",
         account_id="U1", subject_id="draft_1", graph_version=se.GRAPH_VERSION):
    return (
        event_id, "run_1", account_id, "draft", subject_id, status,
        "pending", "pending", "draft_created", "NVDA", "", graph_version,
        occurred_at or datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc),
        {},
    )


def _base_event(**overrides):
    kwargs = dict(
        account_id="U1", subject_type="draft", subject_id="draft_1",
        status="draft_created", transition="created", symbol="NVDA",
        payload={"tactic": "BUY"},
    )
    kwargs.update(overrides)
    return se.build_event(**kwargs)


class _CleanObservers(unittest.TestCase):
    def setUp(self):
        se.clear_observers()
        iris_events.detach()

    def tearDown(self):
        se.clear_observers()
        iris_events.detach()


# ---------------------------------------------------------------------------
# event identity and structured payload
# ---------------------------------------------------------------------------


class TestEventIdentity(_CleanObservers):
    def test_graph_version_matches_iris_view(self):
        self.assertEqual(se.GRAPH_VERSION, iris_view.GRAPH_VERSION)
        self.assertEqual(se.GRAPH_VERSION, "iris-p2-events-v1")

    def test_event_id_is_deterministic(self):
        a = se.event_id(subject_type="draft", subject_id="d1", transition="created")
        b = se.event_id(subject_type="draft", subject_id="d1", transition="created")
        self.assertEqual(a, b)
        self.assertTrue(a.startswith("sev_"))
        self.assertEqual(len(a), 36)

    def test_event_id_changes_with_subject_transition_run_and_version(self):
        base = se.event_id(subject_type="draft", subject_id="d1", transition="created")
        for kwargs in (
            {"subject_id": "d2"},
            {"transition": "pending->approved"},
            {"run_id": "r2"},
            {"version": "iris-p1-static-v1"},
            {"subject_type": "order"},
        ):
            args = dict(subject_type="draft", subject_id="d1", transition="created")
            args.update(kwargs)
            self.assertNotEqual(base, se.event_id(**args))

    def test_event_types_match_the_ratified_vocabulary(self):
        expected = {
            "news_ingested", "factor_computed", "strategy_started",
            "strategy_completed", "filtered", "quiet", "invalid",
            "draft_created", "draft_approved", "draft_rejected",
            "draft_expired", "draft_blocked", "draft_amended",
            "draft_replaced", "order_queued", "order_submitted",
            "order_blocked", "order_cancelled", "order_filled",
            "partial_fill", "observation_gap",
        }
        self.assertEqual(set(se.EVENT_TYPES), expected)
        self.assertEqual(len(se.EVENT_TYPES), 21)

    def test_payload_allowlist_has_no_free_text_fields(self):
        self.assertNotIn("reason", se.PAYLOAD_KEYS)
        self.assertNotIn("reason_text", se.PAYLOAD_KEYS)
        self.assertIn("reason_code", se.PAYLOAD_KEYS)


class TestValidateEvent(_CleanObservers):
    def test_rejects_unknown_status(self):
        with self.assertRaises(ValueError):
            se.validate_event({"status": "invented"})

    def test_rejects_missing_domain_keys(self):
        for key in ("account_id", "subject_type", "subject_id", "transition"):
            event = _base_event()
            event[key] = ""
            with self.assertRaises(ValueError):
                se.validate_event(event)

    def test_rejects_free_text_reason_in_payload(self):
        for key in ("reason", "reason_text"):
            event = _base_event()
            event["payload"] = {key: "because the model felt like it"}
            with self.assertRaisesRegex(ValueError, "free-text reason"):
                se.validate_event(event)

    def test_rejects_payload_key_outside_allowlist(self):
        event = _base_event()
        event["payload"] = {"secret": "token"}
        with self.assertRaises(ValueError):
            se.validate_event(event)

    def test_rejects_oversized_payload(self):
        event = _base_event()
        event["payload"] = {"tactic": "x" * 5000}
        with self.assertRaises(ValueError):
            se.validate_event(event)

    def test_rejects_non_string_structured_column(self):
        event = _base_event()
        event["symbol"] = 42
        with self.assertRaises(ValueError):
            se.validate_event(event)

    def test_rejects_bad_occurred_at(self):
        event = _base_event()
        event["occurred_at"] = object()
        with self.assertRaises(ValueError):
            se.validate_event(event)

    def test_accepts_iso_occurred_at(self):
        event = _base_event()
        event["occurred_at"] = "2026-11-25T12:00:00+00:00"
        se.validate_event(event)

    def test_build_event_is_valid_and_has_deterministic_id(self):
        event = _base_event()
        self.assertEqual(event["event_id"], se.event_id(
            subject_type="draft", subject_id="draft_1", transition="created"))
        self.assertEqual(event["graph_version"], se.GRAPH_VERSION)
        self.assertEqual(event["payload"], {"tactic": "BUY"})
        se.validate_event(event)


# ---------------------------------------------------------------------------
# Q15 post-commit emit
# ---------------------------------------------------------------------------


class TestEmitAfterCommit(_CleanObservers):
    def test_success_inserts_commits_and_notifies_observers(self):
        conn = _Conn()
        seen = []
        se.add_observer(seen.append)
        self.assertTrue(se.emit_after_commit(conn, _base_event()))
        self.assertEqual(conn.commits, 1)
        self.assertEqual(conn.rollbacks, 0)
        self.assertIn("INSERT INTO strategy_events", conn.sqls()[0])
        self.assertIn("ON CONFLICT (event_id) DO NOTHING", conn.sqls()[0])
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["status"], "draft_created")

    def test_failure_rolls_back_only_the_observation_and_never_raises(self):
        conn = _Conn(fail_sql="INSERT INTO strategy_events")
        seen = []
        se.add_observer(seen.append)
        self.assertFalse(se.emit_after_commit(conn, _base_event()))
        self.assertEqual(conn.rollbacks, 1)
        self.assertEqual(conn.commits, 0)
        self.assertEqual(seen, [])

    def test_a_broken_observer_does_not_break_the_write(self):
        conn = _Conn()

        def boom(_event):
            raise RuntimeError("observer down")

        seen = []
        se.add_observer(boom)
        se.add_observer(seen.append)
        self.assertTrue(se.emit_after_commit(conn, _base_event()))
        self.assertEqual(len(seen), 1)

    def test_observe_event_records_a_gap_when_the_write_fails(self):
        conn = _Conn(fail_sql="INSERT INTO strategy_events")
        self.assertFalse(se.observe_event(
            conn, account_id="U1", subject_type="draft", subject_id="d1",
            status="draft_approved", transition="pending->approved",
        ))
        inserts = [params for sql, params in conn.calls if "INSERT INTO strategy_events" in sql]
        self.assertEqual(len(inserts), 2)
        self.assertEqual(inserts[1][5], "observation_gap")  # status column
        self.assertEqual(inserts[1][8], "event_write_failed")  # reason_code column

    def test_operator_direct_tactics_are_not_a_strategy_chain(self):
        for tactic in ("EXTERNAL", "MANUAL", "external", "manual"):
            conn = _Conn()
            self.assertFalse(se.observe_event(
                conn, account_id="U1", subject_type="draft", subject_id="d1",
                status="draft_created", transition="created", tactic=tactic,
            ))
            self.assertEqual(conn.calls, [])

    def test_observer_registry_is_idempotent(self):
        seen = []
        se.add_observer(seen.append)
        se.add_observer(seen.append)
        self.assertEqual(se.observer_count(), 1)
        se.remove_observer(seen.append)
        self.assertEqual(se.observer_count(), 0)


# ---------------------------------------------------------------------------
# cursor replay
# ---------------------------------------------------------------------------


class TestReplay(_CleanObservers):
    def test_empty_cursor_reads_from_the_start(self):
        conn = _Conn(rows=[_row("sev_a"), _row("sev_b")])
        rows = se.replay(conn, account_id="U1")
        self.assertEqual([r["event_id"] for r in rows], ["sev_a", "sev_b"])
        self.assertEqual(len(rows), 2)
        self.assertIn("WHERE account_id = %s\nORDER BY occurred_at, event_id", conn.sqls()[0])
        self.assertEqual(conn.calls[0][1], ("U1", 200))

    def test_known_cursor_resolves_position_then_reads_after(self):
        stamp = datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc)
        conn = _Conn(cursor_rows=[(stamp, "sev_a")], rows=[_row("sev_b")])
        rows = se.replay(conn, account_id="U1", after_event_id="sev_a")
        self.assertEqual([r["event_id"] for r in rows], ["sev_b"])
        self.assertEqual(len(conn.calls), 2)
        self.assertIn("SELECT occurred_at, event_id FROM strategy_events", conn.sqls()[0])
        self.assertEqual(conn.calls[0][1], ("sev_a", "U1"))
        self.assertIn("(occurred_at, event_id) > (%s::timestamptz, %s)", conn.sqls()[1])
        self.assertEqual(conn.calls[1][1], ("U1", stamp, "sev_a", 200))

    def test_unknown_cursor_is_an_explicit_error_not_an_empty_interval(self):
        conn = _Conn(cursor_rows=[])
        with self.assertRaises(se.UnknownCursor):
            se.replay(conn, account_id="U1", after_event_id="sev_missing")

    def test_cursor_is_scoped_to_the_account(self):
        conn = _Conn(cursor_rows=[])
        with self.assertRaises(se.UnknownCursor):
            se.replay(conn, account_id="U1", after_event_id="sev_other_account")
        self.assertIn("AND account_id = %s", conn.sqls()[0])

    def test_replay_rows_keep_all_fourteen_columns(self):
        conn = _Conn(rows=[_row()])
        row = se.replay(conn, account_id="U1")[0]
        self.assertEqual(len(row), 14)
        self.assertEqual(row["status"], "draft_created")
        self.assertEqual(row["graph_version"], se.GRAPH_VERSION)

    def test_event_count_uses_one_scalar_query(self):
        conn = _Conn(rows=[(7,)])
        self.assertEqual(se.event_count(conn, account_id="U1"), 7)
        self.assertIn("SELECT count(*) FROM strategy_events", conn.sqls()[0])


# ---------------------------------------------------------------------------
# reconciler
# ---------------------------------------------------------------------------


class TestReconciler(_CleanObservers):
    def test_backfills_created_plus_terminal_from_drafts(self):
        stamp = datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc)
        conn = _Conn(rows=[("d1", "NVDA", "BUY", "approved", stamp, "U1")])
        result = se.reconcile_drafts(conn, account_id="U1")
        self.assertEqual(result, {"emitted": 2, "gaps": 0, "drafts": 1})
        inserts = [params for sql, params in conn.calls if "INSERT INTO strategy_events" in sql]
        self.assertEqual([p[5] for p in inserts], ["draft_created", "draft_approved"])

    def test_does_not_invent_events_for_unknown_status(self):
        stamp = datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc)
        conn = _Conn(rows=[("d1", "NVDA", "BUY", "mystery", stamp, "U1")])
        result = se.reconcile_drafts(conn, account_id="U1")
        self.assertEqual(result["emitted"], 1)  # only draft_created

    def test_write_failures_are_counted_as_gaps(self):
        stamp = datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc)
        conn = _Conn(rows=[("d1", "NVDA", "BUY", "rejected", stamp, "U1")],
                     fail_sql="INSERT INTO strategy_events")
        result = se.reconcile_drafts(conn, account_id="U1")
        self.assertEqual(result["gaps"], 2)
        self.assertEqual(result["emitted"], 0)

    def test_terminal_status_map_covers_the_ratified_vocabulary(self):
        src = SE_SRC.read_text(encoding="utf-8")
        for status in ("draft_approved", "draft_rejected", "draft_expired",
                       "draft_blocked", "draft_amended", "draft_replaced"):
            self.assertIn(f'"{status}"', src)

    def test_iris_reconcile_module_exposes_run_once_and_daemon_controls(self):
        conn = _Conn(rows=[])
        self.assertEqual(iris_reconcile.run_once(conn, account_id="U1"),
                         {"emitted": 0, "gaps": 0, "drafts": 0})
        conn2 = _Conn(rows=[("U1",), ("U2",)])
        self.assertEqual(iris_reconcile.accounts(conn2), ["U1", "U2"])
        self.assertTrue(callable(iris_reconcile.start_iris_reconciler))
        self.assertTrue(callable(iris_reconcile.stop_iris_reconciler))

    def test_loop_starts_and_stops_the_reconciler(self):
        text = LOOP_SRC.read_text(encoding="utf-8")
        self.assertIn("start_iris_reconciler(database_url)", text)
        self.assertEqual(text.count("stop_iris_reconciler()"), 2)


# ---------------------------------------------------------------------------
# live fan-out registry
# ---------------------------------------------------------------------------


class TestLiveRegistry(_CleanObservers):
    def test_publish_reaches_every_subscriber(self):
        a, b = iris_events.subscribe(), iris_events.subscribe()
        try:
            event = _base_event()
            iris_events.publish(event)
            self.assertEqual(a.get_nowait()["event_id"], event["event_id"])
            self.assertEqual(b.get_nowait()["event_id"], event["event_id"])
        finally:
            iris_events.unsubscribe(a)
            iris_events.unsubscribe(b)

    def test_unsubscribe_removes_only_that_listener(self):
        a, b = iris_events.subscribe(), iris_events.subscribe()
        iris_events.unsubscribe(a)
        self.assertEqual(iris_events.listener_count(), 1)
        iris_events.unsubscribe(b)
        self.assertEqual(iris_events.listener_count(), 0)

    def test_recent_is_bounded(self):
        for i in range(250):
            iris_events.publish({"i": i})
        self.assertEqual(len(iris_events.recent()), 200)

    def test_publish_is_separate_from_the_log_trail(self):
        src = (SRC / "iris_events.py").read_text(encoding="utf-8")
        self.assertNotIn("subscribe_trail(", src)
        self.assertNotIn("trail_recent(", src)
        self.assertNotIn("from athena_orchestrator import runtime", src)
        self.assertIn("_RECENT: deque[dict]", src)

    def test_attach_registers_exactly_one_observer(self):
        se.clear_observers()
        iris_events.detach()
        iris_events.attach()
        iris_events.attach()
        self.assertEqual(se.observer_count(), 1)
        iris_events.detach()
        self.assertEqual(se.observer_count(), 0)

    def test_console_attaches_the_live_observer_at_startup(self):
        text = CONSOLE_SRC.read_text(encoding="utf-8")
        self.assertIn("iris_events.attach()", text)


# ---------------------------------------------------------------------------
# SSE frame validation
# ---------------------------------------------------------------------------


class TestIrisFrame(_CleanObservers):
    def test_valid_frame_keeps_structured_fields_only(self):
        frame = console._iris_frame(_base_event())
        self.assertIsNotNone(frame)
        self.assertEqual(frame["status"], "draft_created")
        self.assertEqual(frame["graph_version"], se.GRAPH_VERSION)
        self.assertEqual(frame["payload"], {"tactic": "BUY"})
        self.assertNotIn("reason", frame["payload"])

    def test_wrong_account_is_dropped(self):
        event = _base_event(account_id="U2")
        self.assertIsNone(console._iris_frame(event, account="U1"))

    def test_unknown_status_is_dropped(self):
        event = _base_event()
        event["status"] = "invented"
        self.assertIsNone(console._iris_frame(event))

    def test_free_text_reason_is_dropped(self):
        event = _base_event()
        event["payload"] = {"reason": "prose"}
        self.assertIsNone(console._iris_frame(event))

    def test_payload_outside_allowlist_is_dropped(self):
        event = _base_event()
        event["payload"] = {"secret": "value"}
        self.assertIsNone(console._iris_frame(event))

    def test_non_dict_payload_is_dropped(self):
        event = _base_event()
        event["payload"] = ["not", "a", "dict"]
        self.assertIsNone(console._iris_frame(event))

    def test_stale_graph_version_is_passed_through_for_client_rejection(self):
        event = _base_event()
        event["graph_version"] = "iris-p1-static-v1"
        frame = console._iris_frame(event)
        self.assertEqual(frame["graph_version"], "iris-p1-static-v1")

    def test_missing_event_id_is_dropped(self):
        event = _base_event()
        event["event_id"] = ""
        self.assertIsNone(console._iris_frame(event))


# ---------------------------------------------------------------------------
# migration and wheel
# ---------------------------------------------------------------------------


class TestMigration(_CleanObservers):
    def test_migration_creates_append_only_narrow_table(self):
        text = MIGRATION.read_text(encoding="utf-8")
        self.assertIn("CREATE TABLE strategy_events", text)
        self.assertIn("event_id text PRIMARY KEY", text)
        self.assertIn("payload jsonb NOT NULL DEFAULT '{}'::jsonb", text)
        self.assertIn("occurred_at timestamptz NOT NULL", text)
        self.assertIn("graph_version text NOT NULL DEFAULT ''", text)
        for forbidden in ("UPDATE strategy_events", "DELETE FROM strategy_events",
                          "ALTER TABLE strategy_events", "DROP TABLE"):
            self.assertNotIn(forbidden, text)
        self.assertIn("CREATE INDEX strategy_events_occurred", text)

    def test_migration_is_registered_in_the_wheel_force_include(self):
        text = PYPROJECT.read_text(encoding="utf-8")
        self.assertIn("056_strategy_events.sql", text)

    def test_no_semicolon_inside_comments(self):
        for line in MIGRATION.read_text(encoding="utf-8").splitlines():
            if line.lstrip().startswith("--"):
                self.assertNotIn(";", line, line)


# ---------------------------------------------------------------------------
# console surface: SSE, CSS, JS, truthful animation
# ---------------------------------------------------------------------------


class TestConsoleSurface(_CleanObservers):
    def setUp(self):
        super().setUp()
        self.text = CONSOLE_SRC.read_text(encoding="utf-8")

    def test_new_sse_route_is_separate_from_logs_and_pulse(self):
        self.assertIn('if parsed.path == "/api/iris/events":', self.text)
        self.assertIn('self._send_iris_events(query)', self.text)
        self.assertNotIn("/api/iris/events", self.text.split(
            'def _send_logs', 1)[1].split("def ", 1)[0])

    def test_sse_replays_first_then_goes_live(self):
        body = self.text.split("def _send_iris_events", 1)[1].split(
            "def _write_iris_frame", 1)[0]
        self.assertLess(body.index("poll_db()  # cursor replay"), body.index("while True:"))
        self.assertIn("listener.get(timeout=5)", body)
        self.assertIn("poll_db()  # closes the replay -> live handoff", body)
        self.assertIn("except store_events.UnknownCursor:", body)
        self.assertIn('gap("unknown_cursor")', body)
        self.assertIn('gap("replay_unavailable")', body)

    def test_sse_writes_id_and_data_lines(self):
        body = self.text.split("def _write_iris_frame", 1)[1].split("def ", 1)[0]
        self.assertIn('"id: " + payload["event_id"]', body)
        self.assertIn('"data: " + json.dumps(payload', body)

    def test_observation_gap_frames_are_structured(self):
        body = self.text.split("def _send_iris_events", 1)[1].split(
            "def _write_iris_frame", 1)[0]
        self.assertIn('"status": "observation_gap"', body)
        self.assertNotIn('"reason":', body)

    def test_css_pulse_is_finite_and_not_perpetual(self):
        self.assertIn("animation: iris-pulse 900ms ease-out 1;", self.text)
        self.assertNotIn("infinite", self.text.split("@keyframes iris-pulse", 1)[0].split(
            ".iris-edge.iris-pulse", 1)[1])
        self.assertIn("@media (prefers-reduced-motion: reduce)", self.text)
        self.assertIn(".iris-edge.iris-pulse { animation: none !important; }", self.text)

    def test_client_uses_eventsource_and_guards_animation(self):
        self.assertIn("new EventSource('/api/iris/events')", self.text)
        self.assertIn("frame.status === 'observation_gap'", self.text)
        self.assertIn("frame.graph_version !== version", self.text)
        self.assertIn("if (!frame.edge_id)", self.text)
        self.assertIn("prefers-reduced-motion: reduce", self.text)
        self.assertIn("edge.classList.add('iris-pulse')", self.text)
        self.assertIn("bootIrisEvents();", self.text)

    def test_no_interval_polling_in_the_client(self):
        body = self.text.split("function bootIrisEvents()", 1)[1].split("})();", 1)[0]
        self.assertNotIn("setInterval", body)
        self.assertNotIn("WebSocket", body)

    def test_iris_body_stays_pure_and_read_only(self):
        iris_src = IRIS_SRC.read_text(encoding="utf-8")
        for forbidden in ("EventSource", "WebSocket", "setInterval", "addEventListener"):
            self.assertNotIn(forbidden, iris_src)
        self.assertIn("data-iris-static=\"true\"", iris_src)

    def test_iris_copy_states_no_event_means_no_animation(self):
        iris_src = IRIS_SRC.read_text(encoding="utf-8")
        self.assertIn("no event means no animation", iris_src)
        self.assertIn("A connector moves only when a matching structured run event arrives", iris_src)
        self.assertIn('data-iris-live="idle"', iris_src)
        pages_src = PAGES_SRC.read_text(encoding="utf-8")
        self.assertIn("Connectors move only on real run events", pages_src)


# ---------------------------------------------------------------------------
# domain wiring: post-commit, contained failure, operator direct
# ---------------------------------------------------------------------------


class TestDomainWiring(_CleanObservers):
    def setUp(self):
        super().setUp()
        self.text = DRAFTS_SRC.read_text(encoding="utf-8")

    def test_observe_helper_is_contained_and_post_commit(self):
        body = self.text.split("def _observe(conn", 1)[1].split("def _ORDER_EVENTS", 1)[0]
        self.assertIn("from athena_store import strategy_events", body)
        self.assertIn("strategy_events.observe_event(conn, **spec)", body)
        self.assertIn("except Exception:\n        pass", body)

    def test_domain_commit_precedes_every_observation_call(self):
        for name, end in (
            ("def expire_pending", "def draft_facts"),
            ("def apply_ticket_block", "def _ticket_leg"),
            ("def _insert_draft", "def draft_cash_need"),
            ("def decide", "def _queue_order"),
        ):
            with self.subTest(function=name):
                body = self.text.split(name, 1)[1].split(end, 1)[0]
                self.assertIn("conn.commit()", body)
                self.assertIn("_observe(conn", body)
                self.assertLess(body.index("conn.commit()"),
                                body.index("_observe(conn"))

    def test_decide_observes_approve_reject_and_orders(self):
        body = self.text.split("def decide(", 1)[1].split("def _queue_order", 1)[0]
        self.assertIn('"draft_approved" if approved else "draft_rejected"', body)
        self.assertIn("_observe_orders(", body)
        self.assertLess(body.index("conn.commit()"), body.index("_observe("))

    def test_observe_swallows_event_write_failures(self):
        """Q15: a broken observation adapter must never escape into the domain."""
        from unittest.mock import patch

        from athena_strategy import drafts

        class Boom:
            def execute(self, *_a, **_k):
                raise RuntimeError("db down")

        with patch("athena_store.strategy_events.observe_event",
                   side_effect=RuntimeError("observer exploded")):
            drafts._observe(Boom(), [dict(
                account_id="U1", subject_type="draft", subject_id="d1",
                status="draft_created", transition="created",
            )])

    def test_operator_direct_exclusion_is_in_the_store_not_the_renderer(self):
        self.assertIn('{"EXTERNAL", "MANUAL"}', SE_SRC.read_text(encoding="utf-8"))

    def test_order_status_map_covers_partial_fill_and_cancel(self):
        self.assertIn('"partial": "partial_fill"', self.text)
        self.assertIn('"cancelled": "order_cancelled"', self.text)
        self.assertIn('"blocked": "order_blocked"', self.text)


# ---------------------------------------------------------------------------
# §20.2.7 fixture: 4 tactics, 7 status classes, failure, no-event-no-animation
# ---------------------------------------------------------------------------


class TestP2Fixture(_CleanObservers):
    TACTICS = ("BUY", "OPEN_CSP", "OPEN_CC", "OPEN_STRANGLE")
    STATUSES = (
        ("rejected", "draft_rejected"),
        ("expired", "draft_expired"),
        ("amended", "draft_amended"),
        ("replaced", "draft_replaced"),
        ("blocked", "draft_blocked"),
        ("partial-fill", "partial_fill"),
        ("cancel", "order_cancelled"),
    )

    def test_four_tactics_produce_valid_frames(self):
        for tactic in self.TACTICS:
            with self.subTest(tactic=tactic):
                event = _base_event(payload={"tactic": tactic})
                se.validate_event(event)
                frame = console._iris_frame(event)
                self.assertEqual(frame["payload"]["tactic"], tactic)

    def test_seven_status_classes_produce_valid_frames(self):
        for label, status in self.STATUSES:
            with self.subTest(status=label):
                event = _base_event(status=status, transition=f"pending->{label}")
                se.validate_event(event)
                frame = console._iris_frame(event)
                self.assertEqual(frame["status"], status)
                self.assertIn("reason", json.dumps(frame))  # reason_code only, not reason
                self.assertNotIn("reason", frame["payload"])

    def test_event_write_failure_does_not_change_a_committed_domain_row(self):
        """Q15: the domain commit is already durable; only a gap is recorded."""
        conn = _Conn(fail_sql="INSERT INTO strategy_events")
        domain_commits_before = conn.commits
        ok = se.observe_event(
            conn, account_id="U1", subject_type="draft", subject_id="d1",
            status="draft_approved", transition="pending->approved",
        )
        self.assertFalse(ok)
        self.assertEqual(conn.commits, domain_commits_before)
        self.assertEqual(conn.rollbacks, 2)
        gap = [p for sql, p in conn.calls
               if "INSERT INTO strategy_events" in sql and p[5] == "observation_gap"]
        self.assertEqual(len(gap), 1)

    def test_replay_to_live_handoff_is_deduplicated_by_event_id(self):
        stamp = datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc)
        event = _base_event()
        conn = _Conn(cursor_rows=[(stamp, event["event_id"])], rows=[])
        replayed = se.replay(conn, account_id="U1", after_event_id=event["event_id"])
        self.assertEqual(replayed, [])  # cursor already at the last event
        # A duplicate live publish with the same id must not re-animate.
        frames = [console._iris_frame(event), console._iris_frame(event)]
        self.assertEqual(frames[0]["event_id"], frames[1]["event_id"])
        self.assertEqual(len({f["event_id"] for f in frames}), 1)

    def test_no_event_means_no_animation(self):
        css = CONSOLE_SRC.read_text(encoding="utf-8")
        self.assertIn("no event means no animation", IRIS_SRC.read_text(encoding="utf-8"))
        self.assertIn("No perpetual animation", css)
        self.assertIn("Event stream idle. No structured run events yet.",
                      IRIS_SRC.read_text(encoding="utf-8"))
        # Idle state is the initial DOM state; no class is applied without a frame.
        self.assertIn('data-iris-live="idle"', IRIS_SRC.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
