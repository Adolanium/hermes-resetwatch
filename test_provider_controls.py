"""Disabled providers must never reach credential discovery or vendor fetches."""
import sys
import contextlib
import json
import os
import subprocess
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from test_probe_runtime import probes


class ProviderControlsTests(unittest.TestCase):
    def test_command_line_filters_before_running_fetchers(self):
        runner = '''
import importlib.util, json, socket, sys
spec = importlib.util.spec_from_file_location("tested", sys.argv[1])
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)
def blocked(*args, **kwargs):
    raise AssertionError("unexpected vendor access")
socket.socket = blocked
probe._collect_hermes = blocked
probe._codex_pool_accounts = blocked
probe._codex_cli_access_context = blocked
probe._read_probe_result_cache = lambda **kwargs: None
probe._emit_json = lambda snapshots, stream: stream.write(json.dumps(snapshots) + "\\n")
probe._wait_for_credential_writes = lambda: None
class Done(BaseException): pass
def done(code): raise Done()
probe.os._exit = done
sys.argv = sys.argv[1:]
try: probe._main_inner()
except Done: pass
'''
        for source, probe in probes():
            with self.subTest(source=source), tempfile.TemporaryDirectory() as tmp:
                env = {key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR") if key in os.environ}
                env.update(HOME=tmp, USERPROFILE=tmp, HERMES_HOME=tmp)
                result = subprocess.run([sys.executable, "-I", "-c", runner, str(Path(probe.__file__).resolve()),
                                         "--cli-only", "--fresh", "--disabled-providers=" + ",".join(probe.LIVE_PROVIDERS)],
                                        env=env, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout), [])

    def test_disabled_fetchers_and_codex_credentials_are_not_called(self):
        for source, probe in probes():
            with self.subTest(source=source), contextlib.ExitStack() as stack, patch.dict(sys.modules, {"httpx": types.ModuleType("httpx")}), \
                 patch.object(probe, "_disabled_providers", set(probe.LIVE_PROVIDERS) - {"cursor"}, create=True), \
                 patch.object(probe, "_codex_pool_accounts", side_effect=AssertionError("disabled account lookup")), \
                 patch.object(probe, "_codex_cli_access_context", side_effect=AssertionError("disabled CLI lookup")), \
                 patch.object(probe, "_fetch_cursor_account_usage", return_value={"provider": "cursor", "details": ["quota"]}) as fetch:
                blocked = []
                for name in vars(probe):
                    if name.startswith("_fetch_") and name != "_fetch_cursor_account_usage":
                        blocked.append(stack.enter_context(patch.object(probe, name, side_effect=AssertionError("disabled vendor"))))
                snapshots, complete = probe._collect_cli()
                self.assertEqual([s["provider"] for s in snapshots], ["cursor"])
                self.assertTrue(complete)
                fetch.assert_called_once()
                for vendor in blocked:
                    vendor.assert_not_called()

    def test_all_disabled_needs_no_httpx(self):
        for source, probe in probes():
            with self.subTest(source=source), patch.dict(sys.modules, {"httpx": None}), \
                 patch.object(probe, "_disabled_providers", set(probe.LIVE_PROVIDERS), create=True):
                self.assertEqual(probe._collect_cli(), ([], True))

    def test_hermes_fetchers_obey_disabled_providers(self):
        for source, probe in probes():
            fetch = Mock()
            module = types.ModuleType("agent.account_usage")
            module.fetch_account_usage = fetch
            with self.subTest(source=source), patch.dict(sys.modules, {"agent": types.ModuleType("agent"), "agent.account_usage": module}), \
                 patch.object(probe, "_disabled_providers", {"openrouter", "openai-codex"}, create=True):
                self.assertEqual(probe._collect_hermes_usage(), [])
                fetch.assert_not_called()

    def test_cache_does_not_cross_provider_selections(self):
        for source, probe in probes():
            with self.subTest(source=source), tempfile.TemporaryDirectory() as tmp, \
                 patch.object(probe, "_probe_result_cache_path", return_value=Path(tmp) / "cache.json"):
                snapshots = [{"provider": "cursor", "details": ["quota"]}]
                probe._store_probe_result_cache(snapshots, cli_only=True)
                with patch.object(probe, "_disabled_providers", {"cursor"}, create=True):
                    self.assertIsNone(probe._read_probe_result_cache(cli_only=True))
                    probe._store_probe_result_cache([], cli_only=True)
                    self.assertEqual(probe._read_probe_result_cache(cli_only=True), [])
                self.assertIsNone(probe._read_probe_result_cache(cli_only=True))


if __name__ == "__main__":
    unittest.main()
