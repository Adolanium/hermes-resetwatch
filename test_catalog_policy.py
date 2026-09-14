"""Protect the reviewed catalog SHA from in-app self-update paths."""
import json
import unittest
from pathlib import Path
from scripts.catalog_policy import strip_updater

ROOT = Path(__file__).resolve().parent


class CatalogPolicyTests(unittest.TestCase):
    def test_packaged_code_has_no_self_update_entrypoints_or_helpers(self):
        source = (ROOT / "catalog/desktop/plugin.js").read_text(encoding="utf-8")
        for symbol in ("createDesktopUpdater", "desktopUpdater", "UPDATE_KEY", "UpdateControls", "runUpdate", "loadUpdateBackup", "fetchUpdateText", "replacePlugin", "Check for updates", "Restore previous version", "releases/latest"):
            with self.subTest(symbol=symbol):
                self.assertNotIn(symbol, source)
        self.assertIn("export default", source)
        self.assertIn("register(ctx)", source)

    def test_unknown_updater_layout_cannot_silently_ship(self):
        config = json.loads((ROOT / "catalog-package.json").read_text())
        with self.assertRaises(ValueError):
            strip_updater("export default {};", config["updater"])


if __name__ == "__main__":
    unittest.main()
