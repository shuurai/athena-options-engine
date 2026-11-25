"""§17 / V2-P1: static, read-only Iris + Orchestration independent columns.

P1 is a truthful architecture view, not a second trading brain. This suite
pins the contract in ``docs/plan/15-athena-v2-iris.md`` §17:

* canonical ``/iris`` with a backward-compatible ``/portfolio-analysis`` alias;
* one nav entry highlighted for both paths;
* a versioned static graph with stable node / edge ids and honest statuses;
* no animation, SSE, polling, event stream or new action ledger;
* a single action rail reusing ``/drafts`` with ``insufficient_provenance``;
* Run C and Guardian as read-only side bands, never a strategy;
* Orchestration as two independent columns, Trade last in the right column;
* zero writes, zero migration, zero settings change.

No engine restart, no live console, no PostgreSQL writes.
"""

from __future__ import annotations

import re
import unittest
from contextlib import ExitStack
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

from athena_orchestrator import console, guardian_regime as gr, iris_view, pages
from athena_orchestrator import guardian_runner as runs
from athena_settings.model import standard_payload
from athena_settings.prompts import AGENT_ORDER, TRADE_NAME

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "apps/orchestrator/src/athena_orchestrator"
PAGES_SRC = SRC / "pages.py"
CONSOLE_SRC = SRC / "console.py"
IRIS_SRC = SRC / "iris_view.py"
RUNNER_SRC = SRC / "guardian_runner.py"
CJK = re.compile(r"[\u3400-\u9fff]")

_EXISTING_TABLES = frozenset(
    {
        "market_bars",
        "news_events",
        "calendar_marks",
        "iv_marks",
        "symbol_phases",
        "advisor_signals",
        "regime_labels",
        "drafts",
        "approvals",
        "orders",
        "fills",
        "guardian_alerts",
        "positions",
    }
)


class _Cursor:
    def __init__(self, rows=(), one=None):
        self.rows = list(rows)
        self.one = one

    def fetchall(self):
        return list(self.rows)

    def fetchone(self):
        if self.one is not None:
            return self.one
        return self.rows[0] if self.rows else None


class _SpyConn:
    """Records every statement. ``commit``/``rollback`` are counted, never run."""

    def __init__(self, rows=(), one=None, one_map=None):
        self.rows = list(rows)
        self.one = one
        self.one_map = one_map or {}
        self.calls: list[tuple[str, object]] = []
        self.commits = 0
        self.rollbacks = 0

    def execute(self, sql, params=None):
        flat = " ".join(sql.split())
        self.calls.append((flat, params))
        if "settings_versions" in flat:
            return _Cursor([], (1, "seed", "x", standard_payload().model_dump()))
        for needle, value in self.one_map.items():
            if needle in flat:
                return _Cursor(self.rows, value)
        return _Cursor(self.rows, self.one)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def _regime_row():
    return {
        "trade_day": date(2026, 11, 24),
        "run": "C",
        "label": "insufficient_data",
        "close_bias": "insufficient_data",
        "evidence": {},
        "inputs": {"vix_slope": "flat", "vix_hits": 1},
        "created_at": datetime(2026, 11, 24, 17, 30, tzinfo=timezone.utc),
        "is_real": False,
    }


def _render_iris(*, drafts=None, conn=None, snap=None):
    """Render the real page. Only the LLM/strategy-ish seams are stubbed."""
    conn = conn if conn is not None else _SpyConn()
    with ExitStack() as stack:
        stack.enter_context(patch("athena_strategy.advisor.today_signals", return_value={}))
        stack.enter_context(patch.object(gr, "latest_label", return_value=_regime_row()))
        if drafts is not None:
            stack.enter_context(patch.object(pages, "_pending_drafts", return_value=list(drafts)))
        if snap is not None:
            stack.enter_context(patch.object(iris_view, "snapshot", return_value=snap))
        built = pages._iris_page(conn, "U1", {})
    return conn, built


def _render_strategy():
    return pages._strategy(_SpyConn(), "U1", {})["body"]


