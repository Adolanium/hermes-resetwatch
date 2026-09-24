"""Grok billing responses must keep signed-in accounts visible with the right usage."""
import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
import probe


SPEC = importlib.util.spec_from_file_location(
    "catalog_grok_probe", Path(__file__).parent / "catalog/desktop/probe.py"
)
catalog_probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(catalog_probe)

RESET = "2026-09-30T14:17:12.715439+00:00"
UNIFIED_BILLING = {"config": {
    "currentPeriod": {
        "type": "USAGE_PERIOD_TYPE_WEEKLY",
        "start": "2026-09-23T14:17:12.715439+00:00",
        "end": RESET,
    },
    "onDemandCap": {"val": 0},
    "onDemandUsed": {"val": 0},
    "isUnifiedBillingUser": True,
    "prepaidBalance": {"val": 0},
    "topUpMethod": "TOP_UP_METHOD_SAVED_PAYMENT_METHOD",
    "billingPeriodStart": "2026-09-23T14:17:12.715439+00:00",
    "billingPeriodEnd": RESET,
}}
UNIFIED_BILLING_USED = {"config": {
    **UNIFIED_BILLING["config"],
    "creditUsagePercent": 9.0,
    "productUsage": [{"product": "GrokBuild", "usagePercent": 9.0}],
}}
ZERO_USAGE = "No usage recorded yet this period"


def row(label, used, reset=RESET, detail=None):
    return {
        "label": label, "used_percent": used,
        "remaining_percent": None if used is None else 100.0 - used,
        "reset_at": reset, "detail": detail,
    }


class GrokUsageTests(unittest.TestCase):
    def fetch(self, module, payload, settings=None, context=("test-token", "test-user")):
        def respond(request):
            if request.url.path.endswith("/settings"):
                return httpx.Response(200, json=settings or {})
            return httpx.Response(200, json=payload)

        client = httpx.Client(transport=httpx.MockTransport(respond))
        self.addCleanup(client.close)
        with patch.object(httpx, "Client", return_value=client), \
             patch.object(module, "_grok_access_context", return_value=context):
            return module._fetch_grok_account_usage()

    def test_unified_billing_without_usage_reads_as_zero(self):
        for module in (probe, catalog_probe):
            with self.subTest(module=module.__name__):
                self.assertEqual(
                    self.fetch(module, UNIFIED_BILLING, {"subscription_tier_display": " SuperGrok "}),
                    {"provider": "grok", "plan": "SuperGrok", "details": [],
                     "windows": [row("Weekly", 0.0, detail=ZERO_USAGE)]},
                )

    def test_unified_billing_reports_recorded_usage(self):
        for module in (probe, catalog_probe):
            with self.subTest(module=module.__name__):
                self.assertEqual(self.fetch(module, UNIFIED_BILLING_USED)["windows"],
                                 [row("Weekly", 9.0), row("Build", 9.0)])

    def test_period_without_usage_reads_as_zero_beside_other_rows(self):
        cases = [
            ({"currentPeriod": {"type": "MONTHLY", "end": RESET}}, [row("Monthly", 0.0, detail=ZERO_USAGE)]),
            ({**UNIFIED_BILLING["config"], "prepaidBalance": {"val": 2500}},
             [row("Weekly", 0.0, detail=ZERO_USAGE), row("Prepaid", None, None, "$25.00 left")]),
        ]
        for module in (probe, catalog_probe):
            for config, windows in cases:
                with self.subTest(module=module.__name__, config=config):
                    self.assertEqual(self.fetch(module, {"config": config})["windows"], windows)

    def test_responses_without_period_have_no_meter(self):
        cases = [
            ({"subscriptionTier": "SuperGrok"}, "SuperGrok", None),
            ({"config": {"billingPeriodEnd": RESET, "monthlyLimit": {"val": 0}, "used": {"val": 0}}}, None, RESET),
        ]
        for module in (probe, catalog_probe):
            for payload, plan, reset in cases:
                with self.subTest(module=module.__name__, payload=payload):
                    self.assertEqual(self.fetch(module, payload), {
                        "provider": "grok", "plan": plan, "details": [],
                        "windows": [row("Weekly", None, reset, "No metered limits on this plan")],
                    })

    def test_legacy_meter_and_prepaid_remain_unchanged(self):
        cases = [
            ({"creditUsagePercent": 30.0}, "Weekly", 30.0, 70.0, None),
            ({"prepaidBalance": {"val": 2500}}, "Prepaid", None, None, "$25.00 left"),
            ({"monthlyLimit": {"val": 1000}, "used": {"val": 250}}, "Weekly", 25.0, 75.0, "$2.50 of $10.00 used"),
        ]
        for module in (probe, catalog_probe):
            for config, label, used, remaining, detail in cases:
                with self.subTest(module=module.__name__, config=config):
                    self.assertEqual(self.fetch(module, {"config": config})["windows"], [{
                        "label": label, "used_percent": used, "remaining_percent": remaining,
                        "reset_at": None, "detail": detail,
                    }])

    def test_unrecognized_payloads_and_missing_login_stay_silent(self):
        for module in (probe, catalog_probe):
            for payload in ({}, {"garbage": True}, {"subscriptionTier": " "},
                            {"subscriptionTier": 42}, {"config": {"billingPeriodEnd": "bad-date"}}, [1]):
                with self.subTest(module=module.__name__, payload=payload):
                    self.assertIsNone(self.fetch(module, payload))
            with self.subTest(module=module.__name__, context=None):
                self.assertIsNone(self.fetch(module, UNIFIED_BILLING, context=None))


if __name__ == "__main__":
    unittest.main()
