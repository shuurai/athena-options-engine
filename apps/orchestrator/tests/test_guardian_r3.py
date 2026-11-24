"""R3 Guardian scheduler (booklet 14, sections 14-15).

Covers: ET/DST wall-clock anchoring (D60), next_run ordering and weekend skip,
Run C derivation without execution (D71), grade persistence + idempotency,
read-only /guard card escaping, migration 054 registration, and loop/pages wiring.
"""

from __future__ import annotations

import unittest
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from athena_risk import AccountMarks, GuardianPosition, GuardianRules, grade
from psycopg.types.json import Jsonb

from athena_orchestrator import guardian_runner as gr
from athena_orchestrator import guardian_schedule as gs
from athena_orchestrator import guardian_view as gv

UTC = timezone.utc
ROOT = Path(__file__).resolve().parents[3]


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
    def __init__(self, rows=None):
        self.calls: list[tuple[str, tuple]] = []
        self.rows = rows or []
        self.commits = 0

    def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params))
        return _Cursor(self.rows)

    def commit(self):
        self.commits += 1


class _Snapshot:
    def __init__(self, report):
        self.report = report
        self.base_currency = "USD"


def _report(nav=Decimal("100000"), excess=Decimal("20000")):
    positions = [
        GuardianPosition(
            symbol="NVDA",
            quantity=Decimal("100"),
            market_value=Decimal("10000"),
        ),
    ]
    marks = AccountMarks(
        net_liquidation=nav,
        excess_liquidity=excess,
        maint_margin_req=Decimal("10000"),
        margin_call=False,
        base_currency="USD",
    )
    return grade(positions, marks, GuardianRules())


def _slot(run="A", day=date(2026, 7, 1)):
    return gs.RunSlot(run=run, trade_day=day, moment=gs.run_moments(day)[run])


# ---------------------------------------------------------------------------
# Schedule: wall times and DST (D60)
# ---------------------------------------------------------------------------


class TestRunWallTimes(unittest.TestCase):
    def test_a_is_open_minus_60(self):
        self.assertEqual(gs.run_wall_times()["A"], time(8, 30))

    def test_b_is_open_plus_5(self):
        self.assertEqual(gs.run_wall_times()["B"], time(9, 35))

    def test_c_is_1230_et(self):
        self.assertEqual(gs.run_wall_times()["C"], time(12, 30))

    def test_us_open_is_0930_et(self):
        self.assertEqual(gs.US_OPEN, time(9, 30))

    def test_runs_are_chronological(self):
        self.assertEqual(gs.RUNS, ("A", "B", "C"))

    def test_edt_utc_instants(self):
        m = gs.run_moments(date(2026, 7, 1))
        self.assertEqual(m["A"].strftime("%H:%M"), "12:30")
        self.assertEqual(m["B"].strftime("%H:%M"), "13:35")
        self.assertEqual(m["C"].strftime("%H:%M"), "16:30")

    def test_est_utc_instants(self):
        m = gs.run_moments(date(2026, 12, 1))
        self.assertEqual(m["A"].strftime("%H:%M"), "13:30")
        self.assertEqual(m["B"].strftime("%H:%M"), "14:35")
        self.assertEqual(m["C"].strftime("%H:%M"), "17:30")

    def test_edt_hkt_wall_clock_matches_d60_case(self):
        m = gs.run_moments(date(2026, 7, 1))
        self.assertEqual(gs.hk_clock(m["A"]), "20:30")
        self.assertEqual(gs.hk_clock(m["B"]), "21:35")

    def test_est_hkt_wall_clock_matches_d60_case(self):
        m = gs.run_moments(date(2026, 12, 1))
        self.assertEqual(gs.hk_clock(m["A"]), "21:30")
        self.assertEqual(gs.hk_clock(m["B"]), "22:35")

    def test_dst_boundary_does_not_drift_hkt_by_more_than_an_hour(self):
        """D60: fixed HKT clocks would put B before the winter open."""
        edt = gs.hk_clock(gs.run_moments(date(2026, 7, 1))["B"])
        est = gs.hk_clock(gs.run_moments(date(2026, 12, 1))["B"])
        self.assertEqual(edt, "21:35")
        self.assertEqual(est, "22:35")

    def test_run_c_hkt_is_next_day(self):
        self.assertEqual(
            gs.hk_stamp(gs.run_moments(date(2026, 7, 1))["C"]),
            "2026-07-02 00:30 HKT",
        )
        self.assertEqual(
            gs.hk_stamp(gs.run_moments(date(2026, 12, 1))["C"]),
            "2026-12-02 01:30 HKT",
        )

    def test_all_moments_are_utc_aware(self):
        for day in (date(2026, 3, 8), date(2026, 11, 1), date(2026, 7, 1)):
            for moment in gs.run_moments(day).values():
                self.assertEqual(moment.tzinfo, UTC)

    def test_no_hardcoded_dst_switch_date(self):
        """section 14.1: slots.py's US_STANDARD_FROM must not leak in here."""
        source = (ROOT / "apps/orchestrator/src/athena_orchestrator/guardian_schedule.py").read_text(encoding="utf-8")
        self.assertNotIn("US_STANDARD_FROM", source)
        self.assertIn('ZoneInfo("America/New_York")', source)


