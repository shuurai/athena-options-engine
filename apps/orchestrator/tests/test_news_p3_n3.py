"""§6.3 N3 / §26 — canonical news read model (V2-P3 batch 3).

This batch is a **dark launch**. The read model and all 17 reader wrappers are
complete and verified, but ``news_read.SOURCE`` ships as ``LEGACY`` because
§26.2's two data-plane preconditions are not met: §25's six-item gate was a
transaction-internal dry-run (ROLLBACK), so the live canonical tables are still
empty, and §26.4 forbids starting the engine to observe runtime N2 dual-write
convergence. Flipping the switch against empty tables would silently blank
every news reader. Activation is one line plus a persisted backfill and needs
an explicit operator go/no-go.

Pinned here:

* the single process-wide switch and the per-reader ``source=`` cutback;
* ``render()`` is byte-identical on the legacy path (zero behaviour change);
* the canonical CTE shadows ``news_events`` with exactly the nine legacy
  columns, joining map -> association on ``association_id``;
* R1: ``ts`` is ``association.effective_ts`` (a real timestamptz), so
  ``guardian_regime`` keeps bucketing by ``America/New_York``; ``published_day``
  (UTC, identity-only) is NOT exposed to readers;
* R2: only ``news_score`` gets the deterministic ``ORDER BY ts, sentiment``
  tie-break, and only on the canonical path; ``ORDER BY ts DESC`` is untouched;
* all 17 non-test readers are wrapped; the three writers / DSH UPDATE /
  dismiss UPDATE are not;
* no new migration (53), and ``news_read.py`` is auto-included in the wheel.
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path
from unittest.mock import patch

from athena_store import news_read

ROOT = Path(__file__).resolve().parents[3]
STORE = ROOT / "packages/store/src/athena_store"
MIG_DIR = STORE / "migrations"
READ_SRC = STORE / "news_read.py"

READER_FILES = {
    "apps/bots/src/athena_bots/telegram.py": 1,
    "apps/orchestrator/src/athena_orchestrator/console.py": 1,
    "apps/orchestrator/src/athena_orchestrator/pages.py": 7,
    "apps/orchestrator/src/athena_orchestrator/iris_view.py": 2,
    "apps/orchestrator/src/athena_orchestrator/guardian_regime.py": 1,
    "packages/strategy-nfr/src/athena_strategy/advisor.py": 1,
    "packages/strategy-nfr/src/athena_strategy/agents.py": 2,
    "packages/strategy-nfr/src/athena_strategy/indicators.py": 1,
    "packages/strategy-nfr/src/athena_strategy/news.py": 1,
}
EXPECTED_SITES = sum(READER_FILES.values())

_SQL_WITH_ORDER_BY_TS = """
SELECT sentiment FROM news_events
WHERE symbol = %s AND ts >= %s AND dismissed = false AND sentiment IS NOT NULL
ORDER BY ts
"""
_SQL_WITH_ORDER_BY_TS_DESC = """
SELECT ts, source, title FROM news_events
WHERE symbol = %s AND dismissed = false
ORDER BY ts DESC
LIMIT 8
"""


def _literals(path: Path) -> list[ast.Constant]:
    """All string constants passed to news_read.render/score_sql in a file."""
    tree = ast.parse(path.read_text())
    out: list[ast.Constant] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        fn = node.func
        if not (isinstance(fn.value, ast.Name) and fn.value.id == "news_read"):
            continue
        if fn.attr not in {"render", "score_sql"} or not node.args:
            continue
        arg = node.args[0]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            out.append(arg)
    return out


def _all_literals() -> list[tuple[str, ast.Constant]]:
    return [
        (rel, lit)
        for rel in READER_FILES
        for lit in _literals(ROOT / rel)
    ]


class TestDarkLaunchSwitch(unittest.TestCase):
    def test_source_ships_as_legacy(self):
        """The critical safety contract: do not cut over to empty tables."""
        self.assertEqual(news_read.SOURCE, news_read.LEGACY)

    def test_constants_are_stable(self):
        self.assertEqual(news_read.CANONICAL, "canonical")
        self.assertEqual(news_read.LEGACY, "legacy")

    def test_is_canonical_default_false(self):
        self.assertFalse(news_read.is_canonical())

    def test_is_canonical_honours_override(self):
        self.assertTrue(news_read.is_canonical(news_read.CANONICAL))
        self.assertFalse(news_read.is_canonical(news_read.LEGACY))

    def test_module_docstring_records_the_gate(self):
        doc = news_read.__doc__ or ""
        self.assertIn("persisted", doc)
        self.assertIn("DARK LAUNCH", doc)


class TestRenderContract(unittest.TestCase):
    def test_legacy_path_is_byte_identical(self):
        sql = "SELECT count(*) FROM news_events WHERE NOT dismissed"
        self.assertEqual(news_read.render(sql), sql)

    def test_canonical_path_is_cte_plus_unchanged_body(self):
        sql = "SELECT count(*) FROM news_events WHERE NOT dismissed"
        self.assertEqual(
            news_read.render(sql, source=news_read.CANONICAL),
            news_read.CANONICAL_CTE + sql,
        )

    def test_explicit_legacy_override_wins_over_global(self):
        sql = "SELECT 1 FROM news_events"
        with patch.object(news_read, "SOURCE", news_read.CANONICAL):
            self.assertEqual(news_read.render(sql, source=news_read.LEGACY), sql)

    def test_explicit_canonical_override_wins_over_global(self):
        sql = "SELECT 1 FROM news_events"
        self.assertEqual(
            news_read.render(sql, source=news_read.CANONICAL),
            news_read.CANONICAL_CTE + sql,
        )

    def test_unknown_source_is_rejected(self):
        with self.assertRaises(ValueError):
            news_read.render("SELECT 1 FROM news_events", source="bogus")

    def test_patching_global_switch_changes_every_reader(self):
        sql = "SELECT 1 FROM news_events"
        with patch.object(news_read, "SOURCE", news_read.CANONICAL):
            self.assertTrue(news_read.render(sql).startswith("WITH news_events AS ("))
        self.assertEqual(news_read.render(sql), sql)

    def test_render_does_not_apply_stable_order(self):
        rendered = news_read.render(_SQL_WITH_ORDER_BY_TS, source=news_read.CANONICAL)
        self.assertIn("ORDER BY ts\n", rendered)
        self.assertNotIn("ORDER BY ts, sentiment", rendered)

    def test_every_canonical_render_is_a_select_body(self):
        for rel, lit in _all_literals():
            with self.subTest(site=rel):
                rendered = news_read.render(lit.value, source=news_read.CANONICAL)
                self.assertTrue(rendered.startswith(news_read.CANONICAL_CTE))
                body = rendered[len(news_read.CANONICAL_CTE):]
                self.assertTrue(body.lstrip().upper().startswith("SELECT"), body[:60])


class TestScoreSqlR2(unittest.TestCase):
    def test_canonical_gets_deterministic_tiebreak(self):
        rendered = news_read.score_sql(_SQL_WITH_ORDER_BY_TS, source=news_read.CANONICAL)
        self.assertIn("ORDER BY ts, sentiment", rendered)
        self.assertNotIn("ORDER BY ts\n", rendered)

    def test_legacy_path_is_untouched(self):
        self.assertEqual(news_read.score_sql(_SQL_WITH_ORDER_BY_TS), _SQL_WITH_ORDER_BY_TS)

    def test_legacy_override_is_untouched_even_when_global_is_canonical(self):
        with patch.object(news_read, "SOURCE", news_read.CANONICAL):
            self.assertEqual(
                news_read.score_sql(_SQL_WITH_ORDER_BY_TS, source=news_read.LEGACY),
                _SQL_WITH_ORDER_BY_TS,
            )

    def test_order_by_ts_desc_is_never_rewritten(self):
        rendered = news_read.score_sql(_SQL_WITH_ORDER_BY_TS_DESC, source=news_read.CANONICAL)
        self.assertIn("ORDER BY ts DESC", rendered)
        self.assertNotIn("ORDER BY ts, sentiment", rendered)

    def test_only_the_first_order_by_is_rewritten(self):
        sql = "SELECT 1 FROM news_events ORDER BY ts\n-- ORDER BY ts\n"
        rendered = news_read.score_sql(sql, source=news_read.CANONICAL)
        self.assertEqual(rendered.count("ORDER BY ts, sentiment"), 1)
        self.assertIn("-- ORDER BY ts", rendered)

    def test_score_sql_is_a_thin_wrapper_over_render(self):
        self.assertEqual(
            news_read.score_sql(_SQL_WITH_ORDER_BY_TS, source=news_read.CANONICAL),
            news_read.render(
                _SQL_WITH_ORDER_BY_TS, stable_order=True, source=news_read.CANONICAL
            ),
        )


class TestCanonicalCTEShape(unittest.TestCase):
    def test_cte_name_shadows_legacy_table(self):
        self.assertTrue(news_read.CANONICAL_CTE.startswith("WITH news_events AS ("))

    def test_cte_is_read_only(self):
        upper = news_read.CANONICAL_CTE.upper()
        for verb in ("INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "DROP", "TRUNCATE"):
            with self.subTest(verb=verb):
                self.assertNotIn(verb, upper)

    def test_cte_exposes_exactly_the_nine_legacy_columns(self):
        # Only the projection list, before the first FROM; table aliases
        # (AS m / AS s) are not output columns.
        projection = news_read.CANONICAL_CTE.split("FROM", 1)[0]
        columns = re.findall(r"\bAS\s+(\w+)\s*,?\s*$", projection, re.M)
        stripped = [c.strip() for c in columns]
        self.assertEqual(tuple(stripped), news_read.LEGACY_COLUMNS)

    def test_legacy_columns_match_reader_contract(self):
        self.assertEqual(
            news_read.LEGACY_COLUMNS,
            (
                "fingerprint", "ts", "symbol", "source", "event_type",
                "sentiment", "title", "url", "dismissed",
            ),
        )

    def test_r1_ts_comes_from_effective_ts(self):
        self.assertIn("s.effective_ts       AS ts", news_read.CANONICAL_CTE)

    def test_r1_published_day_is_not_exposed(self):
        self.assertNotIn("published_day", news_read.CANONICAL_CTE)

    def test_immutable_fields_come_from_the_map(self):
        self.assertIn("m.legacy_fingerprint AS fingerprint", news_read.CANONICAL_CTE)
        self.assertIn("m.title              AS title", news_read.CANONICAL_CTE)

    def test_mutable_fields_come_from_the_association(self):
        for col in ("source", "event_type", "sentiment", "url", "dismissed"):
            with self.subTest(column=col):
                self.assertRegex(news_read.CANONICAL_CTE, rf"s\.{col}\s+AS {col}")

    def test_join_is_map_to_association_on_primary_key(self):
        self.assertIn("FROM news_legacy_event_map AS m", news_read.CANONICAL_CTE)
        self.assertIn("JOIN news_article_symbols AS s", news_read.CANONICAL_CTE)
        self.assertIn("s.association_id = m.association_id", news_read.CANONICAL_CTE)

    def test_cte_does_not_reference_news_events_table(self):
        # Only the CTE *name* may be news_events; no physical table read.
        self.assertNotIn("FROM news_events", news_read.CANONICAL_CTE)
        self.assertNotIn("JOIN news_events", news_read.CANONICAL_CTE)

    def test_read_module_has_no_connection_or_write(self):
        text = READ_SRC.read_text()
        for token in ("connect(", "commit(", "rollback(", "INSERT INTO", "UPDATE ", "DELETE FROM"):
            with self.subTest(token=token):
                self.assertNotIn(token, text)


class TestReaderWiring(unittest.TestCase):
    def test_expected_site_count(self):
        self.assertEqual(EXPECTED_SITES, 17)

    def test_all_sites_are_wrapped(self):
        found = {}
        for rel, expected in READER_FILES.items():
            found[rel] = len(_literals(ROOT / rel))
        # guardian_regime passes _NEWS_SQL by name, not as an inline literal.
        found["apps/orchestrator/src/athena_orchestrator/guardian_regime.py"] += 1
        self.assertEqual(found, READER_FILES)

    def test_every_inline_literal_reads_from_news_events(self):
        for rel, lit in _all_literals():
            with self.subTest(site=rel):
                self.assertIn("FROM news_events", lit.value)

    def test_every_inline_literal_keeps_its_own_where_order_limit(self):
        # The SQL bodies were not rewritten during wiring: each still has its
        # own WHERE / ORDER BY / LIMIT and placeholder count.
        for rel, lit in _all_literals():
            with self.subTest(site=rel):
                rendered = news_read.render(lit.value, source=news_read.CANONICAL)
                self.assertEqual(rendered, news_read.CANONICAL_CTE + lit.value)

    def test_all_reader_modules_import_news_read(self):
        for rel in READER_FILES:
            with self.subTest(site=rel):
                self.assertIn("from athena_store import news_read", (ROOT / rel).read_text())

    def test_guardian_news_sql_is_passed_through_render(self):
        text = (ROOT / "apps/orchestrator/src/athena_orchestrator/guardian_regime.py").read_text()
        self.assertIn("conn.execute(news_read.render(_NEWS_SQL), (trade_day,))", text)

    def test_r1_et_bucketing_sql_is_byte_unchanged(self):
        # Extract _NEWS_SQL itself: the module also has two market_bars ET
        # clauses, so a whole-file assertIn would not detect a change here.
        tree = ast.parse(
            (ROOT / "apps/orchestrator/src/athena_orchestrator/guardian_regime.py").read_text()
        )
        values = [
            node.value.value
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "_NEWS_SQL" for t in node.targets)
            and isinstance(node.value, ast.Constant)
        ]
        self.assertEqual(len(values), 1)
        self.assertIn("(ts AT TIME ZONE 'America/New_York')::date = %s", values[0])
        self.assertNotIn("AT TIME ZONE 'UTC'", values[0])

    def test_only_indicators_uses_score_sql(self):
        users = [
            rel for rel in READER_FILES
            if "news_read.score_sql(" in (ROOT / rel).read_text()
        ]
        self.assertEqual(users, ["packages/strategy-nfr/src/athena_strategy/indicators.py"])

    def test_indicators_uses_score_sql_for_news_score(self):
        text = (ROOT / "packages/strategy-nfr/src/athena_strategy/indicators.py").read_text()
        self.assertIn("news_read.score_sql(", text)
        self.assertIn("ORDER BY ts\n", text)

    def test_writers_are_not_wrapped(self):
        text = (ROOT / "packages/strategy-nfr/src/athena_strategy/news.py").read_text()
        self.assertEqual(text.count("INSERT INTO news_events"), 2)
        self.assertEqual(text.count("UPDATE news_events"), 1)
        # Only one render() in news.py — tags_from_events(), a reader.
        self.assertEqual(text.count("news_read.render("), 1)

    def test_dsh_sentiment_writer_is_not_wrapped(self):
        text = (ROOT / "packages/strategy-nfr/src/athena_strategy/dsh.py").read_text()
        self.assertIn("UPDATE news_events SET sentiment", text)
        self.assertNotIn("news_read.render(", text)


class TestNoSchemaChange(unittest.TestCase):
    def test_no_058_migration(self):
        self.assertFalse(list(MIG_DIR.glob("058*")))
        self.assertEqual(len(list(MIG_DIR.glob("*.sql"))), 53)

    def test_pyproject_has_no_new_force_include(self):
        text = (ROOT / "packages/store/pyproject.toml").read_text()
        self.assertIn('"src/athena_store/migrations/057_news_canonical.sql"', text)
        self.assertNotIn("058_", text)
        # news_read.py is a Python module, auto-included by hatch; no entry needed.
        self.assertNotIn("news_read.py", text)

    def test_store_dependency_already_declared_in_all_reader_packages(self):
        for pyproject in (
            "apps/orchestrator/pyproject.toml",
            "apps/bots/pyproject.toml",
            "packages/strategy-nfr/pyproject.toml",
        ):
            with self.subTest(package=pyproject):
                self.assertIn("athena-store", (ROOT / pyproject).read_text())


class TestCutbackPath(unittest.TestCase):
    def test_flipping_back_to_legacy_restores_all_17_bodies(self):
        with patch.object(news_read, "SOURCE", news_read.CANONICAL):
            canonical = [
                news_read.render(lit.value) for _rel, lit in _all_literals()
            ]
            self.assertTrue(all(c.startswith("WITH news_events AS (") for c in canonical))
        for (_rel, lit), before in zip(_all_literals(), canonical):
            with self.subTest(site=_rel):
                self.assertEqual(news_read.render(lit.value), lit.value)
                self.assertNotEqual(news_read.render(lit.value), before)

    def test_reverting_the_module_would_leave_pure_sql_at_call_sites(self):
        # Each wrapped call is a single expression: news_read.render(<literal>).
        # Removing the import + wrapper is a one-line-per-site mechanical edit.
        for rel in READER_FILES:
            tree = ast.parse((ROOT / rel).read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    if "FROM news_events" in node.value and node.value.lstrip().upper().startswith("SELECT"):
                        parents = [
                            p for p in ast.walk(tree)
                            if isinstance(p, ast.Call)
                            and isinstance(p.func, ast.Attribute)
                            and isinstance(p.func.value, ast.Name)
                            and p.func.value.id == "news_read"
                            and node in p.args
                        ]
                        # _NEWS_SQL is a module-level constant, wrapped at its call site.
                        if not parents:
                            self.assertEqual(
                                rel,
                                "apps/orchestrator/src/athena_orchestrator/guardian_regime.py",
                            )

    def test_p1_readonly_contract_survives_a_canonical_flip(self):
        # Import the P1 helper and prove an arbitrary WITH is still rejected
        # while the exact canonical CTE is accepted.
        from apps.orchestrator.tests.test_iris_p1 import _readonly_body

        arbitrary = "WITH evil AS (DELETE FROM news_events) SELECT 1"
        self.assertFalse(_readonly_body(arbitrary).upper().startswith("SELECT"))
        exact = news_read.render("SELECT 1 FROM news_events", source=news_read.CANONICAL)
        self.assertTrue(_readonly_body(exact).upper().startswith("SELECT"))


if __name__ == "__main__":
    unittest.main()
