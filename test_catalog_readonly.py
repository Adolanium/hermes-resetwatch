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

    def test_indirect_credential_refresh_paths_are_excluded(self):
        self.assertEqual(probe.HERMES_PROVIDERS, ("openrouter",))
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
