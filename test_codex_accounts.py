"""Codex subscriptions are accounts, not individual OAuth login tokens."""
import unittest
from unittest.mock import patch
from test_probe_runtime import probes


class CodexAccountTests(unittest.TestCase):
    def test_rotated_tokens_for_one_account_produce_one_subscription(self):
        rows = [
            {"id": "login-1", "access_token": "token-a", "label": "first"},
            {"id": "login-2", "access_token": "token-b", "label": "second"},
            {"id": "login-3", "access_token": "token-c", "label": "third"},
        ]
        ids = {"token-a": "account-one", "token-b": "account-one", "token-c": "account-two"}
        for source, probe in probes():
            with self.subTest(source=source), patch.object(probe, "_codex_pool_entries", return_value=rows), \
                 patch.object(probe, "_codex_token_expiring", return_value=False), \
                 patch.object(probe, "_codex_account_id_from_token", side_effect=ids.get):
                accounts = probe._codex_pool_accounts()
                self.assertEqual([a["account_id"] for a in accounts], ["account-one", "account-two"])
                self.assertEqual(accounts[0]["label"], "first")

    def test_unknown_accounts_do_not_merge_by_label(self):
        rows = [{"id": "first", "access_token": "token-a", "label": "same"},
                {"id": "second", "access_token": "token-b", "label": "same"},
                {"id": "duplicate-token", "access_token": "token-a", "label": "same"}]
        for source, probe in probes():
            with self.subTest(source=source), patch.object(probe, "_codex_pool_entries", return_value=rows), \
                 patch.object(probe, "_codex_token_expiring", return_value=False), \
                 patch.object(probe, "_codex_account_id_from_token", return_value=None):
                self.assertEqual(len(probe._codex_pool_accounts()), 2)

    def test_expired_entry_does_not_hide_valid_login_for_same_account(self):
        rows = [{"id": "expired", "access_token": "old"}, {"id": "valid", "access_token": "new"}]
        for source, probe in probes():
            with self.subTest(source=source), patch.object(probe, "_codex_pool_entries", return_value=rows), \
                 patch.object(probe, "_codex_token_expiring", side_effect=lambda token: token == "old"), \
                 patch.object(probe, "_codex_account_id_from_token", return_value="one-account"):
                accounts = probe._codex_pool_accounts()
                self.assertEqual([a["token"] for a in accounts], ["new"])
                self.assertNotIn("status", accounts[0])

    def test_dead_login_for_same_account_does_not_hide_valid_login(self):
        rows = [{"id": "revoked", "access_token": "bad", "last_status": "dead"},
                {"id": "valid", "access_token": "good"}]
        for source, probe in probes():
            with self.subTest(source=source), patch.object(probe, "_codex_pool_entries", return_value=rows),                  patch.object(probe, "_codex_token_expiring", return_value=False),                  patch.object(probe, "_codex_account_id_from_token", return_value="one-account"):
                accounts = probe._codex_pool_accounts()
                self.assertEqual([(a["token"], a.get("status")) for a in accounts], [("good", None)])

    def test_unusable_logins_stay_visible_with_a_reason(self):
        rows = [{"id": "revoked", "access_token": "bad", "last_status": "dead", "label": "work"},
                {"id": "stale", "access_token": "old", "label": "home"}]
        ids = {"bad": "account-one", "old": "account-two"}
        for source, probe in probes():
            with self.subTest(source=source), patch.object(probe, "_codex_pool_entries", return_value=rows),                  patch.object(probe, "_codex_token_expiring", side_effect=lambda token: token == "old"),                  patch.object(probe, "_codex_account_id_from_token", side_effect=ids.get),                  patch.object(probe, "_fetch_codex_usage", side_effect=AssertionError("no vendor call")):
                accounts = probe._codex_pool_accounts()
                self.assertEqual([a["status"] for a in accounts],
                                 [probe.CODEX_DEAD_LOGIN, probe.CODEX_EXPIRED_LOGIN])
                cards = [probe._codex_pool_fetcher(a)() for a in accounts]
                self.assertEqual([c["error"] for c in cards], [probe.CODEX_DEAD_LOGIN, probe.CODEX_EXPIRED_LOGIN])
                self.assertTrue(all(c["windows"] == [] for c in cards))
                self.assertEqual([c["account_key"] for c in cards], ["revoked", "stale"])

    def test_working_cli_login_replaces_stale_pool_card_for_its_account(self):
        rows = [{"id": "revoked", "access_token": "bad", "last_status": "dead"}]
        for source, probe in probes():
            calls = []
            with self.subTest(source=source), patch.object(probe, "_codex_pool_entries", return_value=rows),                  patch.object(probe, "_codex_account_id_from_token", return_value="one-account"),                  patch.object(probe, "_codex_cli_access_context", return_value=("cli-token", "one-account")),                  patch.object(probe, "_fetch_codex_cli_account_usage",
                              side_effect=lambda: calls.append("cli") or probe._snapshot("openai-codex", None, [], ["ok"])),                  patch.object(probe, "_disabled_providers", set(probe.LIVE_PROVIDERS) - {"openai-codex"}):
                snapshots, _complete = probe._collect_cli()
                self.assertEqual(calls, ["cli"])
                self.assertFalse([s for s in snapshots if s.get("error")])


if __name__ == "__main__":
    unittest.main()
