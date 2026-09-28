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


if __name__ == "__main__":
    unittest.main()
