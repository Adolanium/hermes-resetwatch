"""Gateway interpreter selection and recovery from old dependency-error caches."""
import importlib.util
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


def probes():
    for source in ("probe.py", "catalog/desktop/probe.py"):
        spec = importlib.util.spec_from_file_location("runtime_probe", Path(__file__).parent / source)
        probe = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(probe)
        yield source, probe


MISSING = {"provider": "resetwatch", "details": ["probe cannot import httpx: No module named 'httpx'"]}


class ProbeRuntimeTests(unittest.TestCase):
    def test_dependency_failure_is_not_a_complete_collection(self):
        for source, probe in probes():
            with self.subTest(source=source), patch.dict("sys.modules", {"httpx": None}):
                snapshots, complete = probe._collect_cli()
                self.assertIn("cannot import httpx", str(snapshots))
                self.assertFalse(complete)

    def test_old_dependency_caches_are_ignored_and_not_written(self):
        for source, probe in probes():
            for cli_only in (False, True):
                for snapshots in ([MISSING], [{"provider": "nous"}, MISSING]):
                    with self.subTest(source=source, cli_only=cli_only, snapshots=snapshots), tempfile.TemporaryDirectory() as tmp:
                        cache = Path(tmp) / "snapshots.json"
                        cache.write_text(json.dumps({"fetched_at": time.time(), "snapshots": snapshots}))
                        with patch.object(probe, "_probe_result_cache_path", return_value=cache):
                            self.assertIsNone(probe._read_probe_result_cache(cli_only=cli_only))
                            cache.unlink()
                            probe._store_probe_result_cache(snapshots, cli_only=cli_only)
                            self.assertFalse(cache.exists())

    def test_vendor_error_caches_still_work(self):
        snapshots = [{"provider": "anthropic", "details": ["HTTP 429"]}]
        for source, probe in probes():
            with self.subTest(source=source), tempfile.TemporaryDirectory() as tmp:
                with patch.object(probe, "_probe_result_cache_path", return_value=Path(tmp) / "cache.json"):
                    probe._store_probe_result_cache(snapshots, cli_only=True)
                    self.assertEqual(probe._read_probe_result_cache(cli_only=True), snapshots)

    def test_parent_runtime_keeps_venv_path_and_ignores_process_arguments(self):
        for source, probe in probes():
            with self.subTest(source=source), tempfile.TemporaryDirectory() as tmp:
                proc = Path(tmp)
                for pid, parent, argv in ((30, 20, ["/bin/sh", "-c", "command"]),
                                          (20, 1, ["/opt/custom install/venv/bin/python3", "-m", "tui_gateway", "secret-argument"])):
                    folder = proc / str(pid)
                    folder.mkdir()
                    (folder / "cmdline").write_bytes(b"\0".join(arg.encode() for arg in argv))
                    (folder / "status").write_text(f"Name:\tprocess\nPPid:\t{parent}\n")
                with patch.dict(os.environ, {}, clear=True), patch.object(probe.os, "getppid", return_value=30):
                    self.assertEqual(probe._gateway_python_candidates(proc), ["/opt/custom install/venv/bin/python3"])

    def test_backend_overrides_and_missing_proc(self):
        for source, probe in probes():
            with self.subTest(source=source), tempfile.TemporaryDirectory() as tmp:
                runtime = str(Path(tmp) / "custom python")
                with patch.dict(os.environ, {"HERMES_PYTHON": runtime}, clear=True):
                    self.assertEqual(probe._gateway_python_candidates(Path(tmp)), [runtime])

    def test_container_gateway_at_pid_one_is_discovered(self):
        for source, probe in probes():
            with self.subTest(source=source), tempfile.TemporaryDirectory() as tmp:
                folder = Path(tmp) / "1"
                folder.mkdir()
                (folder / "cmdline").write_bytes(b"/usr/local/lib/hermes-agent/venv/bin/python3\0-m\0tui_gateway")
                (folder / "status").write_text("PPid:\t0\n")
                with patch.dict(os.environ, {}, clear=True), patch.object(probe.os, "getppid", return_value=1):
                    self.assertEqual(probe._gateway_python_candidates(Path(tmp)), ["/usr/local/lib/hermes-agent/venv/bin/python3"])

    def test_failed_candidates_fall_back_and_reexec_preserves_profile_flags(self):
        class ReplacedProcess(BaseException):
            pass

        for source, probe in probes():
            with self.subTest(source=source):
                flags = [source, "--profile", "worker", "--cli-only", "--fresh", "--disabled-providers=cursor"]
                with patch.object(probe, "_gateway_python_candidates", return_value=["/missing", "/slow", "/no-deps", "/custom/venv/bin/python3"]), \
                     patch.object(probe.sys, "argv", flags), \
                     patch.object(probe.subprocess, "run", side_effect=[OSError(), subprocess.TimeoutExpired("python", 3), SimpleNamespace(returncode=1), SimpleNamespace(returncode=0)]) as check, \
                     patch.object(probe.os, "execv", side_effect=ReplacedProcess) as replace:
                    with self.assertRaises(ReplacedProcess):
                        probe._use_gateway_python()
                    replace.assert_called_once_with("/custom/venv/bin/python3", ["/custom/venv/bin/python3", str(Path(probe.__file__).absolute()), *flags[1:]])
                    self.assertTrue(all(call.kwargs["timeout"] <= 3 for call in check.call_args_list))

    def test_vendor_wait_ends_before_the_gateway_shell_limit(self):
        # shell.exec kills the probe at 30 s and returns no output, so a slow
        # vendor must cost its own card, not the whole page.
        for source, probe in probes():
            release = threading.Event()
            started_ago = probe.SHELL_EXEC_TIMEOUT_SECONDS - probe.EXIT_RESERVE_SECONDS - 0.3
            others = set(probe.LIVE_PROVIDERS) - {"anthropic"}
            with self.subTest(source=source), \
                 patch.object(probe, "_STARTED_AT", time.time() - started_ago, create=True), \
                 patch.object(probe, "_disabled_providers", others), \
                 patch.object(probe, "_fetch_claude_accounts_usage", side_effect=lambda: release.wait(10)):
                try:
                    began = time.monotonic()
                    snapshots, complete = probe._collect_cli()
                    elapsed = time.monotonic() - began
                finally:
                    release.set()
                self.assertLess(elapsed, 2)
                self.assertFalse(complete)
                self.assertEqual([snap["provider"] for snap in snapshots], ["anthropic"])
                self.assertIn("no reply", snapshots[0]["error"])

    def test_reexec_keeps_the_first_start_time_and_ignores_stale_values(self):
        class ReplacedProcess(BaseException):
            pass

        for source, probe in probes():
            with self.subTest(source=source), patch.dict(os.environ), \
                 patch.object(probe, "_STARTED_AT", time.time() - 6, create=True), \
                 patch.object(probe, "_gateway_python_candidates", return_value=["/gateway/python"]), \
                 patch.object(probe.subprocess, "run", return_value=SimpleNamespace(returncode=0)), \
                 patch.object(probe.os, "execv", side_effect=ReplacedProcess):
                with self.assertRaises(ReplacedProcess):
                    probe._use_gateway_python()
                parent_start = probe._STARTED_AT
                replaced = dict(probes())[source]
                self.assertAlmostEqual(replaced._STARTED_AT, parent_start, places=3)
                self.assertLess(replaced._seconds_left(), probe.SHELL_EXEC_TIMEOUT_SECONDS - 5)

            for stale in (str(time.time() - 3600), str(time.time() + 3600), "not-a-time"):
                with self.subTest(source=source, stale=stale), patch.dict(os.environ, {"RESETWATCH_PROBE_STARTED_AT": stale}):
                    fresh = dict(probes())[source]
                    self.assertGreater(fresh._seconds_left(), probe.SHELL_EXEC_TIMEOUT_SECONDS - 1)

    def test_current_python_without_httpx_does_not_hide_gateway_candidate(self):
        for source, probe in probes():
            with self.subTest(source=source), patch.dict("sys.modules", {"httpx": None}), \
                 patch.object(probe, "_gateway_python_candidates", return_value=[probe.sys.executable, "/gateway/python"]), \
                 patch.object(probe.subprocess, "run", return_value=SimpleNamespace(returncode=1)) as check:
                probe._use_gateway_python()
                self.assertEqual(check.call_args.args[0][0], "/gateway/python")


if __name__ == "__main__":
    unittest.main()
