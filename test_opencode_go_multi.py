"""OpenCode Go subscriptions remain separate, without printing their keys."""
import io
import sys
import types
import unittest
from unittest.mock import patch

from test_probe_runtime import probes


class OpenCodeGoMultiTests(unittest.TestCase):
    def test_distinct_contiguous_keys_and_duplicate_are_separate_accounts(self):
        values = {"OPENCODE_GO_API_KEY": "fake-first", "OPENCODE_GO_API_KEY_2": "fake-second",
                  "OPENCODE_GO_API_KEY_3": "fake-second", "OPENCODE_GO_API_KEY_4": "fake-fourth"}
        for source, probe in probes():
            with self.subTest(source=source), patch.object(probe, "_hermes_env_value", side_effect=values.get):
                self.assertEqual(probe._opencode_go_api_keys(),
                                 [("fake-first", "1"), ("fake-second", "2"), ("fake-fourth", "4")])

    def test_fetch_collects_usage_and_errors_under_the_correct_account(self):
        values = {"OPENCODE_GO_API_KEY": "fake-first", "OPENCODE_GO_API_KEY_2": "fake-second"}
        requested = []
        class Response:
            def __init__(self, token): self.token = token
            def raise_for_status(self):
                if self.token == "fake-second": raise ValueError("offline")
            def json(self): return {"usage": {"rolling": {"percent": 25, "resetsAt": None}}}
        class Client:
            def __init__(self, **kwargs): pass
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def get(self, url, headers):
                requested.append(headers["Authorization"])
                return Response(headers["Authorization"].split()[-1])
        fake_httpx = types.SimpleNamespace(
            Client=Client,
            HTTPStatusError=type("HTTPStatusError", (Exception,), {}),
            TimeoutException=type("TimeoutException", (Exception,), {}),
            TransportError=type("TransportError", (Exception,), {}),
        )
        for source, probe in probes():
            requested.clear()
            with self.subTest(source=source), patch.object(probe, "_hermes_env_value", side_effect=values.get), \
                 patch.object(probe, "_disabled_providers", set(probe.LIVE_PROVIDERS) - {"opencode-go"}), \
                 patch.dict(sys.modules, {"httpx": fake_httpx}):
                snapshots, complete = probe._collect_cli()
                self.assertTrue(complete)
                self.assertEqual({s.get("account_label") for s in snapshots}, {"1", "2"})
                self.assertEqual(len(snapshots), 2)
                self.assertIn("25", str(snapshots[0]["windows"]))
                self.assertIn("offline", str(snapshots[1]))
                self.assertEqual(set(requested), {"Bearer fake-first", "Bearer fake-second"})
                output = io.StringIO()
                probe._emit_json(snapshots, output)
                self.assertNotIn("fake-first", output.getvalue())
                self.assertNotIn("fake-second", output.getvalue())

    def test_placeholder_primary_does_not_replace_numbered_accounts(self):
        values = {"OPENCODE_GO_API_KEY": "none", "OPENCODE_GO_API_KEY_2": "fake-second",
                  "OPENCODE_GO_API_KEY_3": "fake-third"}
        for source, probe in probes():
            with self.subTest(source=source), patch.object(probe, "_hermes_env_value", side_effect=values.get):
                self.assertEqual(probe._opencode_go_api_keys(),
                                 [("fake-second", "2"), ("fake-third", "3")])

    def test_single_key_keeps_unlabelled_legacy_card(self):
        for source, probe in probes():
            with self.subTest(source=source), patch.object(probe, "_hermes_env_value", side_effect=lambda name: "fake-only" if name == "OPENCODE_GO_API_KEY" else None):
                self.assertEqual(probe._opencode_go_api_keys(), [("fake-only", None)])

if __name__ == "__main__": unittest.main()
