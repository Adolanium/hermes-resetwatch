"""Ollama Cloud cards read /api/balance in both plugin distributions."""
import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

LEGACY = {
    "included": {
        "session": {"remaining_percent": 90.71, "resets_at": "2026-10-07T18:00:00Z"},
        "weekly": {"remaining_percent": 88.01, "resets_at": "2026-10-12T00:00:00Z"},
    },
    "purchased": {"balance_usd": 0},
}
CURRENT = {
    "included": {
        "balance_usd": 72.5,
        "allowance_usd": 100,
        "period": {"from": "2026-09-15T09:30:00Z", "until": "2026-10-15T09:30:00Z"},
    },
    "purchased": {"balance_usd": 25},
}


def row(label, used, reset=None, detail=None):
    return {
        "label": label, "used_percent": used,
        "remaining_percent": None if used is None else 100.0 - used,
        "reset_at": reset, "detail": detail,
    }


class OllamaBalanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.probes = []
        for filename in ("probe.py", "catalog/desktop/probe.py"):
            spec = importlib.util.spec_from_file_location("ollama_probe", Path(__file__).parent / filename)
            probe = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(probe)
            cls.probes.append((filename, probe))

    def fetch(self, probe, balance, status=200, me=None):
        requests = []

        def respond(request):
            requests.append((request.method, str(request.url)))
            self.assertEqual(request.headers["Authorization"], "Bearer test-key")
            if request.url.path == "/api/me":
                return httpx.Response(200, json=me or {})
            return httpx.Response(status, json=balance)

        client = httpx.Client(transport=httpx.MockTransport(respond))
        self.addCleanup(client.close)
        with patch.object(probe, "_ollama_api_key", return_value="test-key"), \
             patch.object(httpx, "Client", return_value=client):
            try:
                return probe._fetch_ollama_cloud_account_usage()
            finally:
                self.assertEqual(requests, [
                    ("POST", "https://ollama.com/api/me"),
                    ("GET", "https://ollama.com/api/balance"),
                ])

    def test_session_and_weekly_limits_show_used_percent_and_reset_times(self):
        for filename, probe in self.probes:
            with self.subTest(file=filename):
                snapshot = self.fetch(probe, LEGACY, me={"Plan": "pro"})
                self.assertEqual(snapshot["provider"], "ollama")
                self.assertEqual(snapshot["plan"], "Pro")
                self.assertEqual(len(snapshot["windows"]), 2)
                for window, (label, used, reset) in zip(snapshot["windows"], (
                    ("5h", 100.0 - 90.71, "2026-10-07T18:00:00+00:00"),
                    ("Weekly", 100.0 - 88.01, "2026-10-12T00:00:00+00:00"),
                )):
                    self.assertEqual(window["label"], label)
                    self.assertAlmostEqual(window["used_percent"], used)
                    self.assertEqual(window["reset_at"], reset)
                    self.assertIsNone(window["detail"])

    def test_included_allowance_and_purchased_credits(self):
        for filename, probe in self.probes:
            with self.subTest(file=filename):
                self.assertEqual(self.fetch(probe, CURRENT)["windows"], [
                    row("Monthly", 27.5, "2026-10-15T09:30:00+00:00", "$27.50 of $100.00 used"),
                    row("Purchased", None, None, "$25.00 left"),
                ])

    def test_purchased_credits_beside_session_limits(self):
        body = {**LEGACY, "purchased": {"balance_usd": 4.2}}
        for filename, probe in self.probes:
            with self.subTest(file=filename):
                self.assertEqual(self.fetch(probe, body)["windows"][-1], row("Purchased", None, None, "$4.20 left"))

    def test_balance_above_allowance_counts_as_unused(self):
        body = {**CURRENT, "included": {**CURRENT["included"], "balance_usd": 120}}
        for filename, probe in self.probes:
            with self.subTest(file=filename):
                self.assertEqual(self.fetch(probe, body)["windows"][0]["used_percent"], 0.0)

    def test_account_without_allowance_or_credits_has_no_card(self):
        body = {"included": {**CURRENT["included"], "balance_usd": 0, "allowance_usd": 0},
                "purchased": {"balance_usd": 0}}
        for filename, probe in self.probes:
            with self.subTest(file=filename):
                self.assertIsNone(self.fetch(probe, body))

    def test_out_of_range_percent_drops_only_that_row(self):
        body = {"included": {"session": {"remaining_percent": 0.9}, "weekly": {"remaining_percent": 150}},
                "purchased": {"balance_usd": 0}}
        for filename, probe in self.probes:
            with self.subTest(file=filename):
                self.assertEqual([w["label"] for w in self.fetch(probe, body)["windows"]], ["5h"])

    def test_unreadable_limits_raise_instead_of_hiding_the_card(self):
        bodies = [
            # The /api/usage accounting shape that replaced the old limits.
            {"range": "7d", "totals": {"request_count": 2558}, "buckets": []},
            {"included": {"session": {"remaining_percent": True}, "weekly": {"remaining_percent": "88"}},
             "purchased": {"balance_usd": 0}},
            [],
        ]
        for filename, probe in self.probes:
            with self.subTest(file=filename):
                for body in bodies:
                    with self.subTest(body=body), self.assertRaises(ValueError):
                        self.fetch(probe, body)

    def test_http_errors_raise(self):
        for filename, probe in self.probes:
            with self.subTest(file=filename):
                for status in (401, 429, 503):
                    with self.subTest(status=status), self.assertRaises(httpx.HTTPStatusError):
                        self.fetch(probe, {"error": "nope"}, status=status)


if __name__ == "__main__":
    unittest.main()
