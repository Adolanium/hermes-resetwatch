"""DeepSeek balances retain their currencies in both plugin distributions."""
import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx


def balance(currency, total, topped=None, granted="0.00"):
    return {"currency": currency, "total_balance": total,
            "topped_up_balance": total if topped is None else topped, "granted_balance": granted}


class DeepSeekBalanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.probes = []
        for filename in ("probe.py", "catalog/desktop/probe.py"):
            spec = importlib.util.spec_from_file_location("balance_probe", Path(__file__).parent / filename)
            probe = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(probe)
            cls.probes.append((filename, probe))

    def fetch(self, probe, rows, available=True):
        requests = []

        def respond(request):
            requests.append(request)
            self.assertEqual(request.method, "GET")
            self.assertEqual(str(request.url), "https://api.deepseek.com/user/balance")
            self.assertEqual(request.headers["Authorization"], "Bearer test-key")
            return httpx.Response(200, json={"is_available": available, "balance_infos": rows})

        client = httpx.Client(transport=httpx.MockTransport(respond))
        with patch.object(probe, "_deepseek_api_key", return_value="test-key"), patch.object(httpx, "Client", return_value=client):
            result = probe._fetch_deepseek_account_usage()
        self.assertEqual(len(requests), 1)
        return result

    def check_balances(self, rows, expected, available=True):
        for filename, probe in self.probes:
            for ordered in (rows, list(reversed(rows))):
                with self.subTest(file=filename, currencies=ordered):
                    snapshot = self.fetch(probe, ordered, available)
                    self.assertEqual(snapshot["provider"], "deepseek")
                    self.assertEqual([(w["label"], w["detail"]) for w in snapshot["windows"][:-1]], expected)
                    self.assertEqual(snapshot["windows"][-1]["label"], "Pricing")
                    self.assertIn("Mon-Fri", snapshot["windows"][-1]["detail"])

    def test_empty_usd_does_not_hide_funded_cny(self):
        self.check_balances([balance("USD", "0.00"), balance("CNY", "71.46")],
                            [("Balance (CNY)", "¥71.46 left · ¥71.46 topped up")])

    def test_empty_cny_does_not_hide_funded_usd(self):
        self.check_balances([balance("CNY", "0.00"), balance("USD", "100.00")],
                            [("Balance (USD)", "$100.00 left · $100.00 topped up")])

    def test_both_funded_currencies_keep_their_own_details(self):
        self.check_balances([balance("USD", "100.00", "90.00", "10.00"), balance("CNY", "200.00", "150.00", "50.00")],
                            [("Balance (CNY)", "¥200.00 left · ¥150.00 topped up · ¥50.00 granted"),
                             ("Balance (USD)", "$100.00 left · $90.00 topped up · $10.00 granted")])

    def test_all_zero_retains_usd_fallback(self):
        self.check_balances([balance("CNY", "0.00"), balance("USD", "0.00")],
                            [("Balance (USD)", "$0.00 left")])

    def test_single_currency_including_zero_and_debt(self):
        for amount, detail in (("0.00", "¥0.00 left"), ("-2.00", "¥-2.00 left"), ("3.00", "¥3.00 left · ¥3.00 topped up")):
            self.check_balances([balance("CNY", amount)], [("Balance (CNY)", detail)])

    def test_invalid_rows_do_not_hide_valid_balance(self):
        self.check_balances([None, "invalid", {}, balance("USD", True), balance("USD", "bad"),
                             balance("USD", "NaN"), balance("USD", "Infinity"), balance("CNY", "71.46")],
                            [("Balance (CNY)", "¥71.46 left · ¥71.46 topped up")])

    def test_no_valid_balances_returns_no_snapshot(self):
        for filename, probe in self.probes:
            for rows in (None, {}, [], [None, {}, balance("USD", "NaN")]):
                with self.subTest(file=filename, rows=rows):
                    self.assertIsNone(self.fetch(probe, rows))

    def test_unavailable_account_warning_is_preserved(self):
        self.check_balances([balance("CNY", "0.00")],
                            [("Balance (CNY)", "¥0.00 left · balance too low for new calls")], available=False)


if __name__ == "__main__":
    unittest.main()
