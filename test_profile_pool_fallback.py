"""Credential pool resolution delegates to Hermes's read_credential_pool.

Hermes's read_credential_pool() owns profile fallback to the base home's
auth.json. When that helper cannot be imported, the probe skips the pool
instead of opening auth.json directly.
"""

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import probe
from test_credential_helpers import CredentialHelperTests  # noqa: F401  CI runs this module


def write_pool(home: Path, pool: dict) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "auth.json").write_text(json.dumps({"credential_pool": pool}))


class ProfilePoolFallbackTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name) / "hermes"
        self.profile = self.base / "profiles" / "curry"
        self.profile.mkdir(parents=True)
        write_pool(self.base, {"anthropic": [{"id": "base-claude"}]})

    def tearDown(self):
        self.tmp.cleanup()

    def _helper_modules(self):
        def read_credential_pool(provider_id=None):
            active = probe._profile_home or self.base
            homes = [active]
            if active.parent.name == "profiles":
                homes.append(active.parent.parent)
            for home in homes:
                path = home / "auth.json"
                if not path.is_file():
                    continue
                rows = (json.loads(path.read_text()).get("credential_pool") or {}).get(provider_id)
                if isinstance(rows, list) and rows:
                    return list(rows)
            return []

        auth = types.ModuleType("hermes_cli.auth")
        auth.read_credential_pool = read_credential_pool
        pkg = types.ModuleType("hermes_cli")
        pkg.auth = auth
        return {"hermes_cli": pkg, "hermes_cli.auth": auth}

    def pool(self, provider: str) -> list:
        with patch.dict(sys.modules, self._helper_modules()), \
             patch.object(probe, "_profile_home", self.profile):
            return [row["id"] for row in probe._pool_entries(provider)]

    def test_profile_without_auth_json_uses_base_rows_via_hermes_helper(self):
        self.assertEqual(self.pool("anthropic"), ["base-claude"])

    def test_profile_with_other_provider_only_still_uses_base_rows_via_hermes_helper(self):
        write_pool(self.profile, {"openrouter": [{"id": "profile-openrouter"}]})
        self.assertEqual(self.pool("anthropic"), ["base-claude"])
        self.assertEqual(self.pool("openrouter"), ["profile-openrouter"])

    def test_profile_with_own_rows_uses_only_those_via_hermes_helper(self):
        write_pool(self.profile, {"anthropic": [{"id": "profile-claude"}]})
        self.assertEqual(self.pool("anthropic"), ["profile-claude"])

    def test_missing_helper_skips_auth_json_instead_of_reading_directly(self):
        with patch.dict(sys.modules, {"hermes_cli": None, "hermes_cli.auth": None}), \
             patch.object(probe, "_profile_home", self.profile):
            self.assertEqual(probe._pool_entries("anthropic"), [])


class ProbeCacheObservedAtTest(unittest.TestCase):
    def test_cache_read_keeps_observed_at_and_backfills_old_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "probe.json"
            now = probe.datetime.now(probe.timezone.utc).timestamp()
            path.write_text(json.dumps({
                "fetched_at": now - 60,
                "disabled_providers": [],
                "snapshots": [{"provider": "a", "observed_at": now - 200}, {"provider": "b"}],
            }))
            with patch.object(probe, "_probe_result_cache_path", return_value=path), \
                 patch.object(probe, "_disabled_providers", set()):
                snaps = probe._read_probe_result_cache(cli_only=False)
        self.assertEqual([snap["observed_at"] for snap in snaps], [now - 200, now - 60])


if __name__ == "__main__":
    unittest.main()
