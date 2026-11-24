"""§27 / C-20261124-6 and §28 / C-20261124-7: /settings layout + VIX card copy.

The `Trigger lines` and `VIX data` cards each own an independent POST form.
They must stay OUTSIDE `#settings-form` (no nested forms, no `action` hidden
fields leaking into autosave ``FormData(settingsForm)``) while being placed in
the left / middle columns of a second aligned three-column band.

§28: the VIX card must be English-only (like the rest of /settings), and its
three derived states are card-level conclusions — not a second copy of the
closes table — so they live in a distinct bottom-right block.

No new decision node, no migration, no Settings field, no §1/§6 revision.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from athena_marketdata import vixfeed
from athena_orchestrator import pages as pg
from athena_settings.model import SettingsPayload, TriggerLine, standard_payload

ROOT = Path(__file__).resolve().parents[3]
PAGES = ROOT / "apps/orchestrator/src/athena_orchestrator/pages.py"
CONSOLE = ROOT / "apps/orchestrator/src/athena_orchestrator/console.py"

_EXTRAS = 'class="settings-cols settings-extras"'
_COL = '<div class="settings-col">'


class _Cursor:
    def __init__(self, rows, one=None):
        self.rows, self.one = rows, one

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.one


def _payload():
    """One trigger line so the card renders BOTH add and remove forms."""
    return standard_payload().model_copy(
        update={"trigger_lines": [TriggerLine(symbol="NVDA", op="<", level=118)]}
    )


class _FakeConn:
    """Only enough DB surface for ``_settings()`` to render."""

    def execute(self, sql, params=None):
        flat = " ".join(sql.split())
        if "settings_versions" in flat:
            return _Cursor([], (1, "seed", "x", _payload().model_dump()))
        return _Cursor([])  # symbol_facts, market_bars / ib-vix reads


def _render() -> str:
    return pg._settings(_FakeConn(), "U1", {}, "")["body"]


def _columns(band: str) -> list[str]:
    idxs = [i for i in range(len(band)) if band.startswith(_COL, i)]
    return [
        band[idxs[k] + len(_COL): idxs[k + 1] if k + 1 < len(idxs) else len(band)]
        for k in range(len(idxs))
    ]


class TestSettingsLayout(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.body = _render()
        cls.close = cls.body.index("</form>")
        cls.extras_at = cls.body.index(_EXTRAS)
        cls.band = cls.body[cls.extras_at:]
        cls.cols = _columns(cls.band)

    # -- band placement ----------------------------------------------------
    def test_second_aligned_band_exists_after_main_form(self):
        self.assertIn(_EXTRAS, self.body)
        self.assertLess(self.close, self.extras_at)

    def test_main_settings_form_closes_before_both_cards(self):
        self.assertLess(self.close, self.body.index("Trigger lines"))
        self.assertLess(self.close, self.body.index("VIX data"))

    def test_band_has_exactly_three_columns(self):
        self.assertEqual(len(self.cols), 3)

    # -- column assignment -------------------------------------------------
    def test_trigger_lines_is_in_the_left_column(self):
        self.assertIn("Trigger lines", self.cols[0])
        self.assertNotIn("VIX data", self.cols[0])

    def test_vix_data_is_in_the_middle_column(self):
        self.assertIn("VIX data", self.cols[1])
        self.assertNotIn("Trigger lines", self.cols[1])

    def test_right_column_is_an_empty_spacer(self):
        third = self.cols[2]
        self.assertNotIn('<div class="card">', third)
        self.assertNotIn("Trigger lines", third)
        self.assertNotIn("VIX data", third)

    # -- nested-form / autosave hazard -------------------------------------
    def test_no_nested_form_inside_the_main_settings_form(self):
        opening = self.body.index('<form id="settings-form"')
        inner = self.body.index(">", opening) + 1
        main = self.body[inner:self.close]
        self.assertNotIn("<form", main)
        self.assertNotIn("Trigger lines", main)
        self.assertNotIn("VIX data", main)

    def test_card_hidden_actions_stay_outside_the_main_form(self):
        for value in ('value="trigger_add"', 'value="trigger_remove"',
                      'value="sync_vix"'):
            self.assertGreater(self.body.index(value), self.close)

    def test_autosave_still_serializes_only_the_main_form(self):
        js = CONSOLE.read_text(encoding="utf-8")
        self.assertIn("new FormData(settingsForm)", js)
        self.assertIn('bindAutosaveForm(document.getElementById("settings-form"))', js)

    def test_cards_still_own_their_post_forms(self):
        self.assertIn('<form method="post" action="/settings" class="trigger-add">',
                      pg._trigger_lines_card(_payload()))
        self.assertIn('<form method="post" action="/settings">',
                      pg._vix_card(_FakeConn(), "U1"))

    # -- CSS ---------------------------------------------------------------
    def test_css_extras_band_does_not_stretch_or_clip(self):
        css = CONSOLE.read_text(encoding="utf-8")
        self.assertIn(".stage > .settings-cols.settings-extras", css)
        block = css.split(".stage > .settings-cols.settings-extras", 1)[1].split("}", 1)[0]
        self.assertIn("flex: none", block)
        self.assertIn("overflow: visible", block)
        self.assertIn("align-items: start", block)
        self.assertIn(".settings-cols.settings-extras .settings-col", css)

    def test_css_default_grid_still_has_three_equal_columns(self):
        css = CONSOLE.read_text(encoding="utf-8")
        self.assertIn("grid-template-columns: repeat(3, minmax(0, 1fr))", css)

    # -- untouched behaviour ----------------------------------------------
    def test_existing_card_and_dispatch_behaviour_is_unchanged(self):
        src = PAGES.read_text(encoding="utf-8")
        settings_body = src.split("def _settings(", 1)[1].split("def _prediction_page", 1)[0]
        self.assertIn("_trigger_lines_card(current)", settings_body)
        self.assertIn("_vix_card(conn, account)", settings_body)
        dispatch = src.split("def _post_settings", 1)[1]
        self.assertIn('action == "sync_vix"', dispatch)
        self.assertIn('"trigger_add", "trigger_remove"', dispatch)

    def test_no_new_migration_or_settings_field(self):
        migrations = list((ROOT / "packages/store/src/athena_store/migrations").glob("*.sql"))
        self.assertEqual(len(migrations), 51)
        self.assertIn("trigger_lines", SettingsPayload.model_fields)


class TestVixCardConsistency(unittest.TestCase):
    """§28: English-only copy, derived states separated from the closes table."""

    @classmethod
    def setUpClass(cls):
        cls.empty = pg._vix_card(_FakeConn(), "U1")
        series = [("2026-11-18", 18.0), ("2026-11-19", 19.0),
                  ("2026-11-20", 20.0), ("2026-11-21", 21.0),
                  ("2026-11-24", 22.0)]
        levels = {"VIX": 22.0, "VIX9D": 23.0, "VIX3M": 20.0, "VIX1Y": 19.0}
        with patch.object(vixfeed, "vix_closes", return_value=series), \
             patch.object(vixfeed, "latest_levels", return_value=levels):
            cls.loaded = pg._vix_card(_FakeConn(), "U1")
        cls.css = CONSOLE.read_text(encoding="utf-8")

    # -- language ----------------------------------------------------------
    def test_card_has_no_cjk_characters(self):
        for html in (self.empty, self.loaded):
            for ch in html:
                self.assertFalse("\u4e00" <= ch <= "\u9fff",
                                 f"CJK character {ch!r} leaked into the VIX card")

    def test_chinese_derived_strip_is_gone(self):
        for token in ("水位", "斜率", "期限", "VIX 水位"):
            self.assertNotIn(token, self.empty)
            self.assertNotIn(token, self.loaded)

    def test_derived_labels_are_state_slope_term(self):
        for label in ("<dt>State</dt>", "<dt>Slope</dt>", "<dt>Term</dt>"):
            self.assertIn(label, self.empty)
            self.assertIn(label, self.loaded)

    def test_derived_caption_explains_the_relationship(self):
        self.assertIn("Derived from the closes above", self.empty)

    # -- no duplication of the table ---------------------------------------
    def test_table_is_still_two_columns_and_four_series(self):
        table = self.empty.split("<table", 1)[1].split("</table>", 1)[0]
        self.assertIn("<th>Series</th>", table)
        self.assertIn("<th>Latest close</th>", table)
        self.assertNotIn("<th>State</th>", table)
        self.assertEqual(table.count("<tr>"), 5)  # header + 4 series rows
        for symbol in vixfeed.VIX_SYMBOLS:
            self.assertIn(f"<td>{symbol}</td>", table)

    def test_derived_values_are_outside_the_table(self):
        table = self.empty.split("<table", 1)[1].split("</table>", 1)[0]
        self.assertNotIn("vix-derived", table)
        self.assertLess(self.empty.index("</table>"),
                        self.empty.index('<dl class="vix-derived">'))

    def test_derived_block_is_bottom_right_after_the_sync_form(self):
        self.assertIn('<div class="vix-foot">', self.empty)
        self.assertLess(self.empty.index("</form>"),
                        self.empty.index('<div class="vix-derived-wrap">'))
        self.assertIn(".vix-derived-wrap { text-align: right; margin-left: auto; }", self.css)
        self.assertIn(".vix-foot {", self.css)

    # -- unchanged behaviour ----------------------------------------------
    def test_sync_form_and_action_are_untouched(self):
        for html in (self.empty, self.loaded):
            self.assertIn('name="action" value="sync_vix"', html)
            self.assertIn("Sync VIX Data", html)
            self.assertIn('<form method="post" action="/settings">', html)

    def test_unknown_and_known_cells_keep_their_semantics(self):
        self.assertIn('<span class="tag amber">unknown</span>', self.empty)
        self.assertEqual(self.loaded.count('<span class="mono">'), 7)  # 4 closes + 3 derived
        self.assertIn("high_rising", self.loaded)

    def test_stamp_is_an_english_slot_note_on_the_left(self):
        self.assertIn("Newest close 2026-11-24 · 5 sessions stored.", self.loaded)
        foot = self.loaded.split('<div class="vix-foot">', 1)[1]
        self.assertLess(foot.index("Newest close"), foot.index("vix-derived-wrap"))

    def test_section_27_extras_band_css_is_untouched(self):
        self.assertIn(".stage > .settings-cols.settings-extras", self.css)


if __name__ == "__main__":
    unittest.main()
