"""The console footer shows the product version above the host."""

import unittest
from pathlib import Path

from athena_orchestrator.cockpit import footer
from athena_orchestrator.version import __version__


class VersionBadgeTest(unittest.TestCase):
    def test_version_sits_above_the_host(self) -> None:
        html = footer()
        version_at = html.index('data-cycle="version"')
        host_at = html.index('data-cycle="host"')
        self.assertLess(version_at, host_at)
        self.assertIn(f"v{__version__}", html)

    def test_version_matches_the_package(self) -> None:
        text = Path("apps/orchestrator/pyproject.toml").read_text(encoding="utf-8")
        self.assertIn(f'version = "{__version__}"', text)

    def test_nav_foot_has_no_divider(self) -> None:
        css = Path("design/shared/console.css").read_text(encoding="utf-8")
        foot = css.split(".nav .foot {", 1)[1].split("}", 1)[0]
        self.assertIn("border-top: 0", foot)
        self.assertNotIn("border-top: 1px", foot)


if __name__ == "__main__":
    unittest.main()
