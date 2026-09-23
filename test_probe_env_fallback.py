"""Provider key fallbacks: secret-source caches and container env files.

Shell children can run with provider credentials scrubbed, so the probe reads
secret-source plugin caches and the Docker image's container env files when
the real environment and .env have nothing.
"""
import importlib.util
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

NAME = "OPENROUTER_API_KEY"


def probes():
    for source in ("probe.py", "catalog/desktop/probe.py"):
        spec = importlib.util.spec_from_file_location("env_fallback_probe", Path(__file__).parent / source)
        probe = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(probe)
        yield source, probe


def write_secret_cache(home: Path, value: str, name: str = NAME) -> None:
    cache = home / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(json.dumps({"secrets": {"vaultwarden": {
        "enabled": True, "item_name": "Hermes", "override_existing": True,
    }}}), encoding="utf-8")
    (home / ".env").write_text("BW_SESSION=test-session\n", encoding="utf-8")
    fingerprint = hashlib.sha256(b"test-session").hexdigest()[:16]
    (cache / "vaultwarden_cache.json").write_text(
        json.dumps({"key": f"vw|{fingerprint}|Hermes|||", "secrets": {name: value}, "fetched_at": time.time()}),
        encoding="utf-8",
    )


class ProbeEnvFallbackTests(unittest.TestCase):
    def test_dotenv_precedence_and_preserve_existing(self):
        for source, probe in probes():
            with self.subTest(source=source), tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp)
                write_secret_cache(home, "vault-key")
                with (home / ".env").open("a") as stream:
                    stream.write(f"{NAME}=dotenv-key\n")
                config = json.loads((home / "config.yaml").read_text())
                with patch.dict(os.environ, {}, clear=True), \
                     patch.object(probe, "_hermes_homes", return_value=[home]), \
                     patch.object(probe, "_S6_ENV_DIR", home / "s6"):
                    self.assertEqual(probe._hermes_env_value(NAME), "vault-key")
                    config["secrets"]["preserve_existing"] = [NAME]
                    (home / "config.yaml").write_text(json.dumps(config))
                    self.assertEqual(probe._hermes_env_value(NAME), "dotenv-key")
                    del config["secrets"]["preserve_existing"]
                    config["secrets"]["vaultwarden"]["override_existing"] = False
                    (home / "config.yaml").write_text(json.dumps(config))
                    self.assertEqual(probe._hermes_env_value(NAME), "dotenv-key")

    def test_snapshot_survives_refetch_interval_but_not_invalid_timestamp(self):
        for source, probe in probes():
            with self.subTest(source=source), tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp)
                write_secret_cache(home, "vault-key")
                path = home / "cache/vaultwarden_cache.json"
                payload = json.loads(path.read_text())
                payload["fetched_at"] = time.time() - 86400
                path.write_text(json.dumps(payload))
                with patch.dict(os.environ, {}, clear=True), \
                     patch.object(probe, "_hermes_homes", return_value=[home]), \
                     patch.object(probe, "_S6_ENV_DIR", home / "s6"):
                    self.assertEqual(probe._hermes_env_value(NAME), "vault-key")
                    for stamp in (None, True, "yesterday", 10**400, float("nan"), float("inf"), time.time() + 3600):
                        payload["fetched_at"] = stamp
                        path.write_text(json.dumps(payload))
                        self.assertIsNone(probe._hermes_env_value(NAME))

    def test_missing_profile_cache_does_not_read_another_home(self):
        for source, probe in probes():
            with self.subTest(source=source), tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp)
                first, second = home / "first", home / "second"
                first.mkdir()
                write_secret_cache(second, "other-home-key")
                with patch.dict(os.environ, {}, clear=True), \
                     patch.object(probe, "_hermes_homes", return_value=[first, second]), \
                     patch.object(probe, "_S6_ENV_DIR", home / "s6"):
                    self.assertIsNone(probe._hermes_env_value(NAME))
                    write_secret_cache(first, "first-home-key")
                    self.assertEqual(probe._hermes_env_value(NAME), "first-home-key")

    def test_cache_requires_current_source_and_identity(self):
        for source, probe in probes():
            for change in ("disabled", "item", "session", "cache-disabled", "unknown-cache", "other-source"):
                with self.subTest(source=source, change=change), tempfile.TemporaryDirectory() as tmp:
                    home = Path(tmp)
                    write_secret_cache(home, "vault-key")
                    with patch.dict(os.environ, {}, clear=True), \
                         patch.object(probe, "_hermes_homes", return_value=[home]), \
                         patch.object(probe, "_S6_ENV_DIR", home / "s6"):
                        self.assertEqual(probe._hermes_env_value(NAME), "vault-key")
                        config = json.loads((home / "config.yaml").read_text())
                        cfg = config["secrets"]["vaultwarden"]
                        if change == "disabled":
                            cfg["enabled"] = False
                        elif change == "item":
                            cfg["item_name"] = "Other account"
                        elif change == "session":
                            (home / ".env").write_text("BW_SESSION=other-session\n")
                        elif change == "cache-disabled":
                            cfg["cache_ttl_seconds"] = 0
                        elif change == "unknown-cache":
                            (home / "cache/vaultwarden_cache.json").rename(home / "cache/unknown_cache.json")
                        else:
                            config["secrets"]["onepassword"] = {"enabled": True}
                        (home / "config.yaml").write_text(json.dumps(config))
                        self.assertIsNone(probe._hermes_env_value(NAME))

    def test_sibling_profile_never_uses_container_keys(self):
        for source, probe in probes():
            with self.subTest(source=source), tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp)
                s6 = home / "s6"
                s6.mkdir()
                (s6 / NAME).write_text("gateway-key")
                with patch.dict(os.environ, {}, clear=True), \
                     patch.object(probe, "_hermes_homes", return_value=[home]), \
                     patch.object(probe, "_S6_ENV_DIR", s6):
                    self.assertEqual(probe._hermes_env_value(NAME), "gateway-key")
                    with patch.object(probe, "_profile_inherits_env", False):
                        self.assertIsNone(probe._hermes_env_value(NAME))

    def test_usage_env_restores_blank_values_after_exception(self):
        for source, probe in probes():
            with self.subTest(source=source), tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp)
                write_secret_cache(home, "vault-key")
                with patch.dict(os.environ, {NAME: "  "}, clear=True), \
                     patch.object(probe, "_hermes_homes", return_value=[home]), \
                     patch.object(probe, "_S6_ENV_DIR", home / "s6"):
                    with self.assertRaisesRegex(RuntimeError, "usage failed"):
                        with probe._hermes_usage_env():
                            self.assertEqual(os.environ[NAME], "vault-key")
                            raise RuntimeError("usage failed")
                    self.assertEqual(os.environ.get(NAME), "  ")

    def test_secret_source_cache_fills_missing_env_value(self):
        for source, probe in probes():
            with self.subTest(source=source), tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp)
                write_secret_cache(home, "sk-from-cache")
                with patch.dict(os.environ, {}, clear=True), \
                     patch.object(probe, "_hermes_homes", return_value=[home]), \
                     patch.object(probe, "_S6_ENV_DIR", home / "s6"):
                    self.assertEqual(probe._hermes_env_value(NAME), "sk-from-cache")

    def test_environment_value_wins_over_cache(self):
        for source, probe in probes():
            with self.subTest(source=source), tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp)
                write_secret_cache(home, "sk-from-cache")
                with patch.dict(os.environ, {NAME: "sk-from-env"}, clear=True), \
                     patch.object(probe, "_hermes_homes", return_value=[home]), \
                     patch.object(probe, "_S6_ENV_DIR", home / "s6"):
                    self.assertEqual(probe._hermes_env_value(NAME), "sk-from-env")

    def test_container_env_file_fills_missing_env_value(self):
        for source, probe in probes():
            with self.subTest(source=source), tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp)
                s6 = home / "s6"
                s6.mkdir()
                (s6 / NAME).write_text("sk-from-s6\n", encoding="utf-8")
                with patch.dict(os.environ, {}, clear=True), \
                     patch.object(probe, "_hermes_homes", return_value=[home]), \
                     patch.object(probe, "_S6_ENV_DIR", s6):
                    self.assertEqual(probe._hermes_env_value(NAME), "sk-from-s6")

    def test_malformed_caches_are_ignored(self):
        for source, probe in probes():
            with self.subTest(source=source), tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp)
                write_secret_cache(home, "vault-key")
                cache = home / "cache/vaultwarden_cache.json"
                payload = json.loads(cache.read_text())
                payload["secrets"][NAME] = 42
                with patch.dict(os.environ, {}, clear=True), \
                     patch.object(probe, "_hermes_homes", return_value=[home]), \
                     patch.object(probe, "_S6_ENV_DIR", home / "s6"):
                    self.assertEqual(probe._hermes_env_value(NAME), "vault-key")
                    for content in (b"{not json", b"\xff", b"[]", json.dumps(payload).encode()):
                        cache.write_bytes(content)
                        self.assertIsNone(probe._hermes_env_value(NAME))

    def test_usage_env_fills_missing_names_and_restores(self):
        for source, probe in probes():
            with self.subTest(source=source), tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp)
                write_secret_cache(home, "sk-from-cache")
                with patch.dict(os.environ, {}, clear=True), \
                     patch.object(probe, "_hermes_homes", return_value=[home]), \
                     patch.object(probe, "_S6_ENV_DIR", home / "s6"):
                    with probe._hermes_usage_env():
                        self.assertEqual(os.environ.get(NAME), "sk-from-cache")
                    self.assertIsNone(os.environ.get(NAME))

    def test_usage_env_keeps_existing_values(self):
        for source, probe in probes():
            with self.subTest(source=source), tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp)
                write_secret_cache(home, "sk-from-cache")
                with patch.dict(os.environ, {NAME: "sk-from-env"}, clear=True), \
                     patch.object(probe, "_hermes_homes", return_value=[home]), \
                     patch.object(probe, "_S6_ENV_DIR", home / "s6"):
                    with probe._hermes_usage_env():
                        self.assertEqual(os.environ.get(NAME), "sk-from-env")
                    self.assertEqual(os.environ.get(NAME), "sk-from-env")