# ---------------------------------------------------------------------------
# Schedule: next_run and weekday gate
# ---------------------------------------------------------------------------


class TestNextRun(unittest.TestCase):
    def test_before_a_returns_a_today(self):
        slot = gs.next_run(datetime(2026, 7, 1, 12, 0, tzinfo=UTC))
        self.assertEqual((slot.run, slot.trade_day, gs.hk_clock(slot.moment)), ("A", date(2026, 7, 1), "20:30"))

    def test_between_a_and_b_returns_b_today(self):
        slot = gs.next_run(datetime(2026, 7, 1, 13, 0, tzinfo=UTC))
        self.assertEqual((slot.run, slot.trade_day, gs.hk_clock(slot.moment)), ("B", date(2026, 7, 1), "21:35"))

    def test_after_b_returns_next_weekday_a(self):
        slot = gs.next_run(datetime(2026, 12, 1, 15, 0, tzinfo=UTC))
        self.assertEqual((slot.run, slot.trade_day), ("A", date(2026, 12, 2)))

    def test_friday_after_b_skips_weekend(self):
        slot = gs.next_run(datetime(2026, 12, 4, 15, 0, tzinfo=UTC))
        self.assertEqual((slot.run, slot.trade_day), ("A", date(2026, 12, 7)))

    def test_saturday_returns_monday_a(self):
        slot = gs.next_run(datetime(2026, 12, 5, 15, 0, tzinfo=UTC))
        self.assertEqual((slot.run, slot.trade_day), ("A", date(2026, 12, 7)))

    def test_weekday_gate(self):
        self.assertTrue(gs.is_us_weekday(date(2026, 12, 4)))   # Fri
        self.assertFalse(gs.is_us_weekday(date(2026, 12, 5)))  # Sat
        self.assertFalse(gs.is_us_weekday(date(2026, 12, 6)))  # Sun
        self.assertTrue(gs.is_us_weekday(date(2026, 12, 7)))   # Mon

    def test_non_utc_aware_now_is_converted_correctly(self):
        # An HKT-aware 'now' must give the same slot as the UTC equivalent.
        from athena_orchestrator.guardian_schedule import HKT
        hkt_now = datetime(2026, 7, 1, 20, 0, tzinfo=HKT)
        utc_now = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
        self.assertEqual(gs.next_run(hkt_now), gs.next_run(utc_now))

    def test_run_c_never_returned_by_default(self):
        for hour in range(24):
            slot = gs.next_run(datetime(2026, 7, 1, hour, 0, tzinfo=UTC))
            self.assertIsNotNone(slot)
            self.assertNotEqual(slot.run, "C")

    def test_executed_runs_excludes_c(self):
        self.assertEqual(gs.EXECUTED_RUNS, ("A", "B"))

    def test_r7_can_opt_into_run_c(self):
        slot = gs.next_run(datetime(2026, 7, 1, 15, 0, tzinfo=UTC), runs=("C",))
        self.assertEqual((slot.run, gs.hk_clock(slot.moment)), ("C", "00:30"))

    def test_run_c_is_always_derived(self):
        for day in (date(2026, 7, 1), date(2026, 12, 1)):
            self.assertIn("C", gs.run_moments(day))


