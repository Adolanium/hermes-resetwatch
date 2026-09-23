"""Provider key fallbacks: secret-source caches and container env files.

Shell children can run with provider credentials scrubbed, so the probe reads
secret-source plugin caches and the Docker image's container env files when
the real environment and .env have nothing.
"""
import importlib.util
import json
import os
import tempfile
import time
import unittest
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
    (cache / "vaultwarden_cache.json").write_text(
        json.dumps({"key": "fingerprint", "secrets": {name: value}, "fetched_at": time.time()}),
        encoding="utf-8",
    )


class ProbeEnvFallbackTests(unittest.TestCase):
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
                cache = home / "cache"
                cache.mkdir()
                (cache / "broken_cache.json").write_text("{not json", encoding="utf-8")
                (cache / "other_cache.json").write_text(
                    json.dumps({"key": "k", "fetched_at": time.time()}), encoding="utf-8")
                (cache / "typed_cache.json").write_text(
                    json.dumps({"key": "k", "secrets": {NAME: 42}, "fetched_at": time.time()}),
                    encoding="utf-8")
                with patch.dict(os.environ, {}, clear=True), \
                     patch.object(probe, "_hermes_homes", return_value=[home]), \
                     patch.object(probe, "_S6_ENV_DIR", home / "s6"):
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


if __name__ == "__main__":
    unittest.main()
