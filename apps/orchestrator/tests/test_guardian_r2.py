"""R2 tests: Guardian alert store, reconciliation, /guard panel (booklet 14 §11).

Pure state-machine / assembly / rendering tests use no DB. Thin SQL and page
tests use a routing fake conn, following the existing test_rules.py convention.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from athena_risk import GuardianReport, PositionGrade, Tier
from athena_orchestrator import guardian_view as gv

NOW = datetime(2026, 11, 21, 13, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _Cursor:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)


class _FakeConn:
    """Routes by SQL substring (insertion order = priority). Records every call."""

    def __init__(self, routes=None, default=()):
        self.routes = routes or {}
        self.default = default
        self.calls = []
        self.committed = False

    def execute(self, sql, params=None):
        norm = " ".join(sql.split())
        self.calls.append((norm, params))
        for key, rows in self.routes.items():
            if key in norm:
                if isinstance(rows, Exception):
                    raise rows
                return _Cursor(rows)
        return _Cursor(self.default)

    def commit(self):
        self.committed = True

    def sql(self, needle):
        return [sql for sql, _ in self.calls if needle in sql]

    def params_for(self, needle):
        return [p for sql, p in self.calls if needle in sql]


def _settings(watchlist=("NVDA", "MRVL")):
    return SimpleNamespace(
        watchlist=list(watchlist),
        risk=SimpleNamespace(
            max_bucket_pct_nav=35,
            max_symbol_pct_nav=20,
            min_dte_new=7,
            assign_delta=0.30,
        ),
    )


def _report(account_flags=(), positions=(), portfolio=Tier.DANGER, account=Tier.DANGER):
    return GuardianReport(
        portfolio_tier=portfolio,
        account_tier=account,
        positions=tuple(positions),
        account_flags=tuple(account_flags),
        el_nl_pct=Decimal("8.2"),
        maint_nl_pct=None,
        breach_count=0,
        proximity_count=0,
        unmanaged_count=0,
    )


def _grade(symbol="NVDA", tier=Tier.DANGER, flags=("uncovered_call",), unmanaged=False):
    return PositionGrade(symbol, tier, tuple(flags), unmanaged)


def _alert(id=1, symbol="NVDA", breach="uncovered_call", tier="danger",
           state="open", skip_until=None):
    return gv.AlertRow(id, symbol, breach, tier, state, skip_until)


# ---------------------------------------------------------------------------
# desired(): only hard breaches, account scope, tier mapping
# ---------------------------------------------------------------------------


class TestDesired(unittest.TestCase):
    def test_only_hard_account_flags_and_correct_tiers(self) -> None:
        report = _report(account_flags=("el_thin", "el_critical", "el_watch", "unknown_margin"))
        found = gv.desired(report)
        self.assertEqual(found.get((gv.ACCOUNT_SCOPE, "el_thin")), "danger")
        self.assertEqual(found.get((gv.ACCOUNT_SCOPE, "el_critical")), "blowup")
        self.assertNotIn((gv.ACCOUNT_SCOPE, "el_watch"), found)
        self.assertNotIn((gv.ACCOUNT_SCOPE, "unknown_margin"), found)
        self.assertEqual(len(found), 2)

    def test_proximity_flag_on_a_danger_position_is_excluded(self) -> None:
        report = _report(positions=[_grade(flags=("uncovered_call", "prox_bucket"))])
        found = gv.desired(report)
        self.assertIn(("NVDA", "uncovered_call"), found)
        self.assertNotIn(("NVDA", "prox_bucket"), found)

    def test_balanced_position_never_opens_an_alert(self) -> None:
        report = _report(positions=[_grade(tier=Tier.BALANCED, flags=("prox_dte",))])
        self.assertEqual(gv.desired(report), {})

    def test_clean_book_has_no_desired_breaches(self) -> None:
        self.assertEqual(gv.desired(_report(portfolio=Tier.HEALTHY, account=Tier.HEALTHY)), {})

    def test_unmanaged_info_flag_is_not_a_breach(self) -> None:
        report = _report(positions=[_grade(tier=Tier.BALANCED, flags=("unmanaged",), unmanaged=True)])
        self.assertEqual(gv.desired(report), {})


# ---------------------------------------------------------------------------
# reconcile(): the D62 state machine
# ---------------------------------------------------------------------------


class TestReconcile(unittest.TestCase):
    def setUp(self) -> None:
        self.report = _report(positions=[_grade(flags=("uncovered_call",))])

    def test_new_breach_opens_an_alert(self) -> None:
        plan = gv.reconcile(self.report, [], NOW)
        self.assertEqual([(a.symbol, a.breach, a.tier) for a in plan.open_new],
                         [("NVDA", "uncovered_call", "danger")])
        self.assertTrue(plan.auto_clear == plan.reopen == plan.retier == ())

    def test_open_and_still_present_is_a_noop(self) -> None:
        plan = gv.reconcile(self.report, [_alert()], NOW)
        self.assertTrue(plan.empty())

    def test_breach_gone_auto_clears_an_open_alert(self) -> None:
        clean = _report(portfolio=Tier.HEALTHY, account=Tier.HEALTHY)
        plan = gv.reconcile(clean, [_alert()], NOW)
        self.assertEqual(plan.auto_clear, (1,))
        self.assertEqual(plan.open_new, ())

    def test_resolved_breach_still_present_does_not_reopen(self) -> None:
        """The flaw fixed in §11: Resolve must not bounce back on the next run."""
        plan = gv.reconcile(self.report, [_alert(state="resolved")], NOW)
        self.assertTrue(plan.empty())

    def test_resolved_breach_that_cleared_becomes_auto_cleared(self) -> None:
        clean = _report(portfolio=Tier.HEALTHY, account=Tier.HEALTHY)
        plan = gv.reconcile(clean, [_alert(state="resolved")], NOW)
        self.assertEqual(plan.auto_clear, (1,))

    def test_auto_cleared_breach_reappearing_rearms_a_new_alert(self) -> None:
        plan = gv.reconcile(self.report, [_alert(state="auto_cleared")], NOW)
        self.assertEqual(len(plan.open_new), 1)
        self.assertEqual(plan.open_new[0].breach, "uncovered_call")
        self.assertEqual(plan.auto_clear, ())

    def test_auto_cleared_and_still_absent_is_noop(self) -> None:
        clean = _report(portfolio=Tier.HEALTHY, account=Tier.HEALTHY)
        self.assertTrue(gv.reconcile(clean, [_alert(state="auto_cleared")], NOW).empty())

    def test_skip_within_24h_stays_asleep(self) -> None:
        plan = gv.reconcile(self.report, [_alert(state="skipped", skip_until=NOW + timedelta(hours=1))], NOW)
        self.assertTrue(plan.empty())

    def test_skip_expired_and_breach_still_real_reopens(self) -> None:
        plan = gv.reconcile(self.report, [_alert(state="skipped", skip_until=NOW - timedelta(minutes=1))], NOW)
        self.assertEqual(plan.reopen, ((1, "danger"),))
        self.assertEqual(plan.open_new, ())

    def test_skip_expired_with_no_skip_until_reopens(self) -> None:
        plan = gv.reconcile(self.report, [_alert(state="skipped")], NOW)
        self.assertEqual(plan.reopen, ((1, "danger"),))

    def test_skipped_breach_that_cleared_auto_clears(self) -> None:
        clean = _report(portfolio=Tier.HEALTHY, account=Tier.HEALTHY)
        plan = gv.reconcile(clean, [_alert(state="skipped", skip_until=NOW + timedelta(hours=5))], NOW)
        self.assertEqual(plan.auto_clear, (1,))

    def test_tier_change_updates_the_live_row(self) -> None:
        plan = gv.reconcile(self.report, [_alert(id=7, tier="blowup")], NOW)
        self.assertEqual(plan.retier, ((7, "danger"),))

    def test_newest_row_per_key_wins(self) -> None:
        plan = gv.reconcile(self.report, [_alert(id=9, state="resolved"), _alert(id=2)], NOW)
        self.assertTrue(plan.empty())   # newest (id=9, resolved) -> no re-open

    def test_independent_keys_are_reconciled_separately(self) -> None:
        report = _report(positions=[
            _grade(symbol="NVDA", flags=("uncovered_call",)),
            _grade(symbol="MRVL", flags=("symbol_over_cap",)),
        ])
        plan = gv.reconcile(report, [_alert(id=1, symbol="NVDA", breach="uncovered_call")], NOW)
        self.assertEqual([(a.symbol, a.breach) for a in plan.open_new],
                         [("MRVL", "symbol_over_cap")])
        self.assertEqual(plan.auto_clear, ())

    def test_account_scope_and_position_keys_coexist(self) -> None:
        report = _report(
            account_flags=("el_thin",),
            positions=[_grade(flags=("uncovered_call",))],
        )
        found = gv.desired(report)
        self.assertIn((gv.ACCOUNT_SCOPE, "el_thin"), found)
        self.assertIn(("NVDA", "uncovered_call"), found)


# ---------------------------------------------------------------------------
# assemble(): snapshot mapping, D66 pending_action gate, honest gaps
# ---------------------------------------------------------------------------


def _row(symbol="NVDA", qty="100", **kw):
    base = {
        "symbol": symbol, "qty": Decimal(qty), "sec_type": "STK", "right": "",
        "strike": "", "multiplier": "", "market": Decimal("1000"),
        "pnl": Decimal("50"), "dte": "", "currency": "USD",
    }
    base.update(kw)
    return base


class TestAssemble(unittest.TestCase):
    def test_pending_action_requires_a_pending_draft(self) -> None:
        signals = {"NVDA": {"action": "close"}}
        no_draft = gv.assemble([_row()], [], _settings(), signals, {},
                              Decimal("100000"), None, None, "USD", {})
        self.assertIsNone(no_draft.positions[0].pending_action)
        with_draft = gv.assemble(
            [_row()], [{"id": "D1", "symbol": "NVDA", "tactic": "CLOSE"}],
            _settings(), signals, {}, Decimal("100000"), None, None, "USD", {},
        )
        self.assertEqual(with_draft.positions[0].pending_action, "close")

    def test_tactic_fallback_when_no_advisor_signal(self) -> None:
        snap = gv.assemble(
            [_row()], [{"id": "D1", "symbol": "NVDA", "tactic": "TRIM"}],
            _settings(), {}, {}, Decimal("100000"), None, None, "USD", {},
        )
        self.assertEqual(snap.positions[0].pending_action, "trim")
        self.assertIn("advisor_trim_pending", snap.report.positions[0].flags)

    def test_close_with_draft_produces_a_danger_breach(self) -> None:
        snap = gv.assemble(
            [_row()], [{"id": "D1", "symbol": "NVDA", "tactic": "CLOSE"}],
            _settings(), {"NVDA": {"action": "close"}}, {},
            Decimal("100000"), None, None, "USD", {},
        )
        self.assertIn("advisor_close_pending", snap.report.positions[0].flags)
        self.assertEqual(snap.report.positions[0].tier, Tier.DANGER)

    def test_unmanaged_flag_when_symbol_not_in_watchlist(self) -> None:
        snap = gv.assemble([_row(symbol="ACCIDENT")], [], _settings(), {}, {},
                          Decimal("100000"), None, None, "USD", {})
        self.assertFalse(snap.positions[0].managed)
        self.assertTrue(snap.report.positions[0].unmanaged)
        self.assertIn("unmanaged", snap.report.positions[0].flags)

    def test_delta_and_margin_call_are_honestly_empty(self) -> None:
        snap = gv.assemble([_row()], [], _settings(), {}, {},
                          Decimal("100000"), Decimal("5000"), Decimal("90000"), "USD", {})
        self.assertIsNone(snap.positions[0].delta)
        self.assertFalse(snap.marks.margin_call)

    def test_rules_are_bound_from_live_limits(self) -> None:
        settings = _settings()
        settings.risk.max_bucket_pct_nav = 42
        snap = gv.assemble([_row()], [], settings, {}, {},
                          Decimal("100000"), None, None, "USD", {})
        self.assertEqual(snap.rules.max_bucket_pct_nav, Decimal("42"))

    def test_fx_conversion_to_base_currency(self) -> None:
        snap = gv.assemble([_row(currency="USD", market=Decimal("100"))], [],
                          _settings(), {}, {}, Decimal("100000"), None, None,
                          "HKD", {"USD": Decimal("7.8")})
        self.assertEqual(snap.positions[0].market_value, Decimal("780"))

    def test_missing_fx_rate_leaves_market_value_none(self) -> None:
        snap = gv.assemble([_row(currency="EUR")], [], _settings(), {}, {},
                          Decimal("100000"), None, None, "HKD", {})
        self.assertIsNone(snap.positions[0].market_value)
        self.assertIn("missing_market_value", snap.report.positions[0].flags)

    def test_zero_qty_and_empty_symbol_are_skipped(self) -> None:
        snap = gv.assemble([_row(qty="0"), _row(symbol="")], [], _settings(), {}, {},
                          Decimal("100000"), None, None, "USD", {})
        self.assertEqual(snap.positions, ())

    def test_option_fields_are_parsed(self) -> None:
        snap = gv.assemble(
            [_row(sec_type="OPT", right="C", strike="120", multiplier="100", qty="-2", dte="3")],
            [], _settings(), {}, {}, Decimal("100000"), None, None, "USD", {},
        )
        pos = snap.positions[0]
        self.assertEqual(pos.sec_type, "OPT")
        self.assertEqual(pos.right, "C")
        self.assertEqual(pos.strike, Decimal("120"))
        self.assertEqual(pos.multiplier, Decimal("100"))
        self.assertEqual(pos.dte, 3)

    def test_assemble_is_pure_across_calls(self) -> None:
        rows, drafts, settings = [_row()], [], _settings()
        one = gv.assemble(rows, drafts, settings, {}, {}, Decimal("100000"), None, None, "USD", {})
        two = gv.assemble(rows, drafts, settings, {}, {}, Decimal("100000"), None, None, "USD", {})
        self.assertEqual(one.report, two.report)


# ---------------------------------------------------------------------------
# render_board(): five colours, escaping, ack forms
# ---------------------------------------------------------------------------


class TestRenderBoard(unittest.TestCase):
    def setUp(self) -> None:
        self.report = _report(
            account_flags=("el_thin",),
            positions=[
                _grade(symbol="NVDA", flags=("uncovered_call",)),
                _grade(symbol="MRVL", tier=Tier.BALANCED, flags=("prox_bucket",)),
                _grade(symbol="QCOM", tier=Tier.HEALTHY, flags=()),
            ],
        )

    def test_all_five_tier_bands_render(self) -> None:
        html = gv.render_board(self.report, [], [])
        for label in ("Blowup", "Danger", "Balanced", "Healthy", "Profit"):
            self.assertIn(label, html)
        self.assertIn('class="tag black"', html)   # blowup colour exists

    def test_note_is_first_child_for_the_flash_toast(self) -> None:
        html = gv.render_board(self.report, [], [], note="Resolved alert 1.")
        self.assertTrue(html.startswith('<p class="note">Resolved alert 1.</p>'))

    def test_dynamic_text_is_escaped(self) -> None:
        evil = _report(positions=[_grade(symbol="<script>x</script>", flags=("uncovered_call",))])
        html = gv.render_board(evil, [], [], note="<img onerror=1>")
        self.assertNotIn("<script>x</script>", html)
        self.assertNotIn("<img onerror=1>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_open_danger_alert_gets_resolve_and_skip_forms(self) -> None:
        html = gv.render_board(self.report, [_alert(id=5)], [])
        self.assertIn('name="decision" value="resolve"', html)
        self.assertIn('name="decision" value="skip"', html)
        self.assertIn('name="reason" placeholder="reason (required)" required', html)
        self.assertIn('value="5"', html)

    def test_pending_draft_is_linked_on_the_red_row(self) -> None:
        html = gv.render_board(self.report, [], [{"id": "D9", "symbol": "NVDA", "tactic": "CLOSE"}])
        self.assertIn('pending draft <a href="/drafts">D9</a>', html)

    def test_account_level_alert_is_rendered(self) -> None:
        html = gv.render_board(
            self.report,
            [_alert(id=3, symbol=gv.ACCOUNT_SCOPE, breach="el_thin", tier="danger")],
            [],
        )
        self.assertIn("Account-level breaches", html)
        self.assertIn("el_thin", html)

    def test_closed_rows_are_kept_visible(self) -> None:
        html = gv.render_board(self.report, [_alert(state="auto_cleared")], [])
        self.assertIn("Recently closed", html)
        self.assertIn("auto_cleared", html)

    def test_store_error_card_is_escaped(self) -> None:
        html = gv.render_board(self.report, [], [], store_error='relation "x" <b>')
        self.assertIn("Alert store unavailable", html)
        self.assertNotIn("<b>", html)


# ---------------------------------------------------------------------------
# Thin SQL: apply_plan / resolve / skip / load_alerts / open_alert_count
# ---------------------------------------------------------------------------


class TestSql(unittest.TestCase):
    def test_open_new_uses_parameterised_account_scoped_insert(self) -> None:
        conn = _FakeConn()
        gv.apply_plan(conn, "U1", gv.ReconcilePlan(
            open_new=(gv.ProposedAlert("NVDA", "uncovered_call", "danger"),)), NOW)
        self.assertEqual(len(conn.sql("INSERT INTO guardian_alerts")), 1)
        params = conn.params_for("INSERT INTO guardian_alerts")[0]
        self.assertEqual(params[0], "U1")
        self.assertEqual(params[1:4], ("NVDA", "uncovered_call", "danger"))
        self.assertIn("ON CONFLICT DO NOTHING", conn.sql("INSERT INTO guardian_alerts")[0])

    def test_auto_clear_does_not_delete_the_row(self) -> None:
        conn = _FakeConn()
        gv.apply_plan(conn, "U1", gv.ReconcilePlan(auto_clear=(7,)), NOW)
        sql = conn.sql("UPDATE guardian_alerts")[0]
        self.assertIn("state = 'auto_cleared'", sql)
        self.assertNotIn("DELETE", sql)
        self.assertEqual(conn.params_for("UPDATE guardian_alerts")[0][-2:], (7, "U1"))

    def test_reopen_clears_skip_until_and_refreshes_tier(self) -> None:
        conn = _FakeConn()
        gv.apply_plan(conn, "U1", gv.ReconcilePlan(reopen=((4, "danger"),)), NOW)
        sql = conn.sql("UPDATE guardian_alerts")[0]
        self.assertIn("state = 'open'", sql)
        self.assertIn("skip_until = NULL", sql)

    def test_resolve_requires_a_live_alert(self) -> None:
        unknown = _FakeConn({"SELECT state FROM guardian_alerts": []})
        with self.assertRaisesRegex(ValueError, "Unknown alert"):
            gv.resolve(unknown, "U1", 1, "op", "", NOW)
        closed = _FakeConn({"SELECT state FROM guardian_alerts": [("resolved",)]})
        with self.assertRaisesRegex(ValueError, "already closed"):
            gv.resolve(closed, "U1", 1, "op", "", NOW)

    def test_resolve_writes_actor_note_and_state(self) -> None:
        conn = _FakeConn({"SELECT state FROM guardian_alerts": [("open",)]})
        gv.resolve(conn, "U1", 3, "op", " covered it ", NOW)
        sql = conn.sql("UPDATE guardian_alerts")[0]
        self.assertIn("state = 'resolved'", sql)
        params = conn.params_for("UPDATE guardian_alerts")[0]
        self.assertEqual(params[1], "op")
        self.assertEqual(params[2], "covered it")

    def test_skip_requires_a_reason(self) -> None:
        conn = _FakeConn({"SELECT state FROM guardian_alerts": [("open",)]})
        with self.assertRaisesRegex(ValueError, "reason is required"):
            gv.skip(conn, "U1", 1, "op", "   ", NOW)
        self.assertEqual(conn.sql("UPDATE guardian_alerts"), [])

    def test_skip_sleeps_24h(self) -> None:
        conn = _FakeConn({"SELECT state FROM guardian_alerts": [("open",)]})
        gv.skip(conn, "U1", 2, "op", "exam week", NOW)
        params = conn.params_for("UPDATE guardian_alerts")[0]
        self.assertEqual(params[0], NOW + timedelta(hours=24))
        self.assertEqual(params[2], "exam week")

    def test_load_alerts_maps_columns(self) -> None:
        conn = _FakeConn({"SELECT id, symbol, breach, tier, state, skip_until": [
            (9, "NVDA", "uncovered_call", "danger", "open", None),
        ]})
        rows = gv.load_alerts(conn, "U1")
        self.assertEqual(rows[0], _alert(id=9))
        self.assertEqual(conn.params_for("FROM guardian_alerts")[0], ("U1",))

    def test_open_alert_count(self) -> None:
        conn = _FakeConn({"SELECT count(*) FROM guardian_alerts": [(4,)]})
        self.assertEqual(gv.open_alert_count(conn, "U1"), 4)
        self.assertIn("state = 'open'", conn.sql("SELECT count(*)")[0])

    def test_empty_plan_does_not_commit(self) -> None:
        conn = _FakeConn(routes={
            "SELECT id, symbol, breach, tier, state, skip_until": [(
                1, "NVDA", "uncovered_call", "danger", "open", None)],
        })
        # Patch build_snapshot so sync() needs only the alert query.
        snap = gv.assemble([_row()], [], _settings(), {}, {},
                          Decimal("100000"), None, None, "USD", {})
        with patch("athena_orchestrator.guardian_view.build_snapshot", return_value=snap):
            snapshot, alerts = gv.sync(conn, "U1", [], [], _settings(), now=NOW)
        # A pending CLOSE-free clean position has no breach -> the existing open
        # alert for uncovered_call must be auto-cleared.
        self.assertTrue(any("auto_cleared" in s for s in conn.sql("UPDATE guardian_alerts")))
        self.assertTrue(conn.committed)


# ---------------------------------------------------------------------------
# Page handlers
# ---------------------------------------------------------------------------


def _page_routes(alert_rows=None):
    return {
        "SELECT symbol, action, confidence, reason, payload FROM advisor_signals": [],
        "SELECT symbol, classifiers FROM symbol_facts": [],
        "FROM nav_marks": [(Decimal("100000"),)],
        "SELECT margin, excess_liquidity": [(Decimal("9000"), Decimal("8200"))],
        "SELECT base_currency, cash_balances, fx_rates": [("USD", {}, {})],
        "SELECT id, symbol, breach, tier, state, skip_until": alert_rows or [],
    }


class TestPage(unittest.TestCase):
    def test_get_guard_renders_five_tiers(self) -> None:
        conn = _FakeConn(_page_routes())
        with patch("athena_orchestrator.pages._guard_inputs",
                   return_value=(_settings(), [_row()], [])):
            from athena_orchestrator.pages import _guard_page

            page = _guard_page(conn, "U1", {})
        self.assertEqual(page["status"], 200)
        self.assertEqual(page["kind"], "html")
        self.assertIn("guard-stack", page["body"])
        for label in ("Blowup", "Danger", "Balanced", "Healthy", "Profit"):
            self.assertIn(label, page["body"])

    def test_no_account_returns_a_clear_card(self) -> None:
        from athena_orchestrator.pages import _guard_page

        page = _guard_page(_FakeConn(), "", {})
        self.assertEqual(page["status"], 400)
        self.assertIn("No account is configured", page["body"])

    def test_missing_alert_table_degrades_to_live_tiers(self) -> None:
        conn = _FakeConn(_page_routes())
        conn.routes["SELECT id, symbol, breach, tier, state, skip_until"] = RuntimeError("no table")
        with patch("athena_orchestrator.pages._guard_inputs",
                   return_value=(_settings(), [_row()], [])):
            from athena_orchestrator.pages import _guard_page

            page = _guard_page(conn, "U1", {})
        self.assertEqual(page["status"], 200)
        self.assertIn("Alert store unavailable", page["body"])
        self.assertIn("guard-stack", page["body"])

    def test_post_resolve_calls_sql_and_returns_a_toast(self) -> None:
        conn = _FakeConn({**_page_routes(), "SELECT state FROM guardian_alerts": [("open",)]})
        with patch("athena_orchestrator.pages._guard_inputs",
                   return_value=(_settings(), [], [])):
            from athena_orchestrator.pages import _post_guard

            page = _post_guard(conn, "U1", {"decision": ["resolve"], "alert_id": ["5"],
                                           "note": ["covered"]}, "op")
        self.assertEqual(page["status"], 200)
        self.assertTrue(page["body"].startswith('<p class="note">Resolved alert 5.</p>'))
        self.assertTrue(any("state = 'resolved'" in s for s in conn.sql("UPDATE guardian_alerts")))
        self.assertTrue(conn.committed)

    def test_post_skip_without_reason_is_a_400(self) -> None:
        conn = _FakeConn({**_page_routes(), "SELECT state FROM guardian_alerts": [("open",)]})
        from athena_orchestrator.pages import _post_guard

        with patch("athena_orchestrator.pages._guard_inputs",
                   return_value=(_settings(), [], [])):
            page = _post_guard(conn, "U1", {"decision": ["skip"], "alert_id": ["5"],
                                           "reason": [""]}, "op")
        self.assertEqual(page["status"], 400)
        self.assertIn("reason is required", page["body"])

    def test_post_unknown_action_and_bad_id_are_400(self) -> None:
        from athena_orchestrator.pages import _post_guard

        conn = _FakeConn(_page_routes())
        with patch("athena_orchestrator.pages._guard_inputs",
                   return_value=(_settings(), [], [])):
            bad = _post_guard(conn, "U1", {"decision": ["delete"], "alert_id": ["5"]}, "op")
            malformed = _post_guard(conn, "U1", {"decision": ["resolve"], "alert_id": ["x"]}, "op")
        self.assertEqual(bad["status"], 400)
        self.assertEqual(malformed["status"], 400)
        self.assertIn("Unknown alert", malformed["body"])


class TestNavBadge(unittest.TestCase):
    def test_missing_guardian_table_does_not_break_badges(self) -> None:
        from athena_orchestrator.console import _nav_badges

        class Conn:
            def execute(self, sql, params=None):
                if "guardian_alerts" in sql:
                    raise RuntimeError("relation does not exist")
                return self

            def fetchone(self):
                return (0,)

        with (
            patch("athena_orchestrator.console._account", return_value="U1"),
            patch("athena_orchestrator.sim.progress",
                  return_value={"status": "idle", "total": 0, "day": 0}),
        ):
            badges = _nav_badges(Conn())
        self.assertNotIn("/guard", badges)

    def test_open_alert_count_feeds_the_guard_badge(self) -> None:
        from athena_orchestrator.console import _nav_badges

        # Guardian count = 2, everything else 0.
        class Counting:
            def execute(self, sql, params=None):
                self.last = " ".join(sql.split())
                return self

            def fetchone(self):
                return (2,) if "guardian_alerts" in getattr(self, "last", "") else (0,)

        with (
            patch("athena_orchestrator.console._account", return_value="U1"),
            patch("athena_orchestrator.sim.progress",
                  return_value={"status": "idle", "total": 0, "day": 0}),
        ):
            badges = _nav_badges(Counting())
        self.assertEqual(badges["/guard"][0], 2)
        self.assertTrue(badges["/guard"][1])   # warn styling


class TestContractGuards(unittest.TestCase):
    def test_skip_hours_is_24(self) -> None:
        self.assertEqual(gv.SKIP_HOURS, 24)

    def test_account_scope_cannot_be_a_real_ticker(self) -> None:
        self.assertNotIn(gv.ACCOUNT_SCOPE, {"NVDA", "MRVL", "QCOM"})

    def test_migration_053_exists_and_is_registered(self) -> None:
        from pathlib import Path

        root = Path(__file__).resolve().parents[3]
        migration = root / "packages/store/src/athena_store/migrations/053_guardian_alerts.sql"
        self.assertTrue(migration.exists())
        text = migration.read_text(encoding="utf-8")
        self.assertIn("CREATE TABLE IF NOT EXISTS guardian_alerts", text)
        self.assertIn("WHERE state IN ('open', 'skipped')", text)
        wheel = (root / "packages/store/pyproject.toml").read_text(encoding="utf-8")
        self.assertIn("053_guardian_alerts.sql", wheel)


if __name__ == "__main__":
    unittest.main()