# ---------------------------------------------------------------------------
# Pure payload
# ---------------------------------------------------------------------------


class TestReportPayload(unittest.TestCase):
    def setUp(self):
        self.payload = gr.report_payload(_report())

    def test_tiers_are_plain_strings(self):
        self.assertIsInstance(self.payload["portfolio_tier"], str)
        self.assertIsInstance(self.payload["account_tier"], str)

    def test_counts_are_ints(self):
        for key in ("breach_count", "proximity_count", "unmanaged_count"):
            self.assertIsInstance(self.payload[key], int)

    def test_flags_are_lists(self):
        self.assertIsInstance(self.payload["account_flags"], list)
        self.assertIsInstance(self.payload["positions"], list)

    def test_position_shape(self):
        row = self.payload["positions"][0]
        self.assertEqual(row["symbol"], "NVDA")
        self.assertIn("tier", row)
        self.assertIsInstance(row["flags"], list)
        self.assertIsInstance(row["unmanaged"], bool)

    def test_real_ratios_are_decimal(self):
        self.assertIsInstance(self.payload["el_nl_pct"], Decimal)
        self.assertIsInstance(self.payload["maint_nl_pct"], Decimal)

    def test_missing_numbers_stay_none_never_invented(self):
        payload = gr.report_payload(_report(nav=None, excess=None))
        self.assertIsNone(payload["el_nl_pct"])
        self.assertIsNone(payload["maint_nl_pct"])

    def test_payload_is_json_serialisable_except_decimals(self):
        # Decimal is adapted by psycopg into numeric; nothing else is exotic.
        import json
        json.dumps(self.payload["account_flags"])
        json.dumps(self.payload["positions"])


# ---------------------------------------------------------------------------
# Thin SQL: write / load
# ---------------------------------------------------------------------------


class TestWriteRun(unittest.TestCase):
    def setUp(self):
        self.conn = _FakeConn()
        self.grade_row = gr.RunGrade(
            account_id="U1",
            trade_day=date(2026, 7, 1),
            run="A",
            scheduled_at=gs.run_moments(date(2026, 7, 1))["A"],
            ran_at=datetime(2026, 7, 1, 12, 31, tzinfo=UTC),
            payload=gr.report_payload(_report()),
        )

    def test_one_insert_only(self):
        gr.write_run(self.conn, self.grade_row)
        self.assertEqual(len(self.conn.calls), 1)

    def test_sql_has_on_conflict_idempotency_guard(self):
        gr.write_run(self.conn, self.grade_row)
        self.assertIn("ON CONFLICT (account_id, trade_day, run) DO NOTHING", self.conn.calls[0][0])

    def test_sql_targets_guardian_runs(self):
        gr.write_run(self.conn, self.grade_row)
        self.assertIn("INSERT INTO guardian_runs", self.conn.calls[0][0])

    def test_params_carry_account_day_run(self):
        gr.write_run(self.conn, self.grade_row)
        params = self.conn.calls[0][1]
        self.assertEqual(params[0], "U1")
        self.assertEqual(params[1], date(2026, 7, 1))
        self.assertEqual(params[2], "A")

    def test_params_carry_scheduled_and_ran(self):
        gr.write_run(self.conn, self.grade_row)
        params = self.conn.calls[0][1]
        self.assertEqual(params[3], self.grade_row.scheduled_at)
        self.assertEqual(params[4], self.grade_row.ran_at)

    def test_jsonb_columns_use_jsonb_adapter(self):
        gr.write_run(self.conn, self.grade_row)
        params = self.conn.calls[0][1]
        self.assertIsInstance(params[12], Jsonb)
        self.assertIsInstance(params[13], Jsonb)

    def test_status_and_detail_params(self):
        gr.write_run(self.conn, self.grade_row)
        params = self.conn.calls[0][1]
        self.assertEqual(params[14], "ok")
        self.assertEqual(params[15], "")

    def test_param_count_matches_columns(self):
        gr.write_run(self.conn, self.grade_row)
        self.assertEqual(len(self.conn.calls[0][1]), 16)


