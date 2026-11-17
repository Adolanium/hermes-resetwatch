"""Catalog credentials must survive successful and expired-token usage requests."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("catalog_probe", Path(__file__).parent / "catalog/desktop/probe.py")
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


class CatalogReadOnlyTests(unittest.TestCase):
    def test_vendor_usage_never_exchanges_or_rewrites_credentials(self):
        import httpx

        for vendor in ("kimi", "grok"):
            for status in (200, 401):
                with self.subTest(vendor=vendor, status=status), tempfile.TemporaryDirectory() as tmp:
                    credentials = Path(tmp) / "auth.json"
                    entry = {"access_token": "access", "key": "access", "refresh_token": "rotating-secret", "user_id": "user"}
                    credentials.write_text(json.dumps(entry))
                    before = credentials.read_bytes()
                    requests = []

                    def respond(request):
                        requests.append(request)
                        self.assertEqual(request.method, "GET")
                        self.assertNotIn("rotating-secret", str(request.headers))
                        self.assertNotIn("rotating-secret", request.content.decode())
                        return httpx.Response(status, json={"creditUsagePercent": 20})

                    client = httpx.Client(transport=httpx.MockTransport(respond))
                    with patch.object(httpx, "Client", return_value=client), \
                         patch.object(probe, "_kimi_code_credentials_path", return_value=credentials), \
                         patch.object(probe, "_grok_read_auth", return_value=(credentials, "user", {"user": entry}, entry)):
                        fetch = probe._fetch_kimi_cli_usage if vendor == "kimi" else probe._fetch_grok_account_usage
                        if status == 401:
                            with self.assertRaisesRegex(RuntimeError, "Sign in with the .* CLI"):
                                fetch()
                            self.assertEqual(len(requests), 1)
                        else:
                            fetch()
                            self.assertGreaterEqual(len(requests), 1)
                    self.assertEqual(credentials.read_bytes(), before)
                    self.assertEqual(list(Path(tmp).iterdir()), [credentials])

    def test_catalog_resolver_list_is_the_reviewed_allowlist(self):
        self.assertEqual(probe.HERMES_PROVIDERS, ("nous", "openrouter"))

    def test_nous_reads_the_stored_token_and_never_refreshes(self):
        import sys
        import types

        calls = []
        account = types.SimpleNamespace(logged_in=True)
        nous_account = types.ModuleType("hermes_cli.nous_account")
        nous_account.stored_token_account_info = lambda: calls.append("stored") or account

        def refreshing(*args, **kwargs):
            raise AssertionError("refreshing Nous reader called")

        nous_account.get_nous_portal_account_info = refreshing
        account_usage = types.ModuleType("agent.account_usage")
        account_usage._fetch_portal_account = refreshing

        def build(info, *, cta=True):
            self.assertIs(info, account)
            self.assertFalse(cta)
            return types.SimpleNamespace(provider="nous", plan="Hermes Plus", details=(), windows=(),
                                         available=True, usage_window=window)

        account_usage.build_nous_credits_snapshot = build
        modules = {"hermes_cli": types.ModuleType("hermes_cli"), "hermes_cli.nous_account": nous_account,
                   "agent": types.ModuleType("agent"), "agent.account_usage": account_usage}
        with patch.dict(sys.modules, modules):
            window = True
            self.assertEqual(probe._nous_usage_snapshot().plan, "Hermes Plus")
            # Dollar plans come from usage.bars; a second card would duplicate them.
            window = False
            self.assertIsNone(probe._nous_usage_snapshot())
        self.assertEqual(calls, ["stored", "stored"])

    def test_indirect_credential_refresh_paths_are_excluded(self):
        with patch.object(probe.subprocess, "run", side_effect=AssertionError("CLI executed")):
            self.assertIsNone(probe._cursor_cli_json(["status", "--json"]))
        # Resolver imports must not be attempted, even when a Hermes install exists.
        with patch.object(probe, "_hermes_homes", return_value=[]), \
             patch("builtins.__import__", side_effect=AssertionError("resolver imported")):
            self.assertIsNone(probe._hermes_anthropic_oauth_token())
        for name in ("_write_secret_json", "_kimi_code_refresh_tokens", "_grok_refresh_entry"):
            self.assertFalse(hasattr(probe, name))


if __name__ == "__main__":
    unittest.main()
