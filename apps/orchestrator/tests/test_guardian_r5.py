"""R5 Guardian static trigger lines (booklet 14, sections 19-21; D84-D90).

Covers: the declared Settings field (D85), grade-only reconcile (D86), the
pure state-judgement core (D88/D94), state-layer uniqueness (D83), the fixed
300s nag_tick hook (D87), the danger tier (D89), /settings add/remove (D90),
the VIX / zero-migration / no-new-feed boundary (D84/D91), and the D66 note.
"""

from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from athena_orchestrator import guardian_nag as gn
from athena_orchestrator import guardian_trigger as gt
from athena_orchestrator import guardian_view as gv
from athena_orchestrator import pages
from athena_risk import GuardianReport, Tier
from athena_settings.model import SettingsPayload, TriggerLine, standard_payload
from athena_settings.repository import SettingsVersion, _diff, _payload_of, save_settings

UTC = UTC
ROOT = Path(__file__).resolve().parents[3]
NOW = datetime(2026, 7, 1, 14, 0, tzinfo=UTC)  # 10:00 ET, an EDT Wednesday


def _line(symbol="NVDA", op="<", level=118) -> TriggerLine:
    return TriggerLine(symbol=symbol, op=op, level=level)


def _payload(lines=()) -> SettingsPayload:
    return standard_payload().model_copy(update={"trigger_lines": list(lines)})


def _report():
    return GuardianReport(
        portfolio_tier=Tier.HEALTHY,
        account_tier=Tier.HEALTHY,
        positions=(),
        account_flags=(),
        el_nl_pct=None,
        maint_nl_pct=None,
        breach_count=0,
        proximity_count=0,
        unmanaged_count=0,
    )


# ---------------------------------------------------------------------------
# Fakes: route by SQL fragment so tests assert exact statement shape
# ---------------------------------------------------------------------------


class _Cursor:
    def __init__(self, rows):
        self.rows = list(rows)

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)


class _FakeConn:
    def __init__(self, *, routes=None, settings=None, bars=None, daily=None,
                 trigger_rows=None, due=None, live=None, runs=None):
        self.calls: list[tuple[str, tuple]] = []
        self.routes = routes or {}
        self.settings = settings or []
        self.bars = bars or {}
        self.daily = daily or {}
        self.trigger_rows = trigger_rows or []
        self.due = due or []
        self.live = live or []
        self.runs = runs or []
        self.commits = 0

    def execute(self, sql, params=None):
        flat = " ".join(sql.split())
        self.calls.append((flat, params))
        for key, rows in self.routes.items():
            if key in flat:
                return _Cursor(rows)
        if flat.startswith("SELECT account_id, payload FROM settings_versions"):
            return _Cursor(self.settings)
        if "WHERE source = 'trigger' AND state <> 'auto_cleared'" in flat:
            return _Cursor(self.trigger_rows)
        if "source = 'ib-delayed'" in flat:
            symbol = params[0]
            return _Cursor([self.bars[symbol]] if symbol in self.bars else [])
        if "source IN ('ib-daily', 'ib-delayed')" in flat:
            return _Cursor(self.daily.get(params[0], []))
        if "FROM guardian_runs" in flat:
            return _Cursor(self.runs)
        if "WHERE state = 'open'" in flat:
            return _Cursor(self.due)
        if "WHERE state IN ('open', 'skipped')" in flat:
            return _Cursor(self.live)
        return _Cursor([])

    def commit(self):
        self.commits += 1

    def sql(self, needle):
        return [sql for sql, _ in self.calls if needle in sql]

    def params_for(self, needle):
        return [p for sql, p in self.calls if needle in sql]

    def index_of(self, needle):
        for i, (sql, _) in enumerate(self.calls):
            if needle in sql:
                return i
        return -1


