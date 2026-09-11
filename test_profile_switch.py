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
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import probe

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


# Load the real entry point, but stop before vendor fetches on a cache miss.
# This also makes it safe to run these tests against a broken or older probe.
RUNNER = r'''
import importlib.util, json, os, socket, subprocess, sys
from pathlib import Path
script, user_home, mode, *flags = sys.argv[1:]
Path.home = classmethod(lambda cls: Path(user_home))
def blocked(*args, **kwargs):
    raise AssertionError("vendor access is blocked in this test")
socket.socket = blocked
subprocess.Popen = blocked
spec = importlib.util.spec_from_file_location("tested_probe", script)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)
probe._collect_hermes = blocked
probe._collect_cli = blocked
if mode == "credentials":
    def collect():
        values = {
            "key": probe._hermes_env_value("DEEPSEEK_API_KEY"),
            "pool": probe._pool_entries("openai-codex"),
        }
        return [{"provider": "fake", "details": [json.dumps(values)]}], True
    probe._collect_cli = collect
sys.argv = [script, "--cli-only", *flags]
raise SystemExit(probe.main())
'''


def run_probe(probe_path: Path, profile: str | None, scratch_home: Path, *, mode="cache", extra_env=None) -> str:
    # Keep only Windows runtime variables. No real provider keys or CLI paths.
    env = {key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR") if key in os.environ}
    user = scratch_home.parent / "test-user"
    user.mkdir(exist_ok=True)
    env.update(HERMES_HOME=str(scratch_home), HOME=str(user), USERPROFILE=str(user), LOCALAPPDATA=str(user / "local"))
    for key, value in (extra_env or {}).items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    flags = [] if profile is None else ["--profile", profile]
    result = subprocess.run(
        [sys.executable, "-I", "-c", RUNNER, str(probe_path), str(user), mode, *flags],
        capture_output=True,
        text=True,
        timeout=10,
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

    def test_default_and_named_profiles_from_both_install_locations(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            seed_profile(home, "alpha", "ALPHA")
            seed_profile(home, "beta", "BETA")
            cache = home / "cache" / "resetwatch"
            cache.mkdir(parents=True)
            (cache / CACHE_NAME).write_text(json.dumps({"fetched_at": time.time(), "snapshots": [{"provider": "fake", "title": "DEFAULT"}]}))
            for install in (home, home / "profiles" / "alpha"):
                script = install / "desktop-plugins" / "resetwatch" / "probe.py"
                script.parent.mkdir(parents=True)
                shutil.copyfile(PROBE, script)
                for name, expected in (("beta", "BETA"), ("default", "DEFAULT"), (None, "ALPHA")):
                    with self.subTest(install=install, profile=name):
                        data = json.loads(run_probe(script, name, home / "profiles" / "alpha"))
                        self.assertEqual(data[0]["title"], expected)

    def test_missing_and_invalid_profiles_never_reuse_inherited_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            seed_profile(home, "alpha", "ALPHA")
            script = home / "desktop-plugins" / "resetwatch" / "probe.py"
            script.parent.mkdir(parents=True)
            shutil.copyfile(PROBE, script)
            for name in ("missing", "", "../alpha", str(home.resolve())):
                with self.subTest(profile=name):
                    output = run_probe(script, name, home / "profiles" / "alpha")
                    self.assertNotIn("ALPHA", output)
                    self.assertIn("probe failed", output)

    def test_cache_miss_uses_only_selected_profile_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            alpha = home / "profiles" / "alpha"
            beta = home / "profiles" / "beta"
            alpha.mkdir(parents=True)
            beta.mkdir()
            (beta / ".env").write_text("DEEPSEEK_API_KEY=beta-key\n")
            script = home / "desktop-plugins" / "resetwatch" / "probe.py"
            script.parent.mkdir(parents=True)
            shutil.copyfile(PROBE, script)
            # Both platform fallback homes contain a different account.
            user = alpha.parent / "test-user"
            for base in (user / ".hermes", user / "local" / "hermes"):
                base.mkdir(parents=True)
                (base / "auth.json").write_text(json.dumps({"credential_pool": {"openai-codex": [{"id": "base-account"}]}}))
            output = json.loads(run_probe(script, "beta", alpha, mode="credentials", extra_env={"DEEPSEEK_API_KEY": "alpha-key"}))
            values = json.loads(output[0]["details"][0])
            self.assertEqual(values, {"key": "beta-key", "pool": []})

    def test_cache_miss_is_blocked_before_vendor_access(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            output = run_probe(PROBE, None, home)
            self.assertIn("vendor access is blocked", output)

    def test_same_profile_keeps_its_process_env_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            alpha = home / "profiles" / "alpha"
            alpha.mkdir(parents=True)
            (alpha / ".env").write_text("DEEPSEEK_API_KEY=file-key\n")
            script = home / "desktop-plugins" / "resetwatch" / "probe.py"
            script.parent.mkdir(parents=True)
            shutil.copyfile(PROBE, script)
            output = json.loads(run_probe(script, "alpha", alpha, mode="credentials", extra_env={"DEEPSEEK_API_KEY": "process-key"}))
            self.assertEqual(json.loads(output[0]["details"][0])["key"], "process-key")

    def test_no_profile_keeps_existing_env_lookup(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            (home / ".env").write_text("DEEPSEEK_API_KEY=file-key\n")
            output = json.loads(run_probe(PROBE, None, home, mode="credentials", extra_env={"DEEPSEEK_API_KEY": "process-key"}))
            self.assertEqual(json.loads(output[0]["details"][0])["key"], "process-key")

    def test_missing_profile_key_does_not_borrow_inherited_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            alpha = home / "profiles" / "alpha"
            beta = home / "profiles" / "beta"
            alpha.mkdir(parents=True)
            beta.mkdir()
            (beta / "auth.json").write_text(json.dumps({"credential_pool": {"openai-codex": [{"id": "beta-account"}]}}))
            script = home / "desktop-plugins" / "resetwatch" / "probe.py"
            script.parent.mkdir(parents=True)
            shutil.copyfile(PROBE, script)
            output = json.loads(run_probe(script, "beta", alpha, mode="credentials", extra_env={"DEEPSEEK_API_KEY": "alpha-key"}))
            self.assertEqual(json.loads(output[0]["details"][0]), {"key": None, "pool": [{"id": "beta-account"}]})

    def test_unwritable_profile_cache_has_its_own_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            homes = [root / "alpha", root / "beta"]
            original_mkdir = Path.mkdir

            def mkdir(path, *args, **kwargs):
                if path in [home / "cache" / "resetwatch" for home in homes]:
                    raise PermissionError("read only")
                return original_mkdir(path, *args, **kwargs)

            caches = []
            with patch.object(Path, "mkdir", mkdir), patch.object(probe.tempfile, "gettempdir", return_value=tmp):
                for home in homes:
                    with patch.object(probe, "_profile_home", home):
                        caches.append(probe._resetwatch_cache_dir())
            self.assertNotEqual(*caches)
            self.assertTrue(all(path.is_dir() and path.is_relative_to(root) for path in caches))

    def test_upstream_usage_keeps_pool_lookup_and_profile_key_aliases(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / ".env").write_text("OPENAI_API_KEY=beta-key\nOPENROUTER_BASE_URL=https://beta.example/api\n")
            calls = []

            def fetch(provider, **kwargs):
                calls.append((provider, kwargs, os.environ.get("OPENROUTER_API_KEY"),
                              os.environ.get("OPENAI_API_KEY"), os.environ.get("OPENROUTER_BASE_URL"),
                              os.environ.get("CUSTOM_BASE_URL")))
                return None

            account_usage = types.ModuleType("agent.account_usage")
            account_usage.fetch_account_usage = fetch
            with patch.dict(sys.modules, {"agent": types.ModuleType("agent"), "agent.account_usage": account_usage}), \
                 patch.object(probe, "_profile_home", home), patch.object(probe, "_profile_inherits_env", False), \
                 patch.dict(os.environ, {"OPENROUTER_API_KEY": "alpha-key", "OPENAI_API_KEY": "alpha-alias",
                                         "CUSTOM_BASE_URL": "https://alpha.example/api"}):
                probe._collect_hermes()
                self.assertEqual(calls, [(provider, {}, None, "beta-key", "https://beta.example/api", None)
                                         for provider in ("openai-codex", "openrouter")])
                self.assertEqual(os.environ["OPENROUTER_API_KEY"], "alpha-key")
                self.assertEqual(os.environ["OPENAI_API_KEY"], "alpha-alias")
                self.assertEqual(os.environ["CUSTOM_BASE_URL"], "https://alpha.example/api")

    def test_default_home_keeps_env_keys_without_home_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scratch = root / "scratch"
            scratch.mkdir()
            user = root / "test-user"
            home = user / "local" / "hermes" if sys.platform == "win32" else user / ".hermes"
            script = home / "desktop-plugins" / "resetwatch" / "probe.py"
            script.parent.mkdir(parents=True)
            shutil.copyfile(PROBE, script)
            output = json.loads(run_probe(script, "default", scratch, mode="credentials", extra_env={
                "HERMES_HOME": None, "DEEPSEEK_API_KEY": "injected-key",
            }))
            self.assertEqual(json.loads(output[0]["details"][0])["key"], "injected-key")


if __name__ == "__main__":
    unittest.main()