ENTRYPOINT_RUNNER = r'''
import importlib.util, sys
from pathlib import Path
script, home, url, profile = sys.argv[1:]
Path.home = classmethod(lambda cls: Path(home) / "unused-user")
spec = importlib.util.spec_from_file_location("tested_probe", script)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)
probe.DEEPSEEK_BALANCE_URL = url
probe._S6_ENV_DIR = Path(home) / "no-container-env"
probe.FRESH_MIN_INTERVAL_SECONDS = 0
disabled = ",".join(name for name in probe.LIVE_PROVIDERS if name != "deepseek")
sys.argv = [script, "--gateway-runtime", "--cli-only", "--fresh", "--profile", profile,
            "--disabled-providers=" + disabled]
raise SystemExit(probe.main())
'''


class ScrubbedProbeIntegrationTests(unittest.TestCase):
    def test_entrypoint_returns_profile_cards_without_changing_credentials(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                key = self.headers.get("Authorization")
                requests.append(key)
                amount = {"Bearer alpha-key": "10.00", "Bearer beta-key": "20.00"}.get(key)
                self.send_response(200 if amount else 401)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"is_available": True, "balance_infos": [
                    {"currency": "USD", "total_balance": amount, "topped_up_balance": amount},
                ]}).encode())

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for source in ("probe.py", "catalog/desktop/probe.py"):
                with self.subTest(source=source), tempfile.TemporaryDirectory() as tmp:
                    home = Path(tmp)
                    for name in ("alpha", "beta"):
                        profile = home / "profiles" / name
                        write_secret_cache(profile, name + "-key", "DEEPSEEK_API_KEY")
                        # Exercise real YAML, as used by the reported installation.
                        (profile / "config.yaml").write_text(
                            "secrets:\n  vaultwarden:\n    enabled: true\n    item_name: Hermes\n"
                            "    override_existing: true\n", encoding="utf-8")
                    script = home / ("plugins/resetwatch/desktop/probe.py" if source.startswith("catalog")
                                     else "desktop-plugins/resetwatch/probe.py")
                    script.parent.mkdir(parents=True)
                    shutil.copyfile(Path(__file__).parent / source, script)
                    before = {path: path.read_bytes() for path in home.rglob("*") if path.is_file()}
                    env = {key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR") if key in os.environ}
                    env.update(HERMES_HOME=str(home / "profiles/alpha"), HOME=str(home / "unused-user"),
                               USERPROFILE=str(home / "unused-user"), HERMES_PYTHON=sys.executable)
                    for name, expected in (("alpha", "$10.00"), ("beta", "$20.00"), ("alpha", "$10.00")):
                        result = subprocess.run([sys.executable, "-I", "-c", ENTRYPOINT_RUNNER,
                            str(script), str(home), f"http://127.0.0.1:{server.server_port}/balance", name],
                            env=env, capture_output=True, text=True, timeout=15)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        cards = json.loads(result.stdout)
                        self.assertEqual([card["provider"] for card in cards], ["deepseek"])
                        self.assertTrue(cards[0]["windows"][0]["detail"].startswith(expected + " left"))
                        self.assertNotIn("alpha-key", result.stdout + result.stderr)
                        self.assertNotIn("beta-key", result.stdout + result.stderr)
                    self.assertEqual({path: path.read_bytes() for path in before}, before)
            self.assertEqual(requests, ["Bearer alpha-key", "Bearer beta-key", "Bearer alpha-key"] * 2)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
