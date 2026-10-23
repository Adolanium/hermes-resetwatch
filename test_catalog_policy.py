"""Protect the reviewed catalog SHA from in-app self-update paths."""
import json
import unittest
from pathlib import Path
import os
import subprocess
import tempfile
from scripts.catalog_policy import strip_codex_renew, strip_updater

ROOT = Path(__file__).resolve().parent


class CatalogPolicyTests(unittest.TestCase):
    def test_packaged_code_has_no_self_update_entrypoints_or_helpers(self):
        source = (ROOT / "catalog/desktop/plugin.js").read_text(encoding="utf-8")
        for symbol in ("createDesktopUpdater", "desktopUpdater", "UPDATE_KEY", "UpdateControls", "runUpdate", "loadUpdateBackup", "fetchUpdateText", "replacePlugin", "Check for updates", "Restore previous version", "releases/latest"):
            with self.subTest(symbol=symbol):
                self.assertNotIn(symbol, source)
        self.assertIn("export default", source)
        self.assertIn("register(ctx)", source)

    def test_codex_renew_stays_in_the_package_until_it_is_removed_on_purpose(self):
        root = (ROOT / "plugin.js").read_text(encoding="utf-8")
        packaged = (ROOT / "catalog/desktop/plugin.js").read_text(encoding="utf-8")
        build = (ROOT / "scripts/build_catalog.py").read_text(encoding="utf-8")
        self.assertIn("// BEGIN CODEX RENEW ACTION", packaged)
        self.assertIn("['auth', 'refresh', 'openai-codex', id]", packaged)
        self.assertNotIn("strip_codex_renew", build)
        stripped = strip_codex_renew(root)
        self.assertNotIn("CODEX RENEW", stripped)
        self.assertNotIn("codexRenewActionsFor", stripped)
        self.assertNotIn("['auth', 'refresh', 'openai-codex', id]", stripped)
        self.assertIn("export default", stripped)
        probe = (ROOT / "catalog/desktop/probe.py").read_text(encoding="utf-8")
        self.assertNotIn("cli.exec", probe)
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as handle:
            handle.write(stripped)
            stripped_path = handle.name
        try:
            syntax = subprocess.run(["node", "--check", stripped_path], capture_output=True, text=True)
            self.assertEqual(syntax.returncode, 0, syntax.stderr)
        finally:
            os.unlink(stripped_path)

    def test_unknown_updater_layout_cannot_silently_ship(self):
        config = json.loads((ROOT / "catalog-package.json").read_text())
        with self.assertRaises(ValueError):
            strip_updater("export default {};", config["updater"])


if __name__ == "__main__":
    unittest.main()