class _SettingsFakeConn(_FakeConn):
    """Routes save_settings()' four statements and records the audit row."""

    def __init__(self, payload: dict, version_rows=None):
        super().__init__()
        self.payload = payload
        self.version_rows = version_rows or [(1, "seed", "Standard profile", payload)]
        self.versions: list[tuple] = []
        self.audits: list[tuple] = []

    def execute(self, sql, params=None):
        flat = " ".join(sql.split())
        self.calls.append((flat, params))
        if flat.startswith("SELECT id, author, note, payload FROM settings_versions"):
            return _Cursor(self.version_rows)
        if flat.startswith("UPDATE settings_versions SET active"):
            return _Cursor([])
        if "SELECT coalesce(max(id), 0) + 1 FROM settings_versions" in flat:
            return _Cursor([(2,)])
        if flat.startswith("INSERT INTO settings_versions"):
            self.versions.append(params)
            return _Cursor([])
        if flat.startswith("INSERT INTO settings_audits"):
            self.audits.append(params)
            return _Cursor([])
        return _Cursor([])


# ---------------------------------------------------------------------------
# D85: declared Settings field, zero migration
# ---------------------------------------------------------------------------


class TestTriggerLineModel(unittest.TestCase):
    def test_symbol_is_uppercased(self):
        self.assertEqual(_line(" nvda ").symbol, "NVDA")

    def test_bad_symbols_are_rejected(self):
        for raw in ("", "NV DA", "A" * 13, ".NVDA", "NVDA.", "NV..DA"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                TriggerLine(symbol=raw, op="<", level=118)

    def test_op_is_a_literal_not_a_word(self):
        with self.assertRaises(ValueError):
            TriggerLine(symbol="NVDA", op="below", level=118)

    def test_level_must_be_finite_and_positive(self):
        for value in (0, -1, float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                TriggerLine(symbol="NVDA", op="<", level=value)

    def test_declared_field_exists_and_defaults_empty(self):
        self.assertIn("trigger_lines", SettingsPayload.model_fields)
        self.assertEqual(standard_payload().trigger_lines, [])

    def test_payload_is_json_serialisable_for_jsonb(self):
        dumped = _payload([_line()]).model_dump()
        self.assertEqual(json.loads(json.dumps(dumped))["trigger_lines"][0]["symbol"], "NVDA")

    def test_payload_of_keeps_the_declared_field(self):
        # The exact §19.2 trap: _payload_of() clips by model_fields.
        payload = _payload([_line()])
        version = SettingsVersion(id=1, author="seed", note="x", **payload.model_dump())
        self.assertEqual(_payload_of(version).trigger_lines, [_line()])

    def test_old_row_without_trigger_lines_still_loads(self):
        version = SettingsVersion(id=1, author="seed", note="x", **standard_payload().model_dump())
        self.assertEqual(_payload_of(version).trigger_lines, [])

    def test_diff_records_a_new_line(self):
        self.assertIn("trigger_lines", _diff(standard_payload(), _payload([_line()])))

    def test_save_settings_writes_a_version_and_an_audit_row(self):
        conn = _SettingsFakeConn(standard_payload().model_dump())
        new_id, changes = save_settings(conn, "U1", _payload([_line()]),
                                        "console:op", "trigger add")
        self.assertEqual(new_id, 2)
        self.assertIn("trigger_lines", changes)
        self.assertEqual(len(conn.versions), 1)
        self.assertEqual(len(conn.audits), 1)
        self.assertIn("trigger add", conn.audits[0][3])


# ---------------------------------------------------------------------------
# D86: source-aware state machine, grade-only auto_clear
# ---------------------------------------------------------------------------


class TestGradeOnlyReconcile(unittest.TestCase):
    def test_alertrow_source_defaults_to_grade(self):
        self.assertEqual(gv.AlertRow(1, "NVDA", "uncovered_call", "danger", "open").source,
                         "grade")

    def test_trigger_row_is_never_auto_cleared(self):
        row = gv.AlertRow(7, "NVDA", gt.line_key(_line()), "danger", "open", source="trigger")
        plan = gv.reconcile(_report(), [row], NOW)
        self.assertTrue(plan.empty())
        self.assertEqual(plan.auto_clear, ())

    def test_grade_row_is_still_auto_cleared(self):
        row = gv.AlertRow(7, "NVDA", "uncovered_call", "danger", "open")
        plan = gv.reconcile(_report(), [row], NOW)
        self.assertEqual(plan.auto_clear, (7,))

    def test_trigger_and_grade_rows_do_not_share_a_key(self):
        rows = [
            gv.AlertRow(1, "NVDA", "uncovered_call", "danger", "open", source="trigger"),
            gv.AlertRow(2, "NVDA", "uncovered_call", "danger", "open"),
        ]
        plan = gv.reconcile(_report(), rows, NOW)
        self.assertEqual(plan.auto_clear, (2,))

    def test_load_alerts_maps_the_source_column(self):
        conn = _FakeConn(routes={
            "SELECT id, symbol, breach, tier, state, skip_until": [
                (9, "NVDA", "trigger:NVDA:<:118", "danger", "open", None, "trigger"),
            ],
        })
        rows = gv.load_alerts(conn, "U1")
        self.assertEqual(rows[0].source, "trigger")
        self.assertIn("source", conn.sql("FROM guardian_alerts")[0])

    def test_sync_does_not_clear_a_trigger_row(self):
        conn = _FakeConn(routes={
            "SELECT id, symbol, breach, tier, state, skip_until": [
                (9, "NVDA", gt.line_key(_line()), "danger", "open", None, "trigger"),
            ],
        })
        with patch("athena_orchestrator.guardian_view.build_snapshot",
                   return_value=SimpleNamespace(report=_report())):
            _snapshot, alerts = gv.sync(conn, "U1", [], [], standard_payload(), now=NOW)
        self.assertEqual(alerts[0].state, "open")
        self.assertFalse(any("auto_cleared" in sql for sql in conn.sql("UPDATE guardian_alerts")))

    def test_render_board_shows_a_dedicated_trigger_card(self):
        row = gv.AlertRow(5, "NVDA", gt.line_key(_line()), "danger", "open", source="trigger")
        html = gv.render_board(_report(), [row], [])
        self.assertIn("Trigger lines", html)
        self.assertIn("trigger:NVDA:&lt;:118", html)
        self.assertIn('value="resolve"', html)

    def test_render_board_unchanged_without_trigger_rows(self):
        row = gv.AlertRow(5, "NVDA", "uncovered_call", "danger", "open")
        self.assertNotIn("Trigger lines", gv.render_board(_report(), [row], []))

    def test_trigger_card_escapes_dynamic_text(self):
        row = gv.AlertRow(5, "NVDA", "<img onerror=1>", "danger", "open", source="trigger")
        html = gv.render_board(_report(), [row], [])
        self.assertNotIn("<img onerror=1>", html)


# ---------------------------------------------------------------------------
# D88 / D94: pure state judgement
# ---------------------------------------------------------------------------


class TestPureCore(unittest.TestCase):
    def test_level_text_is_stable(self):
        self.assertEqual(gt.level_text(118.0), "118")
        self.assertEqual(gt.level_text(118.5), "118.5")
        self.assertEqual(gt.level_text(0.125), "0.125")

    def test_line_key_is_stable_and_includes_the_level(self):
        self.assertEqual(gt.line_key(_line()), "trigger:NVDA:<:118")
        self.assertNotEqual(gt.line_key(_line()), gt.line_key(_line(level=119)))
        self.assertEqual(gt.line_key(_line(level=118.0)), gt.line_key(_line(level=118)))

    def test_missing_price_is_an_honest_gap_not_false(self):
        self.assertIsNone(gt.crossed(_line(), None))

    def test_below_line_crosses_only_when_strictly_below(self):
        self.assertTrue(gt.crossed(_line(), Decimal("117.50")))
        self.assertFalse(gt.crossed(_line(), Decimal("118.00")))
        self.assertFalse(gt.crossed(_line(), Decimal("119")))

    def test_above_line_crosses_only_when_strictly_above(self):
        line = _line(symbol="MRVL", op=">", level=145)
        self.assertTrue(gt.crossed(line, Decimal("146")))
        self.assertFalse(gt.crossed(line, Decimal("145")))

    def test_comparison_uses_decimal_not_float(self):
        line = _line(level=0.1)
        # float(price) rounds to exactly 0.1, but the Decimal is strictly below.
        price = Decimal("0.099999999999999999")
        self.assertEqual(float(price), line.level)
        self.assertTrue(gt.crossed(line, price))
        src = (ROOT / "apps/orchestrator/src/athena_orchestrator/guardian_trigger.py").read_text(encoding="utf-8")
        self.assertIn("Decimal(str(line.level))", src)


class TestTriggerPlan(unittest.TestCase):
    def test_crossed_line_opens_one_row(self):
        plan = gt.trigger_plan({"U1": [_line()]}, {"NVDA": Decimal("117.50")}, [], NOW)
        self.assertEqual(len(plan.opens), 1)
        item = plan.opens[0]
        self.assertEqual(item.account_id, "U1")
        self.assertEqual(item.symbol, "NVDA")
        self.assertEqual(item.breach, "trigger:NVDA:<:118")
        self.assertIn("117.50", item.reason)
        self.assertIn(NOW.isoformat(), item.reason)

    def test_inside_price_opens_nothing(self):
        plan = gt.trigger_plan({"U1": [_line()]}, {"NVDA": Decimal("119")}, [], NOW)
        self.assertTrue(plan.empty())

    def test_no_price_opens_nothing(self):
        plan = gt.trigger_plan({"U1": [_line()]}, {}, [], NOW)
        self.assertTrue(plan.empty())

    def test_no_price_holds_an_existing_row(self):
        row = gt.TriggerRow(1, "U1", gt.line_key(_line()), "open")
        plan = gt.trigger_plan({"U1": [_line()]}, {}, [row], NOW)
        self.assertTrue(plan.empty())

    def test_unresolved_open_row_blocks_a_second_insert(self):
        row = gt.TriggerRow(1, "U1", gt.line_key(_line()), "open")
        plan = gt.trigger_plan({"U1": [_line()]}, {"NVDA": Decimal("117")}, [row], NOW)
        self.assertEqual(plan.opens, ())
        self.assertEqual(plan.clears, ())

    def test_resolved_row_also_blocks_while_still_crossed(self):
        row = gt.TriggerRow(1, "U1", gt.line_key(_line()), "resolved")
        plan = gt.trigger_plan({"U1": [_line()]}, {"NVDA": Decimal("117")}, [row], NOW)
        self.assertEqual(plan.opens, ())

    def test_price_back_inside_clears_and_rearms(self):
        row = gt.TriggerRow(1, "U1", gt.line_key(_line()), "open")
        plan = gt.trigger_plan({"U1": [_line()]}, {"NVDA": Decimal("119.20")}, [row], NOW)
        self.assertEqual(len(plan.clears), 1)
        self.assertEqual(plan.clears[0][0], 1)
        self.assertEqual(plan.clears[0][1], "U1")
        self.assertIn("back inside", plan.clears[0][2])
        self.assertEqual(plan.opens, ())

    def test_removed_line_clears_its_row(self):
        row = gt.TriggerRow(1, "U1", gt.line_key(_line()), "open")
        plan = gt.trigger_plan({}, {}, [row], NOW)
        self.assertEqual(len(plan.clears), 1)
        self.assertIn("removed or reconfigured", plan.clears[0][2])

    def test_reconfigured_level_clears_the_old_row(self):
        old = gt.TriggerRow(1, "U1", gt.line_key(_line()), "open")
        plan = gt.trigger_plan({"U1": [_line(level=119)]},
                               {"NVDA": Decimal("117")}, [old], NOW)
        self.assertEqual(len(plan.clears), 1)
        self.assertEqual(len(plan.opens), 1)
        self.assertEqual(plan.opens[0].breach, gt.line_key(_line(level=119)))

    def test_expired_skip_reopens(self):
        row = gt.TriggerRow(1, "U1", gt.line_key(_line()), "skipped", NOW - timedelta(minutes=1))
        plan = gt.trigger_plan({"U1": [_line()]}, {"NVDA": Decimal("117")}, [row], NOW)
        self.assertEqual(plan.reopens, ((1, "U1"),))

    def test_active_skip_is_respected(self):
        row = gt.TriggerRow(1, "U1", gt.line_key(_line()), "skipped", NOW + timedelta(hours=1))
        plan = gt.trigger_plan({"U1": [_line()]}, {"NVDA": Decimal("117")}, [row], NOW)
        self.assertEqual(plan.reopens, ())
        self.assertEqual(plan.clears, ())

    def test_accounts_are_isolated_and_opens_sorted(self):
        lines = {"U2": [_line(symbol="MRVL", op=">", level=145)],
                 "U1": [_line()]}
        prices = {"NVDA": Decimal("117"), "MRVL": Decimal("146")}
        plan = gt.trigger_plan(lines, prices, [], NOW)
        self.assertEqual([item.account_id for item in plan.opens], ["U1", "U2"])
        self.assertEqual(len(plan.opens), 2)

    def test_empty_input_is_a_noop(self):
        self.assertTrue(gt.trigger_plan({}, {}, [], NOW).empty())


# ---------------------------------------------------------------------------
# Thin SQL: state-layer uniqueness (D83), account scoping, zero DDL
# ---------------------------------------------------------------------------


class TestThinSql(unittest.TestCase):
    def _crossed_conn(self):
        payload = _payload([_line()]).model_dump()
        return _FakeConn(settings=[("U1", payload)],
                         bars={"NVDA": (NOW, Decimal("117.50"))})

    def test_insert_uses_source_tier_and_state_layer_guard(self):
        conn = self._crossed_conn()
        plan = gt.evaluate_tick(conn, NOW, ["U1"])
        self.assertEqual(len(plan.opens), 1)
        sql = conn.sql("INSERT INTO guardian_alerts")[0]
        self.assertIn("'trigger'", sql)
        self.assertIn("'danger'", sql)
        self.assertIn("WHERE NOT EXISTS", sql)
        self.assertIn("ON CONFLICT DO NOTHING", sql)
        self.assertNotIn("CREATE TABLE", sql)
        params = conn.params_for("INSERT INTO guardian_alerts")[0]
        self.assertEqual(params[0], "U1")
        self.assertEqual(params[1], "NVDA")
        self.assertEqual(params[2], "trigger:NVDA:<:118")
        self.assertEqual(params[6], "U1")

    def test_no_ddl_in_any_statement(self):
        conn = self._crossed_conn()
        gt.evaluate_tick(conn, NOW, ["U1"])
        for sql, _ in conn.calls:
            self.assertNotIn("CREATE TABLE", sql)
            self.assertNotIn("ALTER TABLE", sql)
            self.assertNotIn("DROP TABLE", sql)

    def test_no_accounts_does_no_io(self):
        conn = _FakeConn()
        self.assertTrue(gt.evaluate_tick(conn, NOW, []).empty())
        self.assertEqual(conn.calls, [])

    def test_no_configured_lines_does_not_insert(self):
        conn = _FakeConn()
        self.assertTrue(gt.evaluate_tick(conn, NOW, ["U1"]).empty())
        self.assertEqual(conn.sql("INSERT INTO guardian_alerts"), [])

    def test_clear_is_account_and_source_scoped(self):
        row = gt.TriggerRow(1, "U1", gt.line_key(_line()), "open")
        plan = gt.TriggerPlan(clears=((1, "U1", "reason"),))
        conn = _FakeConn()
        gt.apply_trigger_plan(conn, plan, NOW)
        sql = conn.sql("UPDATE guardian_alerts")[0]
        self.assertIn("source = 'trigger'", sql)
        self.assertIn("state = 'auto_cleared'", sql)
        self.assertIn("account_id = %s", sql)
        self.assertEqual(conn.params_for("UPDATE guardian_alerts")[0][-2:], (1, "U1"))

    def test_reopen_is_skipped_scoped(self):
        plan = gt.TriggerPlan(reopens=((4, "U1"),))
        conn = _FakeConn()
        gt.apply_trigger_plan(conn, plan, NOW)
        sql = conn.sql("UPDATE guardian_alerts")[0]
        self.assertIn("state = 'open'", sql)
        self.assertIn("state = 'skipped'", sql)
        self.assertIn("skip_until = NULL", sql)

    def test_unreadable_settings_payload_is_skipped_not_invented(self):
        conn = _FakeConn(settings=[("U1", "not-json")])
        self.assertTrue(gt.evaluate_tick(conn, NOW, ["U1"]).empty())
        self.assertEqual(conn.sql("INSERT INTO guardian_alerts"), [])

    def test_price_reads_last_tradeable_price_delayed_first(self):
        conn = _FakeConn(bars={"NVDA": (NOW, Decimal("117.50"))},
                         daily={"NVDA": [(NOW, 119, 120, 118, Decimal("119"), 1, "ib-daily")]})
        prices = gt.load_prices(conn, {"U1": [_line()]})
        self.assertEqual(prices["NVDA"], Decimal("117.50"))

    def test_price_falls_back_to_daily_close(self):
        conn = _FakeConn(daily={"NVDA": [(NOW, 119, 120, 118, Decimal("119"), 1, "ib-daily")]})
        prices = gt.load_prices(conn, {"U1": [_line()]})
        self.assertEqual(prices["NVDA"], Decimal("119"))

    def test_load_lines_reads_the_declared_field(self):
        conn = _FakeConn(settings=[("U1", _payload([_line()]).model_dump())])
        self.assertEqual(gt.load_lines(conn, ["U1"]), {"U1": [_line()]})

    def test_load_trigger_rows_maps_columns(self):
        conn = _FakeConn(trigger_rows=[(3, "U1", "trigger:NVDA:<:118", "open", None)])
        rows = gt.load_trigger_rows(conn, ["U1"])
        self.assertEqual(rows, [gt.TriggerRow(3, "U1", "trigger:NVDA:<:118", "open")])


# ---------------------------------------------------------------------------
# D87 / D89: hook into the fixed 300s nag_tick, danger tier
# ---------------------------------------------------------------------------


class TestNagTickHook(unittest.TestCase):
    def test_tier_is_danger_so_due_sql_nags_it(self):
        self.assertEqual(gt.TRIGGER_TIER, "danger")
        self.assertIn("danger", gn.NAG_TIERS)
        self.assertNotIn("source", gn._DUE_SQL)

    def test_interval_and_thread_count_are_unchanged(self):
        self.assertEqual(gn.NAG_INTERVAL, 300)
        src = (ROOT / "apps/orchestrator/src/athena_orchestrator/guardian_nag.py").read_text(encoding="utf-8")
        self.assertEqual(src.count("threading.Thread("), 1)
        self.assertIn("guardian_trigger", src)
        # R4's static guard: the nag module must not own Settings config.
        self.assertNotIn("SettingsPayload", src)
        self.assertNotIn("cycle_minutes", src)

    def test_evaluate_tick_runs_before_due_rows_are_read(self):
        payload = _payload([_line()]).model_dump()
        conn = _FakeConn(
            settings=[("U1", payload)],
            bars={"NVDA": (NOW, Decimal("117.50"))},
            due=[(41, "U1", "NVDA", "trigger:NVDA:<:118", "danger", "open")],
        )
        plan = gn.nag_tick(conn, now=NOW, accounts=["U1"], slots=[], session=False)
        self.assertEqual(plan.triggers, 1)
        self.assertEqual([a for a, _ in plan.nags], [41])
        self.assertLess(conn.index_of("INSERT INTO guardian_alerts"),
                        conn.index_of("WHERE state = 'open'"))

    def test_no_trigger_lines_leaves_r4_tick_untouched(self):
        conn = _FakeConn()
        plan = gn.nag_tick(conn, now=NOW, accounts=["U1"], slots=[], session=False)
        self.assertEqual(plan.triggers, 0)
        self.assertEqual(conn.sql("INSERT INTO guardian_alerts"), [])
        self.assertEqual(plan.nags, ())

    def test_trigger_nag_text_names_the_breach(self):
        text = gn.nag_text(gn.NagRow(41, "U1", "NVDA", "trigger:NVDA:<:118", "danger", "open"))
        self.assertIn("trigger:NVDA:<:118", text)
        self.assertIn("GUARDIAN", text)


# ---------------------------------------------------------------------------
# D90: /settings Trigger lines add/remove
# ---------------------------------------------------------------------------


class TestSettingsTriggerActions(unittest.TestCase):
    def _capture(self, payload, form, action):
        captured = {}

        def fake_save(_conn, _account, saved, actor, note, page=None):
            captured.update(payload=saved, actor=actor, note=note)
            return {}

        with patch.object(pages, "_save_live", fake_save), \
             patch.object(pages, "_settings", return_value={"body": ""}):
            result = pages._post_trigger_line(_FakeConn(), "U1", payload, form, "op", action)
        return result, captured

    def test_add_writes_the_declared_field(self):
        result, captured = self._capture(_payload(), {
            "trigger_symbol": ["nvda"], "trigger_op": ["<"], "trigger_level": ["118"],
        }, "trigger_add")
        self.assertEqual(captured["payload"].trigger_lines, [_line()])
        self.assertIn("trigger add", captured["note"])
        self.assertEqual(captured["actor"], "op")

    def test_add_above_line(self):
        _result, captured = self._capture(_payload(), {
            "trigger_symbol": ["MRVL"], "trigger_op": [">"], "trigger_level": ["145.5"],
        }, "trigger_add")
        self.assertEqual(captured["payload"].trigger_lines,
                         [TriggerLine(symbol="MRVL", op=">", level=145.5)])

    def test_duplicate_line_is_rejected_without_saving(self):
        result, captured = self._capture(_payload([_line()]), {
            "trigger_symbol": ["NVDA"], "trigger_op": ["<"], "trigger_level": ["118"],
        }, "trigger_add")
        self.assertEqual(result["status"], 400)
        self.assertEqual(captured, {})

    def test_invalid_inputs_are_rejected(self):
        for form in (
            {"trigger_symbol": ["NV DA"], "trigger_op": ["<"], "trigger_level": ["118"]},
            {"trigger_symbol": ["NVDA"], "trigger_op": ["below"], "trigger_level": ["118"]},
            {"trigger_symbol": ["NVDA"], "trigger_op": ["<"], "trigger_level": ["nan"]},
            {"trigger_symbol": ["NVDA"], "trigger_op": ["<"], "trigger_level": ["0"]},
            {"trigger_symbol": ["NVDA"], "trigger_op": ["<"], "trigger_level": [""]},
        ):
            with self.subTest(form=form):
                result, captured = self._capture(_payload(), form, "trigger_add")
                self.assertEqual(result["status"], 400)
                self.assertEqual(captured, {})

    def test_remove_by_index(self):
        lines = [_line(), _line(symbol="MRVL", op=">", level=145)]
        result, captured = self._capture(_payload(lines), {"trigger_index": ["0"]},
                                         "trigger_remove")
        self.assertEqual(captured["payload"].trigger_lines, [lines[1]])
        self.assertIn("trigger remove", captured["note"])

    def test_bad_index_is_rejected(self):
        for raw in ("99", "-1", "x", ""):
            with self.subTest(raw=raw):
                result, captured = self._capture(_payload([_line()]),
                                                 {"trigger_index": [raw]}, "trigger_remove")
                self.assertEqual(result["status"], 400)
                self.assertEqual(captured, {})

    def test_trigger_action_is_handled_before_numeric_knobs(self):
        conn = _SettingsFakeConn(standard_payload().model_dump())
        sentinel = {"status": 200, "sentinel": True}
        with patch.object(pages, "_post_trigger_line", return_value=sentinel) as handler:
            result = pages._post_settings(conn, "U1", {"action": ["trigger_add"]}, "op")
        self.assertIs(result, sentinel)
        self.assertEqual(handler.call_args[0][5], "trigger_add")
        self.assertEqual(conn.sql("INSERT INTO settings_versions"), [])

    def test_trigger_lines_card_has_add_and_remove_forms(self):
        html = pages._trigger_lines_card(_payload([_line()]))
        self.assertIn("Trigger lines", html)
        self.assertIn("NVDA", html)
        self.assertIn("118", html)
        self.assertIn('value="trigger_add"', html)
        self.assertIn('value="trigger_remove"', html)
        self.assertIn('name="trigger_symbol"', html)

    def test_trigger_lines_card_is_empty_state_safe(self):
        html = pages._trigger_lines_card(_payload())
        self.assertIn("No trigger lines yet.", html)

    def test_settings_page_includes_the_trigger_card(self):
        src = (ROOT / "apps/orchestrator/src/athena_orchestrator/pages.py").read_text(encoding="utf-8")
        self.assertIn("_trigger_lines_card(current)", src)
        self.assertIn('"/settings"', src)


# ---------------------------------------------------------------------------
# Boundaries: D84 / D91 / D94 / zero migration / D66 note
# ---------------------------------------------------------------------------


class TestBoundaries(unittest.TestCase):
    def test_r5_adds_no_migration_and_055_is_r7s(self):
        # R5 added no migration; 055 is R7's regime_labels (D69 / D104).
        folder = ROOT / "packages/store/src/athena_store/migrations"
        self.assertEqual(sorted(p.name for p in folder.glob("05*.sql")),
                         ["050_cycle_progress.sql", "051_advisor_signals.sql",
                          "052_order_events.sql", "053_guardian_alerts.sql",
                          "054_guardian_runs.sql", "055_regime_labels.sql"])
        self.assertEqual([p for p in folder.glob("056*")], [])

    def test_force_include_registry_has_no_056(self):
        # R5 added no registry entry; R7 adds 055 (D104). 056 must stay absent.
        text = (ROOT / "packages/store/pyproject.toml").read_text(encoding="utf-8")
        self.assertIn("054_guardian_runs.sql", text)
        self.assertIn("055_regime_labels.sql", text)
        self.assertNotIn("056_", text)

    def test_last_tradeable_price_is_the_only_price_reader(self):
        src = (ROOT / "apps/orchestrator/src/athena_orchestrator/guardian_trigger.py").read_text(encoding="utf-8")
        self.assertIn("last_tradeable_price", src)
        self.assertNotIn("upsert_delayed", src)
        self.assertNotIn("quote_pair", src)
        self.assertNotIn("reqMktData", src)
        self.assertNotIn("pendingTickersEvent", src)

    def test_no_new_data_source_and_the_vix_carve_out_is_narrow(self):
        src = (ROOT / "apps/orchestrator/src/athena_orchestrator/guardian_trigger.py").read_text(encoding="utf-8")
        self.assertNotIn("pulse", src)
        self.assertNotIn("IND", src)
        broker = (ROOT / "apps/exec-ib/src/athena_exec/broker.py").read_text(encoding="utf-8")
        # D108 (13th round) is the second ratified section 6 carve-out: one
        # manual, on-demand VIX sync may construct a single Index contract. It
        # is NOT a producer - no subscription, no streaming, no universe entry -
        # so the assertion narrows to "exactly one, inside index_history".
        self.assertEqual(broker.count("Index("), 1)
        self.assertIn("def index_history", broker)
        body = broker.split("def index_history", 1)[1].split("\n    def ", 1)[0]
        self.assertIn("Index(", body)
        # History only: no quote line, no streaming subscription for the index.
        self.assertNotIn("reqMktData", body)
        self.assertNotIn("marketDataType", body)
        self.assertIn("historical(", body)

    def test_due_sql_does_not_filter_source(self):
        self.assertNotIn("source", gn._DUE_SQL)
        self.assertIn("tier IN ('danger', 'blowup')", gn._DUE_SQL)

    def test_r4_and_r2_sources_are_still_registered(self):
        src = (ROOT / "apps/orchestrator/src/athena_orchestrator/guardian_nag.py").read_text(encoding="utf-8")
        self.assertIn("'trigger'", src)
        self.assertEqual(gn.DEADMAN_SOURCE, "deadman")

    def test_d66_note_records_the_settings_field_scope(self):
        booklet = (ROOT / "docs/plan/14-guardian-escalation.md").read_text(encoding="utf-8")
        self.assertIn("D85", booklet)
        self.assertIn("trigger_lines", booklet)


if __name__ == "__main__":
    unittest.main()