class TestLoadRuns(unittest.TestCase):
    def test_empty_account_returns_no_rows_without_sql(self):
        conn = _FakeConn()
        self.assertEqual(gr.load_runs(conn, ""), [])
        self.assertEqual(conn.calls, [])

    def test_rows_are_mapped_and_hk_is_computed(self):
        moment = gs.run_moments(date(2026, 7, 1))["A"]
        conn = _FakeConn([
            (date(2026, 7, 1), "A", moment, moment, "healthy", 0, 1, 0, "ok", ""),
        ])
        rows = gr.load_runs(conn, "U1")
        self.assertEqual(rows[0]["run"], "A")
        self.assertEqual(rows[0]["hk"], "20:30")
        self.assertEqual(rows[0]["portfolio_tier"], "healthy")
        self.assertEqual(rows[0]["breach_count"], 0)

    def test_sql_is_account_scoped_and_limited(self):
        conn = _FakeConn([])
        gr.load_runs(conn, "U1", limit=4)
        sql, params = conn.calls[0]
        self.assertIn("WHERE account_id = %s", sql)
        self.assertEqual(params, ("U1", 4))

    def test_string_scheduled_at_does_not_crash_hk(self):
        conn = _FakeConn([
            ("2026-07-01", "A", "12:30 UTC", "12:31 UTC", "healthy", 0, 0, 0, "ok", ""),
        ])
        self.assertEqual(gr.load_runs(conn, "U1")[0]["hk"], "")

    def test_order_is_latest_first(self):
        conn = _FakeConn([])
        gr.load_runs(conn, "U1")
        self.assertIn("ORDER BY ran_at DESC, id DESC", conn.calls[0][0])


# ---------------------------------------------------------------------------
# run_once
# ---------------------------------------------------------------------------


class TestRunOnce(unittest.TestCase):
    def test_calls_sync_with_the_actual_run_moment(self):
        conn = _FakeConn()
        slot = _slot()
        moment = datetime(2026, 7, 1, 12, 31, tzinfo=UTC)
        with patch.object(gr.gv, "sync", return_value=(_Snapshot(_report()), [])) as sync, \
             patch.object(gr, "write_run") as write:
            gr.run_once(
                conn, "U1", slot,
                fetch=lambda c, a: (None, [], []), now=moment,
            )
        self.assertEqual(sync.call_args.kwargs["now"], moment)
        self.assertEqual(sync.call_args.args[1], "U1")
        written = write.call_args.args[1]
        self.assertEqual((written.account_id, written.trade_day, written.run), ("U1", date(2026, 7, 1), "A"))

    def test_writes_scheduled_at_from_the_slot(self):
        conn = _FakeConn()
        slot = _slot("B", date(2026, 12, 1))
        with patch.object(gr.gv, "sync", return_value=(_Snapshot(_report()), [])), \
             patch.object(gr, "write_run") as write:
            gr.run_once(conn, "U1", slot, fetch=lambda c, a: (None, [], []))
        self.assertEqual(write.call_args.args[1].scheduled_at, slot.moment)
        self.assertEqual(write.call_args.args[1].run, "B")

    def test_commits_after_writing(self):
        conn = _FakeConn()
        with patch.object(gr.gv, "sync", return_value=(_Snapshot(_report()), [])), \
             patch.object(gr, "write_run"):
            gr.run_once(conn, "U1", _slot(), fetch=lambda c, a: (None, [], []))
        self.assertEqual(conn.commits, 1)

    def test_returns_the_snapshot(self):
        conn = _FakeConn()
        snapshot = _Snapshot(_report())
        with patch.object(gr.gv, "sync", return_value=(snapshot, [])), \
             patch.object(gr, "write_run"):
            out = gr.run_once(conn, "U1", _slot(), fetch=lambda c, a: (None, [], []))
        self.assertIs(out, snapshot)

    def test_default_fetch_is_the_r2_page_seam(self):
        with patch("athena_orchestrator.pages._guard_inputs", return_value=(None, [], [])) as fetch, \
             patch.object(gr.gv, "sync", return_value=(_Snapshot(_report()), [])), \
             patch.object(gr, "write_run"):
            gr.run_once(_FakeConn(), "U1", _slot())
        fetch.assert_called_once()


