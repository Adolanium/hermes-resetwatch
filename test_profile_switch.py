"""Regression test: the probe must serve per-profile data when told --profile.

When the Desktop switches profiles (cain -> jho-low), plugin.js re-runs the
probe with --profile <name>. Without that flag the probe reads whatever home
its gateway inherited, so every profile shows the same cards.

The test builds a fake hermes home with two profiles, seeds a fresh result
cache in each profile's cache dir (so the probe exits on the cache hit and
never touches the network or real credentials), then runs the probe twice:
once with --profile cain and once with --profile jho-low. Both runs must
return that profile's own seeded cards. On the pre-fix probe the flag is
ignored, both runs read the same home, and the outputs match -- failing the
test.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
PROBE = REPO_ROOT / "probe.py"
CACHE_NAME = "probe_snapshots.cli.json"


def seed_profile(home: Path, name: str, marker: str) -> None:
    """Create <home>/profiles/<name> with a fresh cli cache naming the marker."""
    cache_dir = home / "profiles" / name / "cache" / "resetwatch"
    cache_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "fetched_at": time.time(),
        "snapshots": [{"provider": f"fake-{name}", "title": marker}],
    }
    (cache_dir / CACHE_NAME).write_text(json.dumps(payload), encoding="utf-8")


def run_probe(probe_path: Path, profile: str, scratch_home: Path) -> str:
    env = dict(os.environ)
    # Point the inherited home at an empty scratch dir: the pre-fix probe
    # (which ignores --profile) must never see real credentials here.
    env["HERMES_HOME"] = str(scratch_home)
    result = subprocess.run(
        [sys.executable, str(probe_path), "--cli-only", "--profile", profile],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    if result.returncode != 0:
        raise AssertionError(f"probe exited {result.returncode}: {result.stderr}")
    return result.stdout


class ProfileSwitchTest(unittest.TestCase):
    def test_profile_flag_selects_distinct_profile_data(self):
        with tempfile.TemporaryDirectory(prefix="resetwatch-pr-") as tmp:
            root = Path(tmp)
            home = root / "home"
            # Probe installed exactly as the plugin expects, under the home.
            plugins = home / "desktop-plugins" / "resetwatch"
            plugins.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(PROBE, plugins / "probe.py")
            seed_profile(home, "cain", "CAIN-CARDS")
            seed_profile(home, "jho-low", "JHOLOW-CARDS")
            scratch = root / "scratch"
            scratch.mkdir()

            cain = run_probe(plugins / "probe.py", "cain", scratch)
            jholow = run_probe(plugins / "probe.py", "jho-low", scratch)

            self.assertNotEqual(cain, jholow, "both profiles returned the same cards")
            self.assertIn("CAIN-CARDS", cain)
            self.assertIn("JHOLOW-CARDS", jholow)
            self.assertNotIn("JHOLOW-CARDS", cain)
            self.assertNotIn("CAIN-CARDS", jholow)


if __name__ == "__main__":
    unittest.main()
