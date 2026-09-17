"""Probe discovery must preserve profile isolation in each supported layout."""
import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from test_profile_switch import CACHE_NAME, REPO_ROOT, run_probe, seed_profile


class ProbeInstallTests(unittest.TestCase):
    def prepare(self, root):
        home = root / "custom home"
        seed_profile(home, "alpha", "ALPHA")
        seed_profile(home, "beta", "BETA")
        cache = home / "cache" / "resetwatch"
        cache.mkdir(parents=True)
        (cache / CACHE_NAME).write_text(json.dumps({
            "fetched_at": time.time(),
            "snapshots": [{"provider": "fake", "title": "DEFAULT"}],
        }))
        return home

    def assert_profiles(self, script, home):
        for name, expected in (("default", "DEFAULT"), ("alpha", "ALPHA"), ("beta", "BETA")):
            with self.subTest(profile=name):
                data = json.loads(run_probe(script, name, home / "profiles" / "alpha"))
                self.assertEqual(data[0]["title"], expected)
                self.assertNotIn("probe failed", json.dumps(data))
        for name in ("missing", "../alpha", "", str(home.resolve())):
            with self.subTest(profile=name):
                data = run_probe(script, name, home / "profiles" / "alpha")
                self.assertIn("probe failed", data)
                self.assertNotIn("ALPHA", data)
                self.assertNotIn("DEFAULT", data)

    def test_root_and_packaged_probe_in_standalone_and_combined_layouts(self):
        for source in ("probe.py", "catalog/desktop/probe.py"):
            for layout in ("desktop-plugins/hermes-resetwatch/probe.py", "plugins/hermes-resetwatch/desktop/probe.py"):
                for profile_install in (False, True):
                    with self.subTest(source=source, layout=layout, profile_install=profile_install), tempfile.TemporaryDirectory() as tmp:
                        home = self.prepare(Path(tmp))
                        install = home / "profiles" / "alpha" if profile_install else home
                        script = install / layout
                        script.parent.mkdir(parents=True)
                        shutil.copyfile(REPO_ROOT / source, script)
                        self.assert_profiles(script, home)

    def test_symlink_to_packaged_probe_resolves_default_and_named_profiles(self):
        for source in ("probe.py", "catalog/desktop/probe.py"):
            with self.subTest(source=source), tempfile.TemporaryDirectory() as tmp:
                home = self.prepare(Path(tmp))
                target = home / "plugins" / "hermes-resetwatch" / "desktop" / "probe.py"
                target.parent.mkdir(parents=True)
                shutil.copyfile(REPO_ROOT / source, target)
                link = home / "desktop-plugins" / "hermes-resetwatch" / "probe.py"
                link.parent.mkdir(parents=True)
                try:
                    link.symlink_to(target)
                except OSError as error:
                    self.skipTest(f"Symlinks unavailable: {error}")
                self.assert_profiles(link, home)


if __name__ == "__main__":
    unittest.main()