# ---------------------------------------------------------------------------
# Read-only card
# ---------------------------------------------------------------------------


class TestRenderRunHistory(unittest.TestCase):
    def test_empty_rows_produce_no_card(self):
        self.assertEqual(gr.render_run_history([]), "")

    def test_card_has_heading(self):
        html = gr.render_run_history([{"trade_day": "2026-07-01", "run": "A", "hk": "20:30", "portfolio_tier": "healthy", "breach_count": 0, "status": "ok"}])
        self.assertIn("Scheduled runs", html)
        self.assertIn("20:30 HKT", html)

    def test_run_c_location_is_visible(self):
        html = gr.render_run_history([{"trade_day": "2026-07-01", "run": "A"}])
        self.assertIn("Latest A / B grades.", html)
        self.assertIn("Run C is the midday regime observation on Portfolio Analysis.", html)
        self.assertNotIn("reserved for R7", html)

    def test_dynamic_text_is_escaped(self):
        html = gr.render_run_history([{"portfolio_tier": "<img onerror=1>", "status": "<script>"}])
        self.assertNotIn("<img", html)
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;img", html)

    def test_uses_shared_table_and_footnote_classes(self):
        html = gr.render_run_history([{"run": "A"}])
        self.assertIn('<table class="data">', html)
        self.assertIn('<p class="slot-note">', html)
        self.assertNotIn('class="guard-history"', html)
        self.assertNotIn('class="guard-empty"', html)

    def test_note_stays_first_child_when_card_injected(self):
        html = gv.render_board(
            _report(), [], [], note="hello",
            extra_cards=gr.render_run_history([{"run": "A"}]),
        )
        self.assertTrue(html.startswith('<p class="note">hello</p>'))

    def test_extra_cards_default_keeps_r2_output_unchanged(self):
        plain = gv.render_board(_report(), [], [])
        with_extra = gv.render_board(_report(), [], [], extra_cards="")
        self.assertEqual(plain, with_extra)

    def test_card_appears_after_summary(self):
        html = gv.render_board(
            _report(), [], [], extra_cards='<div class="card guard-runs">MARKER</div>',
        )
        self.assertLess(html.index("guard-summary"), html.index("MARKER"))


class TestGuardPageDegrade(unittest.TestCase):
    def test_missing_guardian_runs_table_does_not_break_the_page(self):
        """section 14.3: a migration gap degrades to no card, GET still 200."""
        from athena_orchestrator import pages

        with patch.object(pages, "_guard_inputs", return_value=(None, [], [])), \
             patch.object(gv, "sync", return_value=(_Snapshot(_report()), [])), \
             patch.object(gr, "load_runs", side_effect=RuntimeError("no table")):
            built = pages._guard_page(_FakeConn(), "U1", {})
        self.assertEqual(built["status"], 200)
        self.assertNotIn("Scheduled runs", built["body"])

    def test_card_is_shown_when_table_is_present(self):
        from athena_orchestrator import pages

        rows = [{"trade_day": "2026-07-01", "run": "A", "hk": "20:30",
                 "portfolio_tier": "healthy", "breach_count": 0, "status": "ok"}]
        with patch.object(pages, "_guard_inputs", return_value=(None, [], [])), \
             patch.object(gv, "sync", return_value=(_Snapshot(_report()), [])), \
             patch.object(gr, "load_runs", return_value=rows):
            built = pages._guard_page(_FakeConn(), "U1", {})
        self.assertEqual(built["status"], 200)
        self.assertIn("Scheduled runs", built["body"])
        self.assertIn("20:30 HKT", built["body"])


