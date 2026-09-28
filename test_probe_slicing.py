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
        rows = [{"provider": "cursor", "account_label": "1", "account_key": "cursor:one", "details": ["x" * 6000]},
                {"provider": "kimi", "details": ["ok"]}]
        for source, probe in probes():
            with self.subTest(source=source), patch.object(probe, "_slice_args", (0, 8)):
                page = probe._apply_slice(rows)
                self.assertEqual(page[0]["provider"], "cursor")
                self.assertEqual(page[0]["account_label"], "1")
                self.assertEqual(page[0]["account_key"], "cursor:one")
                self.assertIn("exceeds", page[0]["error"])
                out = io.StringIO()
                probe._emit_json(page, out)
                self.assertLessEqual(len(out.getvalue()), 3500)
                with patch.object(probe, "_slice_args", (len(page), 8)):
                    self.assertEqual(probe._apply_slice(rows), rows[len(page):])

    def test_pinned_incomplete_fresh_pages_ignore_older_cache_and_concurrent_refresh(self):
        runner = '''
import importlib.util, json, os, sys, time
from pathlib import Path
spec = importlib.util.spec_from_file_location('tested', sys.argv[1])
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)
action, token = sys.argv[2:4]
probe._collect_hermes = lambda: []
probe._collect_cli = lambda: ([{'provider': action + str(i), 'windows': []} for i in range(12)], False)
if action == 'seed':
    probe._store_probe_result_cache([{'provider': 'old' + str(i)} for i in range(12)], cli_only=True)
    cache = probe._probe_result_cache_path(cli_only=True)
    payload = json.loads(cache.read_text())
    payload['fetched_at'] = time.time() - 120
    cache.write_text(json.dumps(payload))
    sys.exit(0)
sys.argv = [sys.argv[1], '--cli-only', '--slice=0:8', '--fresh', '--pin-snapshot'] if not token else [sys.argv[1], '--cli-only', '--slice=8:8', '--snapshot-token=' + token]
probe.main()
'''
        for source, probe in probes():
            with self.subTest(source=source), tempfile.TemporaryDirectory() as tmp:
                env = {name: os.environ[name] for name in ("SYSTEMROOT", "WINDIR") if name in os.environ}
                env.update(HOME=tmp, USERPROFILE=tmp, HERMES_HOME=tmp)
                def call(action, token=''):
                    run = subprocess.run([sys.executable, '-I', '-c', runner, str(Path(probe.__file__).resolve()), action, token],
                                         env=env, capture_output=True, text=True, timeout=10)
                    self.assertEqual(run.returncode, 0, run.stderr)
                    if action != 'seed':
                        self.assertLessEqual(len(run.stdout), 4000)
                        return json.loads(run.stdout)
                call('seed')
                first = call('new')
                second = call('other')
                self.assertEqual([row['provider'] for row in first['snapshots']], ['new' + str(i) for i in range(8)])
                self.assertEqual([row['provider'] for row in second['snapshots']], ['other' + str(i) for i in range(8)])
                self.assertNotEqual(first['snapshot_token'], second['snapshot_token'])
                for first_page, name in ((first, 'new'), (second, 'other')):
                    page = call('unused', first_page['snapshot_token'])
                    self.assertEqual(page['snapshot_token'], first_page['snapshot_token'])
                    self.assertEqual([row['provider'] for row in page['snapshots']], [name + str(i) for i in range(8, 12)])
                self.assertEqual(call('unused', first['snapshot_token'])['snapshots'][0]['provider'], 'new8')
                # Malformed and nonexistent pins must never read an older cache or query vendors.
                for bad in ('../../oops', 'f' * 32):
                    failed = call('unused', bad)
                    self.assertNotIn('snapshot_token', failed)
                    self.assertIn('probe failed', str(failed))

    def test_shared_cache_expiry_and_replacement_do_not_mutate_pins(self):
        for source, probe in probes():
            with self.subTest(source=source), tempfile.TemporaryDirectory() as tmp, \
                 patch.object(probe, '_resetwatch_cache_dir', return_value=Path(tmp)):
                first = [{'provider': 'cached-' + str(i)} for i in range(12)]
                probe._store_probe_result_cache(first, cli_only=True)
                cached_rows = probe._read_probe_result_cache(cli_only=True)
                self.assertEqual(cached_rows, first)
                token = probe._pin_probe_snapshots(cached_rows)
                cache = probe._probe_result_cache_path(cli_only=True)
                cached = json.loads(cache.read_text())
                cached['fetched_at'] -= 301
                cache.write_text(json.dumps(cached))
                self.assertIsNone(probe._read_probe_result_cache(cli_only=True))
                probe._store_probe_result_cache([{'provider': 'replacement'}], cli_only=True)
                self.assertEqual(probe._read_pinned_snapshots(token), first)
                self.assertEqual(probe._read_probe_result_cache(cli_only=True), [{'provider': 'replacement'}])

    def test_expired_pin_is_not_replaced_by_shared_cache(self):
        for source, probe in probes():
            with self.subTest(source=source), tempfile.TemporaryDirectory() as tmp, \
                 patch.object(probe, '_resetwatch_cache_dir', return_value=Path(tmp)):
                token = probe._pin_probe_snapshots([{'provider': 'first'}])
                path = Path(tmp) / f'probe_page.{token}.json'
                if os.name == 'posix':
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                os.utime(path, (1, 1))
                with self.assertRaises(ValueError):
                    probe._read_pinned_snapshots(token)
                with self.assertRaises(ValueError):
                    probe._read_pinned_snapshots('../' + token)
                other = Path(tmp) / 'other-profile'
                other.mkdir()
                with patch.object(probe, '_resetwatch_cache_dir', return_value=other):
                    with self.assertRaises(ValueError):
                        probe._read_pinned_snapshots(token)
                second = probe._pin_probe_snapshots([{'provider': 'second'}])
                self.assertNotEqual(second, token)
                self.assertEqual(probe._read_pinned_snapshots(second), [{'provider': 'second'}])

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
