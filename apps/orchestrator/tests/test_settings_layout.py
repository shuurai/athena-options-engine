"""§27 / C-20261124-6: /settings three-column layout.

The `Trigger lines` and `VIX data` cards each own an independent POST form.
They must stay OUTSIDE `#settings-form` (no nested forms, no `action` hidden
fields leaking into autosave ``FormData(settingsForm)``) while being placed in
the left / middle columns of a second aligned three-column band.

No new decision node, no migration, no Settings field, no §1/§6 revision.
"""

from __future__ import annotations

import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
