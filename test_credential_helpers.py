"""Hermes credential helpers stand in front of direct file reads."""
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import probe


def install_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    return module


class CredentialHelperTests(unittest.TestCase):
    def test_pool_rows_come_from_the_helper_without_opening_the_store(self):
        rows = [{"id": "from-helper", "access_token": "token"}]

        def read_credential_pool(provider_id=None):
            self.assertEqual(provider_id, "openai-codex")
            return list(rows)

        auth = install_module("hermes_cli.auth", read_credential_pool=read_credential_pool)
        package = install_module("hermes_cli", auth=auth)
        with patch.dict(sys.modules, {"hermes_cli": package, "hermes_cli.auth": auth}), \
             patch.object(probe, "_read_auth_store", side_effect=AssertionError("auth store opened")):
            self.assertEqual(probe._pool_entries("openai-codex"), rows)

    def test_an_empty_pool_answer_does_not_open_the_store(self):
        auth = install_module("hermes_cli.auth", read_credential_pool=lambda provider_id=None: [])
        package = install_module("hermes_cli", auth=auth)
        with patch.dict(sys.modules, {"hermes_cli": package, "hermes_cli.auth": auth}), \
             patch.object(probe, "_read_auth_store", side_effect=AssertionError("auth store opened")):
            self.assertEqual(probe._pool_entries("anthropic"), [])

    def test_saved_pool_is_read_only_when_the_helper_cannot_be_reached(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "auth.json").write_text(json.dumps({
                "credential_pool": {"openai-codex": [{"id": "from-file"}]}
            }))
            with patch.object(probe, "_hermes_homes", return_value=[home]), \
                 patch.object(probe, "_profile_home", None):
                self.assertEqual([row["id"] for row in probe._pool_entries("openai-codex")], ["from-file"])

    def test_profile_secrets_come_from_the_helper_without_the_local_parser(self):
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
             patch.object(probe, "_secret_source_env_value", return_value=None), \
             patch.object(probe, "_read_env_file_value", side_effect=AssertionError("dotenv parser")):
            self.assertEqual(probe._hermes_env_value("DEEPSEEK_API_KEY"), "from-helper")
        self.assertEqual(seen["path"], home / ".env")
        self.assertEqual(seen["name"], "DEEPSEEK_API_KEY")
        self.assertEqual(seen["reset"], "scope-token")

    def test_dotenv_parser_remains_when_the_helper_cannot_be_imported(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / ".env").write_text("DEEPSEEK_API_KEY=from-file\n", encoding="utf-8")
            with patch.dict(sys.modules, {"agent": None, "agent.secret_scope": None}), \
                 patch.object(probe, "_hermes_homes", return_value=[home]), \
                 patch.object(probe, "_profile_inherits_env", False), \
                 patch.object(probe, "_secret_source_env_value", return_value=None):
                self.assertEqual(probe._hermes_env_value("DEEPSEEK_API_KEY"), "from-file")

    def test_oauth_singleton_stays_a_direct_read_and_is_explained(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / ".anthropic_oauth.json").write_text(json.dumps({"access_token": "sk-ant-oat01-test"}))
            with patch.object(probe, "_hermes_homes", return_value=[home]):
                self.assertEqual(probe._hermes_anthropic_oauth_token(), "sk-ant-oat01-test")
        text = Path(__file__).resolve().parent.joinpath("docs", "credentials.md").read_text(encoding="utf-8")
        for phrase in (
            "read_credential_pool",
            "get_secret",
            ".anthropic_oauth.json",
            "model.options",
            "does not refresh",
        ):
            self.assertIn(phrase, text)


if __name__ == "__main__":
    unittest.main()
