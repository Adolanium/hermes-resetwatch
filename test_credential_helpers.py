"""Hermes credentials come exclusively through Hermes's own read-only helpers."""
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from test_probe_runtime import probes


def install_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    return module


class CredentialHelperTests(unittest.TestCase):
    def test_pool_rows_come_from_the_helper_and_skip_when_unavailable(self):
        rows = [{"id": "from-helper", "access_token": "token"}]

        def read_credential_pool(provider_id=None):
            self.assertEqual(provider_id, "openai-codex")
            return list(rows)

        auth = install_module("hermes_cli.auth", read_credential_pool=read_credential_pool)
        package = install_module("hermes_cli", auth=auth)
        for source, probe in probes():
            with self.subTest(source=source):
                self.assertFalse(hasattr(probe, "_read_auth_store"))
                self.assertFalse(hasattr(probe, "_pool_rows_from_saved_store"))
                with patch.dict(sys.modules, {"hermes_cli": package, "hermes_cli.auth": auth}):
                    self.assertEqual(probe._pool_entries("openai-codex"), rows)

                empty_auth = install_module("hermes_cli.auth", read_credential_pool=lambda provider_id=None: [])
                empty_pkg = install_module("hermes_cli", auth=empty_auth)
                with patch.dict(sys.modules, {"hermes_cli": empty_pkg, "hermes_cli.auth": empty_auth}):
                    self.assertEqual(probe._pool_entries("anthropic"), [])

                # Without the helper (or when it raises), skip the pool — never read auth.json directly.
                with tempfile.TemporaryDirectory() as tmp:
                    home = Path(tmp)
                    (home / "auth.json").write_text(json.dumps({
                        "credential_pool": {"openai-codex": [{"id": "from-file"}]}
                    }))
                    with patch.dict(sys.modules, {"hermes_cli": None, "hermes_cli.auth": None}), \
                         patch.object(probe, "_hermes_homes", return_value=[home]), \
                         patch.object(probe, "_profile_home", None):
                        self.assertEqual(probe._pool_entries("openai-codex"), [])

                    def broken_pool(provider_id=None):
                        raise RuntimeError("boom")

                    bad_auth = install_module("hermes_cli.auth", read_credential_pool=broken_pool)
                    bad_pkg = install_module("hermes_cli", auth=bad_auth)
                    with patch.dict(sys.modules, {"hermes_cli": bad_pkg, "hermes_cli.auth": bad_auth}), \
                         patch.object(probe, "_hermes_homes", return_value=[home]), \
                         patch.object(probe, "_profile_home", None):
                        self.assertEqual(probe._pool_entries("openai-codex"), [])

    def test_profile_secrets_come_from_secret_scope_and_skip_when_unavailable(self):
        for source, probe in probes():
            with self.subTest(source=source):
                self.assertFalse(hasattr(probe, "_read_env_file_value"))
                seen = {}

                def load_env_file(path):
                    seen["path"] = Path(path)
                    return {"DEEPSEEK_API_KEY": "from-helper"}

                def set_secret_scope(mapping, profile_home=None):
                    seen["scope"] = dict(mapping)
                    seen["home"] = profile_home
                    return "scope-token"

                def reset_secret_scope(token):
                    seen["reset"] = token

                def get_secret(name, default=None):
                    seen["name"] = name
                    return seen["scope"].get(name, default)

                secret_scope = install_module(
                    "agent.secret_scope",
                    load_env_file=load_env_file,
                    set_secret_scope=set_secret_scope,
                    reset_secret_scope=reset_secret_scope,
                    get_secret=get_secret,
                )
                agent = install_module("agent", secret_scope=secret_scope)
                home = Path("/tmp/resetwatch-profile")
                with patch.dict(sys.modules, {"agent": agent, "agent.secret_scope": secret_scope}), \
                     patch.object(probe, "_hermes_homes", return_value=[home]), \
                     patch.object(probe, "_profile_inherits_env", False), \
                     patch.object(probe, "_secret_source_env_value", return_value=None):
                    self.assertEqual(probe._hermes_env_value("DEEPSEEK_API_KEY"), "from-helper")
                self.assertEqual(seen["path"], home / ".env")
                self.assertEqual(seen["name"], "DEEPSEEK_API_KEY")
                self.assertEqual(seen["reset"], "scope-token")

                # Without agent.secret_scope, skip .env — never parse the file directly.
                with tempfile.TemporaryDirectory() as tmp:
                    tmp_home = Path(tmp)
                    (tmp_home / ".env").write_text("DEEPSEEK_API_KEY=from-file\n", encoding="utf-8")
                    with patch.dict(sys.modules, {"agent": None, "agent.secret_scope": None}), \
                         patch.object(probe, "_hermes_homes", return_value=[tmp_home]), \
                         patch.object(probe, "_profile_inherits_env", False), \
                         patch.object(probe, "_secret_source_env_value", return_value=None):
                        self.assertIsNone(probe._hermes_env_value("DEEPSEEK_API_KEY"))

    def test_anthropic_oauth_uses_readonly_helper_and_skips_when_unavailable(self):
        for source, probe in probes():
            with self.subTest(source=source):
                calls = []

                def read_hermes_oauth_credentials():
                    calls.append("read")
                    return {"accessToken": "sk-ant-oat01-from-helper", "refreshToken": "unused"}

                def forbidden_resolver(*args, **kwargs):
                    raise AssertionError("refreshing resolver must never be called")

                creds_mod = install_module(
                    "agent.anthropic_credentials",
                    read_hermes_oauth_credentials=read_hermes_oauth_credentials,
                    resolve_anthropic_token=forbidden_resolver,
                )
                agent = install_module("agent", anthropic_credentials=creds_mod)
                with patch.dict(sys.modules, {"agent": agent, "agent.anthropic_credentials": creds_mod}):
                    self.assertEqual(probe._hermes_anthropic_oauth_token(), "sk-ant-oat01-from-helper")
                self.assertEqual(calls, ["read"])

                # Without the helper, skip — never read .anthropic_oauth.json directly.
                with tempfile.TemporaryDirectory() as tmp:
                    home = Path(tmp)
                    (home / ".anthropic_oauth.json").write_text(json.dumps({"accessToken": "sk-ant-oat01-from-file"}))
                    with patch.dict(sys.modules, {"agent": None, "agent.anthropic_credentials": None}), \
                         patch.object(probe, "_hermes_homes", return_value=[home]):
                        self.assertIsNone(probe._hermes_anthropic_oauth_token())

    def test_standalone_codex_uses_readonly_runtime_helper_and_skips_older_hermes(self):
        for source, probe in probes():
            with self.subTest(source=source):
                seen = []

                def resolve_codex_runtime_credentials(*, read_only=False):
                    seen.append(read_only)
                    self.assertTrue(read_only)
                    return {
                        "provider": "openai-codex",
                        "api_key": "codex-standalone-token",
                        "base_url": "https://chatgpt.com/backend-api/codex",
                        "source": "hermes-auth-store",
                    }

                auth_codex = install_module(
                    "hermes_cli.auth_codex",
                    resolve_codex_runtime_credentials=resolve_codex_runtime_credentials,
                )
                pkg = install_module("hermes_cli", auth_codex=auth_codex)
                with patch.dict(sys.modules, {"hermes_cli": pkg, "hermes_cli.auth_codex": auth_codex}):
                    entry = probe._hermes_codex_runtime_entry()
                self.assertEqual(seen, [True])
                self.assertEqual(entry, {
                    "access_token": "codex-standalone-token",
                    "base_url": "https://chatgpt.com/backend-api/codex",
                })

                # Older Hermes lacking read_only= raises TypeError and must be skipped, never retried without it.
                def older_resolver(*, force_refresh=False):
                    raise AssertionError("older resolver without read_only must not run")

                old_mod = install_module("hermes_cli.auth_codex", resolve_codex_runtime_credentials=older_resolver)
                old_pkg = install_module("hermes_cli", auth_codex=old_mod)
                with patch.dict(sys.modules, {"hermes_cli": old_pkg, "hermes_cli.auth_codex": old_mod}):
                    self.assertIsNone(probe._hermes_codex_runtime_entry())

                with patch.dict(sys.modules, {"hermes_cli": None, "hermes_cli.auth_codex": None}):
                    self.assertIsNone(probe._hermes_codex_runtime_entry())

    def test_credentials_doc_describes_hermes_only_helpers(self):
        text = Path(__file__).resolve().parent.joinpath("docs", "credentials.md").read_text(encoding="utf-8")
        for phrase in (
            "read_credential_pool",
            "resolve_codex_runtime_credentials",
            "read_hermes_oauth_credentials",
            "get_secret",
            "model.options",
            "does not refresh",
        ):
            self.assertIn(phrase, text)


if __name__ == "__main__":
    unittest.main()
