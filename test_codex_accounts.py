"""Codex subscriptions are accounts, not individual OAuth login tokens."""
import base64
import json
import unittest
from unittest.mock import patch
from test_probe_runtime import probes


def codex_jwt(sub, account="workspace-1", residency=None):
    auth = {"chatgpt_account_id": account}
    if residency:
        auth["chatgpt_data_residency"] = residency
    raw = json.dumps({"sub": sub, "https://api.openai.com/auth": auth}, separators=(",", ":")).encode()
    body = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    return "e30." + body + ".sig"


def usage_payload(*, allowed=True, limit_reached=False, reached_type="", used=12):
    rate = {
        "allowed": allowed,
        "limit_reached": limit_reached,
        "primary_window": {"used_percent": used, "limit_window_seconds": 18000, "reset_at": 1900000000},
    }
    if reached_type:
        rate["rate_limit_reached_type"] = reached_type
    return {"plan_type": "plus", "rate_limit": rate}


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

    def test_workspace_members_stay_separate(self):
        rows = [
            {"id": "ada", "access_token": codex_jwt("user-a"), "label": "ada@example.com"},
            {"id": "bo", "access_token": codex_jwt("user-b"), "label": "bo@example.com"},
        ]
        for source, probe in probes():
            with self.subTest(source=source), patch.object(probe, "_codex_pool_entries", return_value=rows), \
                 patch.object(probe, "_codex_token_expiring", return_value=False):
                accounts = probe._codex_pool_accounts()
                self.assertEqual(sorted(account["key"] for account in accounts), ["ada", "bo"])
                self.assertEqual({account["account_id"] for account in accounts}, {"workspace-1"})

    def test_same_person_on_different_hosts_stays_separate(self):
        token = codex_jwt("user-a")
        rows = [
            {"id": "home", "access_token": token, "base_url": "https://chatgpt.com/backend-api/codex"},
            {"id": "eu", "access_token": token + "eu", "base_url": "https://eu.example.test/backend-api/codex"},
        ]
        for source, probe in probes():
            with self.subTest(source=source), patch.object(probe, "_codex_pool_entries", return_value=rows), \
                 patch.object(probe, "_codex_token_expiring", return_value=False):
                accounts = probe._codex_pool_accounts()
                self.assertEqual(sorted(account["key"] for account in accounts), ["eu", "home"])

    def test_healthy_and_revoked_logins_become_one_row(self):
        good = codex_jwt("user-a")
        rows = [
            {"id": "revoked", "access_token": good + "revoked", "last_status": "dead", "label": "ada@example.com"},
            {"id": "healthy", "access_token": good, "label": "ada@example.com"},
        ]
        for source, probe in probes():
            with self.subTest(source=source), patch.object(probe, "_codex_pool_entries", return_value=rows), \
                 patch.object(probe, "_codex_token_expiring", return_value=False):
                accounts = probe._codex_pool_accounts()
                self.assertEqual(len(accounts), 1)
                self.assertEqual(accounts[0]["key"], "healthy")
                self.assertNotIn("status", accounts[0])
                self.assertEqual([item["key"] for item in accounts[0]["candidates"]], ["healthy"])

    def _fetch(self, probe, rows, respond):
        import httpx

        accounts = None
        with patch.object(probe, "_codex_pool_entries", return_value=rows), \
             patch.object(probe, "_codex_token_expiring", return_value=False):
            accounts = probe._codex_pool_accounts()
        real_client = httpx.Client

        def factory(*_args, **_kwargs):
            return real_client(transport=httpx.MockTransport(respond))

        with patch.object(httpx, "Client", side_effect=factory):
            snap = probe._codex_pool_fetcher(accounts[0])()
        return accounts, snap

    def test_sibling_fallback_stops_after_one_success(self):
        bad = codex_jwt("user-a")
        good = bad + "good"
        extra = bad + "extra"
        rows = [
            {"id": "bad", "access_token": bad},
            {"id": "good", "access_token": good},
            {"id": "extra", "access_token": extra},
        ]
        calls = []

        def respond(request):
            auth = request.headers["authorization"]
            calls.append(auth)
            if auth.endswith(bad):
                return __import__("httpx").Response(401, json={"error": {"code": "token_revoked"}})
            return __import__("httpx").Response(200, json=usage_payload())

        for source, probe in probes():
            calls.clear()
            with self.subTest(source=source):
                accounts, snap = self._fetch(probe, rows, respond)
                self.assertEqual(len(accounts), 1)
                self.assertEqual(len(calls), 2)
                self.assertTrue(calls[0].endswith(bad))
                self.assertTrue(calls[1].endswith(good))
                self.assertEqual(snap["auth_state"], "ok")
                self.assertEqual(snap["quota_state"], "available")
                self.assertEqual(snap["account_key"], "good")
                self.assertNotIn("error", snap)
                self.assertTrue(snap.get("last_attempt_at"))

    def test_token_revoked_maps_to_sign_in_again(self):
        import httpx

        def respond(_request):
            return httpx.Response(401, json={"detail": {"code": "token_revoked"}})

        for source, probe in probes():
            with self.subTest(source=source):
                client = httpx.Client(transport=httpx.MockTransport(respond))
                with patch.object(httpx, "Client", return_value=client):
                    snap = probe._fetch_codex_usage(codex_jwt("user-a"), "workspace-1")
                self.assertEqual(snap["auth_state"], "sign_in")
                self.assertEqual(snap["reason_code"], "token_revoked")
                self.assertEqual(snap["windows"], [])
                leaked = {key: value for key, value in snap.items() if key != "reason_code"}
                self.assertNotIn("token_revoked", json.dumps(leaked))

    def test_unknown_401_maps_to_expired_not_checked(self):
        import httpx

        def respond(_request):
            return httpx.Response(401, json={"error": {"code": "session_expired"}})

        for source, probe in probes():
            with self.subTest(source=source):
                client = httpx.Client(transport=httpx.MockTransport(respond))
                with patch.object(httpx, "Client", return_value=client):
                    snap = probe._fetch_codex_usage(codex_jwt("user-a"), "workspace-1")
                self.assertEqual(snap["auth_state"], "expired")
                self.assertNotIn("reason_code", snap)
                self.assertEqual(snap["windows"], [])

    def test_limit_reached_is_limited_with_a_reset(self):
        import httpx

        def respond(_request):
            return httpx.Response(200, json=usage_payload(allowed=False, limit_reached=True, reached_type="primary", used=100))

        for source, probe in probes():
            with self.subTest(source=source):
                client = httpx.Client(transport=httpx.MockTransport(respond))
                with patch.object(httpx, "Client", return_value=client):
                    snap = probe._fetch_codex_usage(codex_jwt("user-a"), "workspace-1")
                self.assertEqual(snap["auth_state"], "ok")
                self.assertEqual(snap["quota_state"], "limited")
                self.assertTrue(snap["windows"][0]["reset_at"])
                self.assertNotIn("error", snap)

    def test_residency_header_is_sent(self):
        import httpx

        seen = {}

        def respond(request):
            seen["residency"] = request.headers.get("x-openai-internal-codex-residency")
            seen["account"] = request.headers.get("ChatGPT-Account-Id")
            return httpx.Response(200, json=usage_payload())

        for source, probe in probes():
            seen.clear()
            with self.subTest(source=source):
                client = httpx.Client(transport=httpx.MockTransport(respond))
                with patch.object(httpx, "Client", return_value=client):
                    snap = probe._fetch_codex_usage(codex_jwt("user-a", residency="eu"))
                self.assertEqual(seen["residency"], "eu")
                self.assertEqual(seen["account"], "workspace-1")
                self.assertEqual(snap["quota_state"], "available")

    def test_429_does_not_fall_back_to_a_sibling(self):
        import httpx

        first = codex_jwt("user-a")
        second = first + "second"
        rows = [
            {"id": "first", "access_token": first},
            {"id": "second", "access_token": second},
        ]
        calls = []

        def respond(request):
            calls.append(request.headers["authorization"])
            return httpx.Response(429, json={"error": {"code": "rate_limit_exceeded"}})

        for source, probe in probes():
            calls.clear()
            with self.subTest(source=source):
                _accounts, snap = self._fetch(probe, rows, respond)
                self.assertEqual(len(calls), 1)
                self.assertTrue(calls[0].endswith(first))
                self.assertEqual(snap["auth_state"], "unknown")
                self.assertNotIn("reason_code", snap)

    def test_fallback_stops_when_the_time_budget_is_gone(self):
        import httpx

        first = codex_jwt("user-a")
        second = first + "second"
        rows = [
            {"id": "first", "access_token": first},
            {"id": "second", "access_token": second},
        ]
        calls = []

        def respond(request):
            calls.append(request.headers["authorization"])
            return httpx.Response(403, json={"error": {"code": "token_invalidated"}})

        for source, probe in probes():
            calls.clear()
            with self.subTest(source=source), patch.object(probe, "_seconds_left", return_value=0):
                _accounts, snap = self._fetch(probe, rows, respond)
                self.assertEqual(len(calls), 1)
                self.assertEqual(snap["auth_state"], "sign_in")
                self.assertEqual(snap["reason_code"], "token_invalidated")


if __name__ == "__main__":
    unittest.main()
