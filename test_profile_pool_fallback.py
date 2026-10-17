"""A named profile falls back to the base home's credential pool per provider.

Hermes's read_credential_pool() uses the base home's rows for a provider
whenever the profile has none for that provider, even if the profile has its
own auth.json. The probe must follow the same rule so the Claude card does
not disappear on a profile that runs on the base Claude login (#36).
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import probe


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

    def pool(self, provider: str) -> list:
        with patch.object(probe, "_profile_home", self.profile):
            return [row["id"] for row in probe._pool_entries(provider)]

    def test_profile_without_auth_json_uses_base_rows(self):
        self.assertEqual(self.pool("anthropic"), ["base-claude"])

    def test_profile_with_other_provider_only_still_uses_base_rows(self):
        write_pool(self.profile, {"openrouter": [{"id": "profile-openrouter"}]})
        self.assertEqual(self.pool("anthropic"), ["base-claude"])
        self.assertEqual(self.pool("openrouter"), ["profile-openrouter"])

    def test_profile_with_own_rows_uses_only_those(self):
        write_pool(self.profile, {"anthropic": [{"id": "profile-claude"}]})
        self.assertEqual(self.pool("anthropic"), ["profile-claude"])

    def test_home_outside_profiles_has_no_fallback(self):
        other = Path(self.tmp.name) / "elsewhere" / "curry"
        other.mkdir(parents=True)
        with patch.object(probe, "_profile_home", other):
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
