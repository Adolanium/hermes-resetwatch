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
        import sys
        import types

        with patch.object(probe.subprocess, "run", side_effect=AssertionError("CLI executed")):
            self.assertIsNone(probe._cursor_cli_json(["status", "--json"]))
        # Anthropic OAuth uses only read_hermes_oauth_credentials (never resolve_anthropic_token or direct file read).
        creds = types.ModuleType("agent.anthropic_credentials")
        creds.read_hermes_oauth_credentials = lambda: {"accessToken": "sk-ant-oat01-readonly"}
        creds.resolve_anthropic_token = lambda *a, **kw: (_ for _ in ()).throw(AssertionError("refreshing resolver"))
        agent_pkg = types.ModuleType("agent")
        agent_pkg.anthropic_credentials = creds
        with patch.dict(sys.modules, {"agent": agent_pkg, "agent.anthropic_credentials": creds}):
            self.assertEqual(probe._hermes_anthropic_oauth_token(), "sk-ant-oat01-readonly")
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / ".anthropic_oauth.json").write_text(json.dumps({"accessToken": "sk-ant-oat01-file"}))
            with patch.dict(sys.modules, {"agent": None, "agent.anthropic_credentials": None}), \
                 patch.object(probe, "_hermes_homes", return_value=[home]):
                self.assertIsNone(probe._hermes_anthropic_oauth_token())
        # Codex standalone helper must pass read_only=True and skip older resolvers that lack it.
        seen_read_only = []
        auth_codex = types.ModuleType("hermes_cli.auth_codex")
        auth_codex.resolve_codex_runtime_credentials = lambda *, read_only=False: (
            seen_read_only.append(read_only) or {"api_key": "codex-ro", "base_url": "https://chatgpt.com/backend-api/codex"}
        )
        cli_pkg = types.ModuleType("hermes_cli")
        cli_pkg.auth_codex = auth_codex
        with patch.dict(sys.modules, {"hermes_cli": cli_pkg, "hermes_cli.auth_codex": auth_codex}):
            self.assertEqual(probe._hermes_codex_runtime_entry()["access_token"], "codex-ro")
        self.assertEqual(seen_read_only, [True])
        for name in ("_write_secret_json", "_kimi_code_refresh_tokens", "_grok_refresh_entry",
                     "_read_env_file_value", "_read_auth_store", "_pool_rows_from_saved_store"):
            self.assertFalse(hasattr(probe, name))
        text = Path(__file__).resolve().parent.joinpath("catalog/desktop/probe.py").read_text(encoding="utf-8")
        for forbidden in ("cli.exec", "auth refresh", "resolve_anthropic_token", ".anthropic_oauth.json"):
            self.assertNotIn(forbidden, text)


if __name__ == "__main__":
    unittest.main()