def _render_layout(path: str) -> str:
    return console._layout(path, "Iris", "<p>probe</p>", [], conn=_SpyConn())


def _agent_names_after(body: str, marker: str) -> list[str]:
    tail = body[body.index(marker) :]
    return re.findall(r'data-agent-name="([^"]+)"', tail)


class TestIrisRoutesAndNav(unittest.TestCase):
    def test_build_dispatches_canonical_iris(self):
        with (
            patch.object(pages, "_account", return_value="U1"),
            patch("athena_strategy.advisor.today_signals", return_value={}),
            patch.object(gr, "latest_label", return_value=_regime_row()),
        ):
            built = pages.build(_SpyConn(), "/iris", {})
        self.assertIsNotNone(built)
        self.assertEqual(built["title"], "Iris")

    def test_build_alias_renders_the_same_iris_page(self):
        with (
            patch.object(pages, "_account", return_value="U1"),
            patch("athena_strategy.advisor.today_signals", return_value={}),
            patch.object(gr, "latest_label", return_value=_regime_row()),
        ):
            canonical = pages.build(_SpyConn(), "/iris", {})
            alias = pages.build(_SpyConn(), "/portfolio-analysis", {})
        self.assertEqual(canonical, alias)

    def test_unknown_route_is_still_none(self):
        with patch.object(pages, "_account", return_value="U1"):
            self.assertIsNone(pages.build(_SpyConn(), "/iris-does-not-exist", {}))

    def test_no_account_still_renders_iris_title(self):
        built = pages._iris_page(_SpyConn(), "", {})
        self.assertEqual(built["title"], "Iris")
        self.assertIn("No account is configured.", built["body"])

    def test_nav_has_one_iris_entry_and_drops_old_label(self):
        core = dict(console.NAV_GROUPS)["Core logic"]
        self.assertIn(("/iris", "Iris"), core)
        self.assertNotIn(("/portfolio-analysis", "Portfolio Analysis"), core)
        self.assertEqual([h for h, _ in core].count("/iris"), 1)

    def test_nav_active_for_canonical_path(self):
        self.assertIn('class="active" href="/iris">Iris</a>', _render_layout("/iris"))

    def test_nav_active_for_alias_path(self):
        html = _render_layout("/portfolio-analysis")
        self.assertIn('class="active" href="/iris">Iris</a>', html)
        self.assertNotIn('href="/portfolio-analysis"', html)

    def test_nav_not_active_for_unrelated_path(self):
        html = _render_layout("/drafts")
        self.assertIn('<a href="/iris">Iris</a>', html)
        self.assertNotIn('class="active" href="/iris"', html)

    def test_alias_set_covers_both_paths(self):
        self.assertEqual(console.IRIS_ALIASES, frozenset({"/iris", "/portfolio-analysis"}))