# ---------------------------------------------------------------------------
# Migration + wiring (static guards)
# ---------------------------------------------------------------------------


class TestMigration054(unittest.TestCase):
    def test_migration_file_exists(self):
        path = ROOT / "packages/store/src/athena_store/migrations/054_guardian_runs.sql"
        self.assertTrue(path.exists())

    def test_migration_is_registered_in_wheel(self):
        wheel = (ROOT / "packages/store/pyproject.toml").read_text(encoding="utf-8")
        self.assertIn("054_guardian_runs.sql", wheel)

    def test_migration_has_unique_idempotency_key(self):
        sql = (ROOT / "packages/store/src/athena_store/migrations/054_guardian_runs.sql").read_text(encoding="utf-8")
        self.assertIn("UNIQUE (account_id, trade_day, run)", sql)

    def test_migration_has_latest_index(self):
        sql = (ROOT / "packages/store/src/athena_store/migrations/054_guardian_runs.sql").read_text(encoding="utf-8")
        self.assertIn("guardian_runs_latest", sql)

    def test_migration_splits_into_two_clean_statements(self):
        from athena_store.migrate import _statements
        sql = (ROOT / "packages/store/src/athena_store/migrations/054_guardian_runs.sql").read_text(encoding="utf-8")
        parts = _statements(sql)
        self.assertEqual(len(parts), 2)
        self.assertIn("CREATE TABLE", parts[0])
        self.assertIn("CREATE INDEX", parts[1])

    def test_055_belongs_to_r7(self):
        # R3 (054) never took 055; R7 consumes it for regime_labels (D69 / D104).
        folder = ROOT / "packages/store/src/athena_store/migrations"
        self.assertEqual([p.name for p in folder.glob("055_*.sql")],
                         ["055_regime_labels.sql"])


class TestWiring(unittest.TestCase):
    def test_loop_starts_the_scheduler(self):
        src = (ROOT / "apps/orchestrator/src/athena_orchestrator/loop.py").read_text(encoding="utf-8")
        self.assertIn("start_guardian_scheduler(database_url)", src)

    def test_loop_stops_scheduler_on_both_restart_paths(self):
        src = (ROOT / "apps/orchestrator/src/athena_orchestrator/loop.py").read_text(encoding="utf-8")
        self.assertEqual(src.count("stop_guardian_scheduler()"), 2)

    def test_guard_page_injects_extra_cards(self):
        src = (ROOT / "apps/orchestrator/src/athena_orchestrator/pages.py").read_text(encoding="utf-8")
        self.assertIn("extra_cards=runs_html", src)

    def test_guard_page_degrades_when_table_missing(self):
        src = (ROOT / "apps/orchestrator/src/athena_orchestrator/pages.py").read_text(encoding="utf-8")
        self.assertIn("gr.load_runs(conn, account)", src)
        self.assertIn("except Exception:", src)

    def test_scheduler_thread_is_daemon(self):
        src = (ROOT / "apps/orchestrator/src/athena_orchestrator/guardian_runner.py").read_text(encoding="utf-8")
        self.assertIn('name="guardian-r3", daemon=True', src)

    def test_scheduler_never_passes_c(self):
        src = (ROOT / "apps/orchestrator/src/athena_orchestrator/guardian_runner.py").read_text(encoding="utf-8")
        self.assertIn("runs=EXECUTED_RUNS", src)


if __name__ == "__main__":
    unittest.main()
