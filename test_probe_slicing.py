"""Probe stdout stays parseable under the gateway's 4000-character tail cap."""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_probe_runtime import probes


class ProbePaginationTests(unittest.TestCase):
    def test_slices_advance_by_actual_rows_when_byte_budget_fills_early(self):
        rows = [{"provider": "cursor", "plan": "Go", "details": ["x" * 1300],
                 "account_label": str(index), "windows": []} for index in range(14)]
        for source, probe in probes():
            with self.subTest(source=source):
                found = []
                offset = 0
                while True:
                    with patch.object(probe, "_slice_args", (offset, 8)):
                        page = probe._apply_slice(rows)
                        out = io.StringIO()
                        probe._emit_json(page, out)
                        self.assertLessEqual(len(out.getvalue()), 3500)
                        self.assertEqual(json.loads(out.getvalue()), page)
                    if not page:
                        break
                    found.extend(page)
                    offset += len(page)
                self.assertEqual(found, rows)

    def test_single_large_row_becomes_a_bounded_visible_error_then_advances(self):
        rows = [{"provider": "cursor", "account_label": "1", "details": ["x" * 6000]},
                {"provider": "kimi", "details": ["ok"]}]
        for source, probe in probes():
            with self.subTest(source=source), patch.object(probe, "_slice_args", (0, 8)):
                page = probe._apply_slice(rows)
                self.assertEqual(page[0]["provider"], "cursor")
                self.assertEqual(page[0]["account_label"], "1")
                self.assertIn("exceeds", page[0]["error"])
                out = io.StringIO()
                probe._emit_json(page, out)
                self.assertLessEqual(len(out.getvalue()), 3500)
                with patch.object(probe, "_slice_args", (len(page), 8)):
                    self.assertEqual(probe._apply_slice(rows), rows[len(page):])

    def test_cli_reads_cached_slice_without_vendor_access(self):
        runner = '''
import importlib.util, json, socket, sys
spec = importlib.util.spec_from_file_location('tested', sys.argv[1])
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)
rows = json.loads(sys.argv[2])
probe._read_probe_result_cache = lambda **kwargs: rows
probe._collect_hermes = lambda: (_ for _ in ()).throw(AssertionError('no vendor'))
probe._collect_cli = probe._collect_hermes
socket.socket = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('network'))
sys.argv = [sys.argv[1], '--slice=8:8', '--cli-only', '--fresh']
probe._main_inner()
'''
        rows = [{"provider": "cursor", "details": ["x" * 300], "account_label": str(i)} for i in range(14)]
        for source, probe in probes():
            with self.subTest(source=source), tempfile.TemporaryDirectory() as tmp:
                env = {name: os.environ[name] for name in ("SYSTEMROOT", "WINDIR") if name in os.environ}
                env.update(HOME=tmp, USERPROFILE=tmp, HERMES_HOME=tmp)
                run = subprocess.run([sys.executable, "-I", "-c", runner, str(Path(probe.__file__ or "").resolve()),
                                      json.dumps(rows)], env=env, capture_output=True, text=True, timeout=10)
                self.assertEqual(run.returncode, 0, run.stderr)
                self.assertLessEqual(len(run.stdout), 3500)
                self.assertEqual(json.loads(run.stdout), rows[8:])

if __name__ == "__main__": unittest.main()