class TestIrisStaticGraph(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _, cls.built = _render_iris()
        cls.body = cls.built["body"]
        cls.graph = iris_view.render_graph(iris_view.snapshot(_SpyConn(), "U1", standard_payload()))

    def test_graph_version_is_stable_and_exposed(self):
        self.assertEqual(iris_view.GRAPH_VERSION, "iris-p1-static-v1")
        self.assertIn('data-graph-version="iris-p1-static-v1"', self.body)
        self.assertEqual(self.body.count("iris-p1-static-v1"), 2)

    def test_every_registered_node_has_a_stable_id_and_status(self):
        for node in iris_view.NODES:
            with self.subTest(node=node.node_id):
                self.assertIn(f'id="node-{node.node_id}"', self.graph)
                self.assertIn(f'data-node-id="{node.node_id}"', self.graph)
                self.assertIn(f'data-column="{node.column}"', self.graph)
                self.assertIn('data-graph-role="node"', self.graph)
        self.assertEqual(self.graph.count('data-graph-role="node"'), len(iris_view.NODES))

    def test_every_registered_edge_has_stable_endpoints(self):
        for edge in iris_view.EDGES:
            with self.subTest(edge=edge.edge_id):
                self.assertIn(f'id="edge-{edge.edge_id}"', self.graph)
                self.assertIn(f'data-edge-id="{edge.edge_id}"', self.graph)
                self.assertIn(f'data-source="{edge.source}" data-target="{edge.target}"', self.graph)
                self.assertIn(f'data-flow="{edge.flow}"', self.graph)
        self.assertEqual(self.graph.count('data-graph-role="edge"'), len(iris_view.EDGES))

    def test_all_edge_endpoints_reference_real_nodes(self):
        ids = {n.node_id for n in iris_view.NODES}
        for edge in iris_view.EDGES:
            self.assertIn(edge.source, ids)
            self.assertIn(edge.target, ids)
        self.assertEqual(len({e.edge_id for e in iris_view.EDGES}), len(iris_view.EDGES))

    def test_shadow_and_direct_edges_are_visually_dashed(self):
        for edge in iris_view.EDGES:
            if edge.flow in {"shadow", "direct"}:
                marker = f'id="edge-{edge.edge_id}"'
                chunk = self.graph[self.graph.index(marker) : self.graph.index(marker) + 400]
                self.assertIn('stroke-dasharray="6 5"', chunk)

    def test_graph_is_accessible_svg(self):
        self.assertIn('role="img"', self.graph)
        self.assertIn('aria-label="Iris static architecture graph"', self.graph)
        self.assertIn('tabindex="0"', self.graph)
        self.assertIn("<title>", self.graph)
        self.assertIn("<svg", self.graph)

    def test_no_animation_no_event_stream_in_rendered_page(self):
        forbidden = (
            "@keyframes",
            "animation:",
            "requestAnimationFrame",
            "EventSource",
            "WebSocket",
            "setInterval",
            "addEventListener",
            "<script",
            "fetch(",
            "text/event-stream",
        )
        for token in forbidden:
            with self.subTest(token=token):
                self.assertNotIn(token, self.body)

    def test_node_states_are_honest_for_empty_data(self):
        snap = iris_view.snapshot(_SpyConn(), "U1", standard_payload())
        states = iris_view._node_states(snap)
        self.assertEqual(states["news"][0], "no_data")
        self.assertEqual(states["daily_bars"][0], "no_data")
        self.assertEqual(states["vix"][0], "no_data")
        self.assertEqual(states["news_sentiment"][0], "unknown")
        self.assertEqual(states["price_location"][0], "unknown")
        self.assertEqual(states["realized_vol"][0], "unknown")
        self.assertEqual(states["premium_ratio"][0], "unknown")
        self.assertEqual(states["weekly_sma"][0], "shadow")
        self.assertEqual(states["strategy_3"][0], "shadow")
        self.assertEqual(states["strategy_1"][0], "configured")
        self.assertEqual(states["strategy_2"][0], "configured")
        self.assertEqual(states["drafts"][0], "idle")

    def test_no_factor_claims_ready_without_run_evidence(self):
        snap = iris_view.snapshot(_SpyConn(), "U1", standard_payload())
        self.assertEqual(snap["run_evidence"], "unavailable")
        self.assertEqual(snap["daily_gap_state"], "unknown")
        graph = iris_view.render_graph(snap)
        self.assertNotIn('data-status="ready" data-column="factor"', graph)

    def test_graph_copy_is_english(self):
        self.assertIsNone(CJK.search(self.graph))
        self.assertIn("No line is animated", self.graph)

    def test_header_states_no_live_events_and_daily_gap_is_unknown(self):
        header = iris_view.render_header(iris_view.snapshot(_SpyConn(), "U1", standard_payload()))
        self.assertIn("Read-only static graph", header)
        self.assertIn("No live events or connector animation in P1", header)
        self.assertIn("internal gaps are unknown", header)
        self.assertIn('data-iris-static="true"', header)

    def test_css_explicitly_disables_iris_animation_for_reduced_motion(self):
        css = CONSOLE_SRC.read_text()
        self.assertIn("@media (prefers-reduced-motion: reduce)", css)
        self.assertIn(".iris-graph * { animation: none !important; transition: none !important; }", css)

    def test_css_says_no_connector_animation_in_p1(self):
        self.assertIn("No connector animation in this phase", CONSOLE_SRC.read_text())


class TestIrisReadOnlySnapshot(unittest.TestCase):
    def test_like_wildcard_is_escaped_for_psycopg(self):
        """A bare ``LIKE 'VIX%'`` is parsed as a placeholder and crashes on real PG."""
        conn = _SpyConn()
        iris_view.snapshot(conn, "U1", standard_payload())
        like_sql = [sql for sql, _ in conn.calls if "LIKE" in sql]
        self.assertEqual(len(like_sql), 1)
        self.assertIn("LIKE 'VIX%%'", like_sql[0])
        self.assertNotIn("LIKE 'VIX%'", like_sql[0])
        stripped = like_sql[0].replace("%%", "").replace("%s", "")
        self.assertNotIn("%", stripped)

    def test_snapshot_issues_only_select_statements(self):
        conn = _SpyConn()
        iris_view.snapshot(conn, "U1", standard_payload())
        self.assertTrue(conn.calls)
        for sql, _params in conn.calls:
            self.assertTrue(sql.lstrip().upper().startswith("SELECT"), sql[:80])
            for verb in ("INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "DROP", "COMMIT"):
                self.assertNotIn(verb, sql.upper())

    def test_snapshot_never_commits_or_rolls_back(self):
        conn = _SpyConn()
        iris_view.snapshot(conn, "U1", standard_payload())
        self.assertEqual(conn.commits, 0)
        self.assertEqual(conn.rollbacks, 0)

    def test_snapshot_reads_only_existing_tables(self):
        conn = _SpyConn()
        iris_view.snapshot(conn, "U1", standard_payload())
        tables = set()
        for sql, _ in conn.calls:
            tables.update(re.findall(r"(?i)\bFROM\s+([a-z_][a-z0-9_]*)", sql))
        self.assertTrue(tables)
        self.assertTrue(tables <= _EXISTING_TABLES, tables - _EXISTING_TABLES)
        self.assertNotIn("strategy_events", tables)
        self.assertNotIn("iris_actions", tables)

    def test_snapshot_scopes_account_specific_queries(self):
        conn = _SpyConn()
        iris_view.snapshot(conn, "U1", standard_payload())
        scoped = [params for sql, params in conn.calls if "account_id = %s" in sql]
        self.assertTrue(scoped)
        for params in scoped:
            self.assertEqual(params, ("U1",))

    def test_snapshot_returns_explicit_unknowns_not_fabricated_values(self):
        snap = iris_view.snapshot(_SpyConn(), "U1", standard_payload())
        self.assertEqual(snap["daily_gap_state"], "unknown")
        self.assertEqual(snap["run_evidence"], "unavailable")
        self.assertEqual(snap["daily_first"], "unknown")
        self.assertEqual(snap["daily_last"], "unknown")
        self.assertEqual(snap["news_last"], "unknown")
        self.assertEqual(snap["graph_version"], iris_view.GRAPH_VERSION)

    def test_snapshot_counts_and_stringifies_stamps(self):
        conn = _SpyConn(
            one=(7,),
            one_map={
                "min(ts::date)": ("2026-11-01 00:00:00",),
                "max(ts::date)": ("2026-11-24 00:00:00",),
                "max(ts)": ("2026-11-24 12:30:00",),
            },
        )
        snap = iris_view.snapshot(conn, "U1", standard_payload())
        self.assertEqual(snap["daily_bars"], 7)
        self.assertEqual(snap["news_rows"], 7)
        self.assertEqual(snap["daily_first"], "2026-11-01 00:00:00")
        self.assertEqual(snap["daily_last"], "2026-11-24 00:00:00")
        self.assertEqual(snap["news_last"], "2026-11-24 12:30:00")

    def test_iris_module_source_has_no_writes_or_streams(self):
        text = IRIS_SRC.read_text()
        for token in (".commit(", "INSERT ", "UPDATE ", "DELETE FROM", "CREATE TABLE",
                      "EventSource", "WebSocket", "requestAnimationFrame", "setInterval"):
            with self.subTest(token=token):
                self.assertNotIn(token, text)

    def test_iris_page_render_never_writes(self):
        conn, built = _render_iris()
        self.assertEqual(conn.commits, 0)
        self.assertEqual(conn.rollbacks, 0)
        for sql, _ in conn.calls:
            self.assertTrue(sql.lstrip().upper().startswith("SELECT"), sql[:80])
        self.assertIn("Iris snapshot", built["body"])


class TestIrisActionRail(unittest.TestCase):
    def test_draft_rail_links_back_to_existing_drafts(self):
        drafts = [
            {"id": "D-1", "symbol": "NVDA", "tactic": "BUY", "reason": "cheap"},
            {"id": "D-2", "symbol": "INTC", "tactic": "OPEN_CSP", "reason": ""},
        ]
        _, built = _render_iris(drafts=drafts)
        body = built["body"]
        for d in drafts:
            self.assertIn(f'<a href="/drafts">{d["id"]}</a>', body)
        self.assertIn('<a href="/drafts">/drafts</a>', body)

    def test_legacy_drafts_never_get_guessed_lineage(self):
        drafts = [
            {"id": "D-1", "symbol": "NVDA", "tactic": "BUY", "reason": "r1"},
            {"id": "D-2", "symbol": "INTC", "tactic": "OPEN_CSP", "reason": "r2"},
        ]
        _, built = _render_iris(drafts=drafts)
        body = built["body"]
        self.assertEqual(body.count("insufficient_provenance"), len(drafts))
        self.assertIn("lineage is never guessed", body)

    def test_lineage_is_not_inferred_from_tactic_names(self):
        rail = iris_view.render_draft_rail(
            [{"id": "D-9", "symbol": "NVDA", "tactic": "V2-S1 NFR Wheel", "reason": ""}]
        )
        lineage = rail[rail.index("<th>Lineage</th>") : rail.index("<th>Reason</th>")]
        self.assertNotIn("V2-S1", lineage)
        self.assertIn("insufficient_provenance", rail)

    def test_empty_rail_shows_no_draft_waiting(self):
        rail = iris_view.render_draft_rail([])
        self.assertIn("No draft is waiting.", rail)
        self.assertNotIn("insufficient_provenance", rail)

    def test_dynamic_reason_is_escaped(self):
        rail = iris_view.render_draft_rail(
            [{"id": "<img>", "symbol": "N&V", "tactic": "BUY", "reason": "<script>alert(1)</script>"}]
        )
        self.assertNotIn("<script>", rail)
        self.assertIn("&lt;script&gt;", rail)
        self.assertIn("&lt;img&gt;", rail)
        self.assertIn("N&amp;V", rail)

    def test_iris_page_has_exactly_one_action_rail(self):
        _, built = _render_iris()
        self.assertEqual(built["body"].count("iris-draft-rail"), 1)

    def test_old_advisor_actions_table_is_retired_from_page(self):
        _, built = _render_iris()
        body = built["body"]
        self.assertNotIn("Advisor actions", body)
        self.assertNotIn("settings-cols analysis", body)
        self.assertIn("Strategy 2 evidence", body)
        self.assertIn("actions stay in the single draft rail", body)

    def test_advisor_report_remains_as_evidence_only(self):
        _, built = _render_iris()
        self.assertIn("<h2>Advisor report</h2>", built["body"])
        self.assertIn('<h2>Action rail · existing drafts</h2>', built["body"])


class TestIrisGuardianSideBand(unittest.TestCase):
    def test_run_c_card_is_still_rendered(self):
        _, built = _render_iris()
        self.assertIn("Midday Regime · Run C", built["body"])
        self.assertIn("read-only observation, never a gate", built["body"])

    def test_guardian_band_is_not_a_strategy(self):
        band = iris_view.render_guardian_band(
            iris_view.snapshot(_SpyConn(), "U1", standard_payload())
        )
        self.assertIn("Guardian observes; it is not a strategy and creates no drafts", band)
        self.assertIn("read-only", band)
        self.assertNotIn("<form", band)
        self.assertNotIn("action=", band)

    def test_guardian_is_not_drawn_as_strategy_4(self):
        self.assertFalse(any(n.column == "strategy" and "Guardian" in n.label for n in iris_view.NODES))
        self.assertFalse(any("guardian" in n.node_id for n in iris_view.NODES))

    def test_operator_direct_paths_are_labelled_not_strategy_approved(self):
        direct = [n for n in iris_view.NODES if n.column == "direct"]
        self.assertEqual([n.node_id for n in direct], ["operator_direct"])
        self.assertIn("Operator direct paths", direct[0].label)
        self.assertTrue(any(e.flow == "direct" for e in iris_view.EDGES))

    def test_iris_lead_and_chips_mark_static_p1(self):
        _, built = _render_iris()
        self.assertIn("No live animation in P1", built["lead"])
        self.assertIn(("info", "STATIC P1"), built["chips"])

    def test_iris_page_is_english(self):
        _, built = _render_iris()
        text = built["title"] + built["lead"] + built["body"] + " ".join(
            t for _tone, t in built["chips"]
        )
        self.assertIsNone(CJK.search(text))

    def test_iris_page_structural_order(self):
        _, built = _render_iris()
        body = built["body"]
        order = [
            'class="iris-page"',
            'class="card iris-header"',
            'class="iris-grid"',
            "Architecture · static",
            "Strategy 2 evidence",
            "Guardian side band",
            "Midday Regime · Run C",
            'class="card fill iris-draft-rail"',
        ]
        positions = [body.index(token) for token in order]
        self.assertEqual(positions, sorted(positions))


class TestOrchestrationColumns(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.body = _render_strategy()

    def test_independent_columns_replace_the_shared_grid(self):
        self.assertIn('class="agent-columns"', self.body)
        self.assertIn('data-agent-column="left"', self.body)
        self.assertIn('data-agent-column="right"', self.body)
        self.assertNotIn('class="agent-grid"', self.body)
        self.assertNotIn(".agent-grid", CONSOLE_SRC.read_text())

    def test_trade_card_is_in_the_right_column(self):
        right_at = self.body.index('data-agent-column="right"')
        left_at = self.body.index('data-agent-column="left"')
        trade_at = self.body.index('data-agent-name="Trade"')
        self.assertLess(left_at, right_at)
        self.assertGreater(trade_at, right_at)

    def test_trade_is_the_last_card_in_the_right_column(self):
        names = _agent_names_after(self.body, 'data-agent-column="right"')
        self.assertTrue(names)
        self.assertEqual(names[-1], TRADE_NAME)
        self.assertEqual(names.count(TRADE_NAME), 1)

    def test_right_column_holds_the_expected_agents(self):
        names = _agent_names_after(self.body, 'data-agent-column="right"')
        self.assertEqual(set(names), set(pages._ORCH_RIGHT_AGENTS))
        self.assertNotIn("Consolidator", names)

    def test_left_and_right_sets_partition_all_agent_cards(self):
        body = self.body
        left_at = body.index('data-agent-column="left"')
        right_at = body.index('data-agent-column="right"')
        left = set(re.findall(r'data-agent-name="([^"]+)"', body[left_at:right_at]))
        right = set(_agent_names_after(body, 'data-agent-column="right"'))
        self.assertFalse(left & right)
        self.assertEqual(left | right, (set(AGENT_ORDER) - {"Consolidator"}) | {TRADE_NAME})

    def test_every_agent_card_carries_a_mobile_order(self):
        for name in list(AGENT_ORDER) + [TRADE_NAME]:
            with self.subTest(name=name):
                self.assertIn(f'data-agent-name="{name}"', self.body)
        self.assertEqual(self.body.count("data-agent-order="), len(AGENT_ORDER) + 1)
        self.assertEqual(self.body.count("--agent-order:"), len(AGENT_ORDER) + 1)

    def test_mobile_order_is_sequential_and_matches_agent_order(self):
        expected = tuple(n for n in AGENT_ORDER if n != "Consolidator") + (TRADE_NAME,)
        self.assertEqual(pages._ORCH_MOBILE_AGENT_ORDER, expected)
        self.assertEqual(len(set(pages._ORCH_MOBILE_AGENT_ORDER)), len(expected))
        for i, name in enumerate(expected, start=1):
            marker = f'data-agent-name="{name}"'
            chunk = self.body[self.body.index(marker) : self.body.index(marker) + 200]
            self.assertIn(f'data-agent-order="{i}"', chunk)
            self.assertIn(f'style="--agent-order:{i}"', chunk)

    def test_post_action_and_hidden_agent_fields_are_unchanged(self):
        forms = re.findall(r"<form[^>]*>", self.body)
        agent_forms = [f for f in forms if 'action="/orchestration"' in f]
        self.assertEqual(len(agent_forms), len(AGENT_ORDER) + 1)
        for form in agent_forms:
            self.assertIn('method="post"', form)
            self.assertIn('action="/orchestration"', form)
        hidden = re.findall(r'<input type="hidden" name="agent" value="([^"]+)">', self.body)
        self.assertEqual(set(hidden), set(AGENT_ORDER) | {TRADE_NAME})

    def test_css_has_independent_stacks_and_mobile_ordering(self):
        css = CONSOLE_SRC.read_text()
        self.assertIn(".agent-columns { display: grid;", css)
        self.assertIn("minmax(0, 1fr) minmax(0, 1fr)", css)
        self.assertIn(".agent-col { min-width: 0; display: flex; flex-direction: column; gap: 14px; }", css)
        self.assertIn("order: var(--agent-order, 99)", css)
        self.assertIn(".agent-col { display: contents; }", css)

    def test_trade_card_helper_accepts_an_explicit_order(self):
        from types import SimpleNamespace

        prompt = SimpleNamespace(system="sys", execution="exec", reasoning=False)
        card = pages._trade_prompt_card(prompt, 10)
        self.assertIn(f'data-agent-name="{TRADE_NAME}"', card)
        self.assertIn('data-agent-order="10"', card)
        self.assertIn('--agent-order:10', card)

    def test_iris_css_and_orchestration_css_coexist(self):
        css = CONSOLE_SRC.read_text()
        self.assertIn(".iris-page", css)
        self.assertIn(".agent-columns", css)


class TestCopySynchronisation(unittest.TestCase):
    def test_scheduled_runs_footnote_points_at_iris(self):
        html = runs.render_run_history([{"trade_day": "2026-07-01", "run": "A"}])
        self.assertIn("Run C is the midday regime observation on Iris.", html)
        self.assertNotIn("on Portfolio Analysis.", html)

    def test_guardian_regime_docstring_points_at_iris(self):
        self.assertIn("for Iris", gr.render_card.__doc__)
        self.assertNotIn("/portfolio-analysis", gr.render_card.__doc__)

    def test_pages_source_has_no_stale_portfolio_analysis_title(self):
        text = PAGES_SRC.read_text()
        self.assertNotIn('"Portfolio Analysis"', text)
        self.assertIn('"Iris"', text)

    def test_route_map_points_both_paths_at_iris_page(self):
        text = PAGES_SRC.read_text()
        self.assertIn('"/iris": _iris_page,', text)
        self.assertIn('"/portfolio-analysis": _iris_page,', text)

    def test_alias_helper_is_a_thin_backward_compatible_wrapper(self):
        body = PAGES_SRC.read_text().split("def _portfolio_analysis_page", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("Backward-compatible alias", body)
        self.assertIn("return _iris_page(conn, account, query)", body)
        self.assertNotIn("_regime_card", body)

    def test_iris_page_owns_the_regime_card(self):
        body = PAGES_SRC.read_text().split("def _iris_page", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("_regime_card(conn, account)", body)
        self.assertIn('f"{regime}"', body)


if __name__ == "__main__":
    unittest.main()
