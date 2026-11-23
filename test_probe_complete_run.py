"""A run that silently dropped a served provider must not be cached.

A fetcher that returns None/[] instead of raising leaves no row and no error
card; the run used to stay complete=True and the 5-minute result cache baked
the omission in. _collect_cli now compares the served provider set against
probe_snapshots.full.json (written only for complete runs): a provider that
was served there and is now missing with no error card, is not disabled, and
still has live credentials (same availability gates the fetchers use) makes
the run incomplete. Providers with no credentials on the machine, providers
never served by a complete run, and providers with a visible error card stay
exempt.
"""
import io
import json
import tempfile
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

import probe

CLI_PROVIDERS = (
    "anthropic", "openai-codex", "cursor", "kimi", "grok", "glm", "deepseek",
    "opencode-go", "ollama", "minimax", "novita", "deepinfra", "ai-gateway",
    "commandcode", "copilot",
)

# Every credential-presence helper the availability table may call. A rename
# upstream makes patched() raise AttributeError and this suite fail loudly.
CRED_HELPERS = (
    "_claude_pool_accounts", "_claude_code_access_token",
    "_codex_pool_accounts", "_codex_cli_access_context",
    "_cursor_access_token", "_kimi_code_access_token", "_kimi_coding_api_key",
    "_grok_access_context",
    "_zcode_coding_credentials", "_glm_hermes_api_key",
    "_deepseek_api_key", "_opencode_go_api_keys", "_ollama_api_key",
    "_minimax_credentials", "_novita_api_key", "_deepinfra_api_key",
    "_ai_gateway_api_key", "_commandcode_api_key", "_copilot_token_candidates",
)

FETCHER_HELPERS = (
    "_fetch_claude_accounts_usage", "_fetch_codex_cli_account_usage",
    "_fetch_cursor_account_usage", "_fetch_kimi_account_usage",
    "_fetch_grok_account_usage", "_fetch_glm_zcode_account_usage",
    "_fetch_deepseek_account_usage", "_fetch_ollama_cloud_account_usage",
    "_fetch_minimax_account_usage", "_fetch_novita_account_usage",
    "_fetch_deepinfra_account_usage", "_fetch_ai_gateway_account_usage",
    "_fetch_commandcode_account_usage", "_fetch_copilot_account_usage",
)


def card(provider, error=None):
    if error is not None:
        return {
            "provider": provider,
            "plan": None,
            "details": ["Could not fetch usage: %s" % error],
            "windows": [],
            "error": error,
        }
    return {
        "provider": provider,
        "plan": None,
        "details": [],
        "windows": [{"used_percent": 10.0, "remaining_percent": 90.0, "reset_at": None}],
    }


def live_creds():
    """anthropic/grok/glm/minimax have credentials; every other provider none."""
    return patch.multiple(
        probe,
        _claude_pool_accounts=lambda: [],
        _claude_code_access_token=lambda: "fake-claude-token",
        _grok_access_context=lambda: ("fake-grok-token", "user-1"),
        _zcode_coding_credentials=lambda: ("fake-glm-token", "https://fake.z.ai"),
        _glm_hermes_api_key=lambda: None,
        _minimax_credentials=lambda: ("fake-minimax-token", []),
    )


def fetchers_serving(*silent):
    """Fake every fetcher; the named providers return None, live ones a row."""
    def make(provider):
        if provider in silent:
            return lambda: None
        if provider in ("anthropic", "grok", "glm", "minimax"):
            return lambda: card(provider)
        return lambda: None
    values = {name: make(provider)
              for name, provider in zip(FETCHER_HELPERS, CLI_PROVIDERS)}
    values["_codex_pool_accounts"] = lambda: []
    values["_codex_cli_access_context"] = lambda: None
    values["_opencode_go_api_keys"] = lambda: []
    return patch.multiple(probe, **values)


class CompleteRunTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "hermes-home"
        self.cache_dir = self.home / "cache" / "resetwatch"
        self.cache_dir.mkdir(parents=True)
        patcher = patch.object(probe, "_hermes_homes", lambda: [self.home])
        patcher.start()
        self.addCleanup(patcher.stop)
        saved = set(probe._disabled_providers)
        self.addCleanup(setattr, probe, "_disabled_providers", saved)

    def write_baseline(self, provider_keys):
        payload = {
            "fetched_at": time.time(),
            "disabled_providers": [],
            "snapshots": [card(key) for key in provider_keys],
        }
        (self.cache_dir / "probe_snapshots.full.json").write_text(
            json.dumps(payload), encoding="utf-8")

    def collect(self):
        err = io.StringIO()
        with redirect_stderr(err):
            snaps, complete = probe._collect_cli()
        served = {s.get("provider") for s in snaps}
        return complete, served, err.getvalue()

    def test_complete_run_when_every_provider_serves(self):
        self.write_baseline(["anthropic", "grok", "glm", "minimax"])
        with live_creds(), fetchers_serving():
            complete, served, _ = self.collect()
        self.assertTrue(complete)
        self.assertTrue({"anthropic", "grok", "glm", "minimax"} <= served)

    def test_silent_drop_of_a_served_provider_is_not_cached(self):
        self.write_baseline(["anthropic", "grok", "glm", "minimax"])
        with live_creds(), fetchers_serving("anthropic"):
            complete, served, note = self.collect()
        self.assertFalse(complete)
        self.assertNotIn("anthropic", served)
        self.assertIn("anthropic", note)
        self.assertTrue({"grok", "glm", "minimax"} <= served)

    def test_error_card_keeps_the_run_complete(self):
        self.write_baseline(["anthropic", "grok", "glm", "minimax"])
        with live_creds(), fetchers_serving("anthropic"), patch.object(
                probe, "_fetch_claude_accounts_usage",
                lambda: [card("anthropic", error="HTTP 503 Service Unavailable")]):
            complete, served, _ = self.collect()
        self.assertTrue(complete)
        self.assertIn("anthropic", served)

    def test_provider_without_credentials_is_exempt(self):
        self.write_baseline(["anthropic", "grok", "glm", "minimax"])
        with patch.multiple(probe,
                            _claude_pool_accounts=lambda: [],
                            _claude_code_access_token=lambda: None), \
             fetchers_serving("anthropic"):
            complete, _, _ = self.collect()
        self.assertTrue(complete)

    def test_disabled_provider_is_exempt(self):
        self.write_baseline(["anthropic", "grok", "glm", "minimax"])
        probe._disabled_providers = {"anthropic"}
        with live_creds(), fetchers_serving("anthropic"):
            complete, _, _ = self.collect()
        self.assertTrue(complete)

    def test_provider_never_served_by_a_complete_run_is_exempt(self):
        self.write_baseline(["grok", "glm", "minimax"])
        with live_creds(), fetchers_serving("anthropic"):
            complete, _, _ = self.collect()
        self.assertTrue(complete)

    def test_crashing_availability_gate_is_exempt(self):
        self.write_baseline(["anthropic", "grok", "glm", "minimax"])
        def boom():
            raise RuntimeError("gate exploded")
        with patch.object(probe, "_claude_code_access_token", boom), \
             fetchers_serving("anthropic"):
            complete, _, _ = self.collect()
        self.assertTrue(complete)

    def test_missing_baseline_file_is_exempt(self):
        with live_creds(), fetchers_serving("anthropic"):
            complete, _, _ = self.collect()
        self.assertTrue(complete)

    def test_provider_alias_normalizes_before_comparison(self):
        self.write_baseline(["xai-oauth", "glm", "minimax"])
        with live_creds(), fetchers_serving("grok"):
            complete, _, _ = self.collect()
        self.assertFalse(complete)

    def test_availability_table_covers_every_live_cli_provider(self):
        with patch.multiple(probe, **{name: (lambda *a, **k: None)
                                      for name in CRED_HELPERS}):
            uncovered = [p for p in CLI_PROVIDERS
                         if probe._provider_has_live_credentials(p)]
        self.assertEqual(uncovered, [])

    def test_unknown_provider_defaults_to_live(self):
        self.assertTrue(probe._provider_has_live_credentials("brand-new-vendor"))


if __name__ == "__main__":
    unittest.main()
