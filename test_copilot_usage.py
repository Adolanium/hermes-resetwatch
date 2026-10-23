"""GitHub Copilot cards read copilot_internal/user in both plugin distributions."""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import httpx

ROOT = Path(__file__).parent
URL = "https://api.github.com/copilot_internal/user"
TOKEN = "gho_resetwatch_test"

# Public response shapes, with the account name replaced. Sources are listed
# in the Copilot notes that shipped with this change.
PRO_CREDITS = {
    "login": "octocat",
    "access_type_sku": "monthly_subscriber_quota",
    "copilot_plan": "individual",
    "token_based_billing": True,
    "quota_reset_date": "2026-12-01",
    "quota_reset_date_utc": "2026-11-01T00:00:00.000Z",
    "quota_snapshots": {
        "chat": {"unlimited": True, "entitlement": 0, "percent_remaining": 100.0, "quota_remaining": 0},
        "completions": {"unlimited": True, "entitlement": 0, "percent_remaining": 100.0},
        "premium_interactions": {
            "unlimited": False,
            "entitlement": 1500,
            "quota_remaining": 600,
            "remaining": 600,
            "percent_remaining": 40,
            "overage_count": 0,
            "overage_permitted": False,
            "token_based_billing": True,
        },
    },
}
LEGACY_PRO_PLUS = {
    "access_type_sku": "plus_monthly_subscriber_quota",
    "copilot_plan": "individual_pro",
    "chat_enabled": True,
    "quota_reset_date": "2026-02-01",
    "quota_snapshots": {
        "chat": {
            "entitlement": 0, "overage_count": 0, "overage_permitted": False,
            "percent_remaining": 100.0, "quota_remaining": 0.0, "remaining": 0, "unlimited": True,
        },
        "completions": {
            "entitlement": 0, "overage_count": 0, "overage_permitted": False,
            "percent_remaining": 100.0, "quota_remaining": 0.0, "remaining": 0, "unlimited": True,
        },
        "premium_interactions": {
            "entitlement": 1500, "overage_count": 0, "overage_permitted": False,
            "percent_remaining": 88.504, "quota_remaining": 1327.56, "remaining": 1327, "unlimited": False,
        },
    },
}
BUSINESS_CREDITS = {
    "login": "octocat",
    "access_type_sku": "copilot_for_business_seat_quota",
    "copilot_plan": "business",
    "token_based_billing": True,
    "quota_reset_date": "2026-09-01",
    "quota_snapshots": {
        "chat": {"unlimited": True, "token_based_billing": True, "credits_used": 0, "entitlement": 0, "percent_remaining": 100.0},
        "completions": {"unlimited": True, "token_based_billing": True, "credits_used": 0, "entitlement": 0, "percent_remaining": 100.0},
        "premium_interactions": {
            "unlimited": True, "token_based_billing": True, "credits_used": 2111,
            "entitlement": 0, "overage_permitted": True, "overage_count": 0, "percent_remaining": 100.0,
        },
    },
}
FREE_CURRENT = {
    "copilot_plan": "individual",
    "access_type_sku": "free_limited_copilot",
    "token_based_billing": True,
    "quota_reset_date": "2099-07-01",
    "quota_snapshots": {
        "chat": {"entitlement": 200, "remaining": 182, "percent_remaining": 91.0, "overage_permitted": False, "token_based_billing": True},
        "completions": {"entitlement": 2000, "remaining": 1989, "percent_remaining": 99.4, "overage_permitted": False, "token_based_billing": True},
        "premium_interactions": {"entitlement": 0, "remaining": 0, "percent_remaining": 0.0, "overage_permitted": False, "token_based_billing": True},
    },
}
FREE_LEGACY = {
    "copilot_plan": "individual",
    "limited_user_quotas": {"chat": 250, "completions": 2000},
    "monthly_quotas": {"chat": 500, "completions": 4000},
    "limited_user_reset_date": "2099-02-15",
}


def _window(snapshot, label):
    matches = [item for item in snapshot["windows"] if item["label"] == label]
    return matches[0] if matches else None


class CopilotUsageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.probes = []
        for index, filename in enumerate(("probe.py", "catalog/desktop/probe.py")):
            spec = importlib.util.spec_from_file_location(f"copilot_probe_{index}", ROOT / filename)
            probe = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(probe)
            cls.probes.append((filename, probe))

    def snapshot(self, probe, payload):
        return probe._copilot_snapshot_from_payload(payload)

    def fetch(self, probe, payloads, tokens=(TOKEN,), statuses=None):
        requests = []
        bodies = list(payloads if isinstance(payloads, list) else [payloads])
        codes = list(statuses or [200] * len(bodies))

        def respond(request):
            requests.append(request)
            index = len(requests) - 1
            code = codes[index] if index < len(codes) else codes[-1]
            body = bodies[index] if index < len(bodies) else bodies[-1]
            return httpx.Response(code, json=body)

        client = httpx.Client(transport=httpx.MockTransport(respond))
        self.addCleanup(client.close)
        with patch.object(probe, "_copilot_token_candidates", return_value=list(tokens)), \
             patch.object(httpx, "Client", return_value=client):
            try:
                return probe._fetch_copilot_account_usage(), requests
            except Exception:
                self.requests = requests
                raise

    def test_pro_on_credits_shows_plan_used_percent_and_reset(self):
        for filename, probe in self.probes:
            with self.subTest(file=filename):
                card = self.snapshot(probe, PRO_CREDITS)
                self.assertEqual(card["provider"], "copilot")
                self.assertEqual(card["plan"], "Pro")
                self.assertEqual(card["account_label"], "octocat")
                self.assertEqual([item["label"] for item in card["windows"]], ["AI credits"])
                window = card["windows"][0]
                self.assertAlmostEqual(window["used_percent"], 60.0)
                self.assertEqual(window["reset_at"], "2026-11-01T00:00:00+00:00")
                self.assertEqual(window["detail"], "900 of 1500 AI credits used")
                wired, requests = self.fetch(probe, PRO_CREDITS)
                self.assertEqual(wired["plan"], "Pro")
                self.assertEqual(str(requests[0].url), URL)

    def test_legacy_pro_plus_uses_premium_requests_and_the_date_field(self):
        for filename, probe in self.probes:
            with self.subTest(file=filename):
                card = self.snapshot(probe, LEGACY_PRO_PLUS)
                self.assertEqual(card["plan"], "Pro+")
                self.assertEqual([item["label"] for item in card["windows"]], ["Premium requests"])
                window = card["windows"][0]
                self.assertAlmostEqual(window["used_percent"], 11.496, places=3)
                self.assertEqual(window["reset_at"], "2026-02-01T00:00:00+00:00")
                self.assertEqual(window["detail"], "172 of 1500 Premium requests used")

    def test_missing_percent_remaining_uses_quota_remaining(self):
        payload = {
            "access_type_sku": "monthly_subscriber_quota",
            "token_based_billing": True,
            "quota_reset_date_utc": "2026-12-01T00:00:00Z",
            "quota_snapshots": {
                "premium_interactions": {
                    "entitlement": 1000,
                    "quota_remaining": 250,
                    "remaining": 100,
                    "unlimited": False,
                },
            },
        }
        for filename, probe in self.probes:
            with self.subTest(file=filename):
                window = self.snapshot(probe, payload)["windows"][0]
                self.assertAlmostEqual(window["used_percent"], 75.0)
                self.assertEqual(window["detail"], "750 of 1000 AI credits used")

    def test_paid_extra_stays_off_the_included_bar(self):
        payload = {
            "access_type_sku": "monthly_subscriber_quota",
            "token_based_billing": True,
            "quota_reset_date_utc": "2026-11-01T00:00:00Z",
            "quota_snapshots": {
                "premium_interactions": {
                    "entitlement": 1000,
                    "quota_remaining": 0,
                    "percent_remaining": -25,
                    "overage_permitted": True,
                    "overage_count": 36,
                    "unlimited": False,
                },
            },
        }
        for filename, probe in self.probes:
            with self.subTest(file=filename):
                card = self.snapshot(probe, payload)
                self.assertEqual([item["label"] for item in card["windows"]], ["AI credits", "Additional usage"])
                included, extra = card["windows"]
                self.assertEqual(included["used_percent"], 100.0)
                self.assertEqual(included["detail"], "1000 of 1000 AI credits used")
                self.assertIsNone(extra["used_percent"])
                self.assertIsNone(extra["remaining_percent"])
                self.assertEqual(extra["detail"], "36 AI credits beyond included")
                self.assertEqual(extra["reset_at"], included["reset_at"])

    def test_business_seat_shows_credits_used_or_the_organization_note(self):
        for filename, probe in self.probes:
            with self.subTest(file=filename):
                card = self.snapshot(probe, BUSINESS_CREDITS)
                self.assertEqual(card["plan"], "Business")
                self.assertEqual([item["label"] for item in card["windows"]], ["AI credits used"])
                window = card["windows"][0]
                self.assertIsNone(window["used_percent"])
                self.assertIn("2111", window["detail"])
                self.assertIn("organization", window["detail"].lower())
                self.assertEqual(window["reset_at"], "2026-09-01T00:00:00+00:00")
                self.assertIsNone(_window(card, "Additional usage"))
                for credits in (None, 0):
                    body = json.loads(json.dumps(BUSINESS_CREDITS))
                    premium = body["quota_snapshots"]["premium_interactions"]
                    if credits is None:
                        premium.pop("credits_used")
                    else:
                        premium["credits_used"] = credits
                    empty = self.snapshot(probe, body)
                    self.assertEqual(empty["windows"], [])
                    self.assertEqual(empty["plan"], "Business")
                    self.assertTrue(any("organization" in line.lower() for line in empty["details"]))

    def test_free_plan_reads_current_and_legacy_shapes(self):
        for filename, probe in self.probes:
            with self.subTest(file=filename):
                current = self.snapshot(probe, FREE_CURRENT)
                self.assertEqual(current["plan"], "Free")
                self.assertEqual([item["label"] for item in current["windows"]], ["Chat", "Completions"])
                chat, completions = current["windows"]
                self.assertAlmostEqual(chat["used_percent"], 9.0)
                self.assertEqual(chat["detail"], "18 of 200 used")
                self.assertAlmostEqual(completions["used_percent"], 0.6, places=3)
                self.assertEqual(completions["detail"], "11 of 2000 used")
                self.assertEqual(chat["reset_at"], "2099-07-01T00:00:00+00:00")
                legacy = self.snapshot(probe, FREE_LEGACY)
                self.assertEqual(legacy["plan"], "Individual")
                self.assertEqual([item["label"] for item in legacy["windows"]], ["Chat", "Completions"])
                self.assertAlmostEqual(legacy["windows"][0]["used_percent"], 50.0)
                self.assertEqual(legacy["windows"][0]["detail"], "250 of 500 used")
                self.assertEqual(legacy["windows"][1]["detail"], "2000 of 4000 used")
                self.assertEqual(legacy["windows"][0]["reset_at"], "2099-02-15T00:00:00+00:00")
                # limited_user_quotas is what is left, monthly_quotas is the allowance.
                uneven = {
                    "copilot_plan": "individual",
                    "limited_user_quotas": {"chat": 100, "completions": 500},
                    "monthly_quotas": {"chat": 400, "completions": 2000},
                    "limited_user_reset_date": "2099-03-01",
                }
                counted = self.snapshot(probe, uneven)
                self.assertAlmostEqual(counted["windows"][0]["used_percent"], 75.0)
                self.assertEqual(counted["windows"][0]["detail"], "300 of 400 used")

    def test_unlimited_and_empty_buckets_are_ignored(self):
        payload = {
            "access_type_sku": "monthly_subscriber_quota",
            "token_based_billing": False,
            "quota_reset_date_utc": "2026-11-01T00:00:00Z",
            "quota_snapshots": {
                "chat": {"unlimited": True, "entitlement": 500, "percent_remaining": 10, "quota_remaining": 50},
                "completions": {"entitlement": -1, "remaining": -1, "percent_remaining": 0},
                "premium_interactions": {
                    "unlimited": False, "entitlement": 1000, "percent_remaining": 80,
                    "quota_remaining": 800, "overage_permitted": False,
                },
            },
        }
        for filename, probe in self.probes:
            with self.subTest(file=filename):
                card = self.snapshot(probe, payload)
                self.assertEqual([item["label"] for item in card["windows"]], ["Premium requests"])
                self.assertAlmostEqual(card["windows"][0]["used_percent"], 20.0)
                with self.assertRaises(ValueError):
                    self.snapshot(probe, {"copilot_plan": "pro"})

    def test_headers_and_fallthrough_after_401(self):
        for filename, probe in self.probes:
            with self.subTest(file=filename):
                card, requests = self.fetch(
                    probe,
                    [{"message": "no"}, PRO_CREDITS],
                    tokens=("gho_old", "gho_new"),
                    statuses=[401, 200],
                )
                self.assertEqual(card["plan"], "Pro")
                self.assertEqual(
                    [request.headers["Authorization"] for request in requests],
                    ["token gho_old", "token gho_new"],
                )
                for request in requests:
                    self.assertTrue(request.headers["Authorization"].startswith("token "))
                    self.assertEqual(request.headers["Accept"], "application/json")
                    self.assertEqual(request.headers["Editor-Version"], "vscode/1.96.2")
                    self.assertEqual(request.headers["Editor-Plugin-Version"], "copilot-chat/0.26.7")
                    self.assertEqual(request.headers["User-Agent"], "GitHubCopilotChat/0.26.7")
                    self.assertEqual(request.headers["X-Github-Api-Version"], "2025-04-01")

    def test_every_rejected_token_shows_an_unavailable_card(self):
        secret = "gho_rejected_login_value"
        for filename, probe in self.probes:
            with self.subTest(file=filename):
                def respond(request):
                    return httpx.Response(401, json={"message": secret})

                client = httpx.Client(transport=httpx.MockTransport(respond))
                self.addCleanup(client.close)
                with patch.object(probe, "_disabled_providers", set(probe.LIVE_PROVIDERS) - {"copilot"}), \
                     patch.object(probe, "_copilot_token_candidates", return_value=[secret]), \
                     patch.object(httpx, "Client", return_value=client):
                    snapshots, complete = probe._collect_cli()
                self.assertTrue(complete)
                self.assertEqual([item["provider"] for item in snapshots], ["copilot"])
                card = snapshots[0]
                self.assertTrue(card.get("error"))
                self.assertEqual(card["windows"], [])
                self.assertIn("401", card["error"])
                self.assertNotIn(secret, json.dumps(card))

    def test_classic_pat_is_skipped(self):
        for filename, probe in self.probes:
            with self.subTest(file=filename):
                seen = []

                def respond(request):
                    seen.append(request.headers["Authorization"])
                    return httpx.Response(200, json=PRO_CREDITS)

                client = httpx.Client(transport=httpx.MockTransport(respond))
                self.addCleanup(client.close)
                env = {
                    "COPILOT_GITHUB_TOKEN": "ghp_classic_skipped",
                    "GH_TOKEN": "gho_kept",
                    "GITHUB_TOKEN": "ghp_also_skipped",
                }
                with tempfile.TemporaryDirectory() as tmp, \
                     patch.dict(os.environ, env, clear=False), \
                     patch.dict(sys.modules, {"agent.secret_scope": None}), \
                     patch.object(probe, "_user_home", return_value=Path(tmp)), \
                     patch.object(probe, "_hermes_env_value", side_effect=AssertionError("dotenv")), \
                     patch.object(probe, "_read_env_file_value", side_effect=AssertionError("dotenv")), \
                     patch.object(probe.subprocess, "run", side_effect=AssertionError("gh ran")), \
                     patch.object(httpx, "Client", return_value=client):
                    hermes = Path(tmp) / ".hermes"
                    hermes.mkdir()
                    (hermes / ".env").write_text("COPILOT_GITHUB_TOKEN=gho_from_dotenv\n", encoding="utf-8")
                    (hermes / "auth.json").write_text('{"token":"gho_from_auth"}\n', encoding="utf-8")
                    card = probe._fetch_copilot_account_usage()
                self.assertEqual(card["plan"], "Pro")
                self.assertEqual(seen, ["token gho_kept"])
                self.assertNotIn("gho_from_dotenv", "".join(seen))
                self.assertNotIn("gho_from_auth", "".join(seen))

    def test_secret_helper_is_used_without_reading_env_files(self):
        for filename, probe in self.probes:
            with self.subTest(file=filename):
                seen = []

                def respond(request):
                    seen.append(request.headers["Authorization"])
                    return httpx.Response(200, json=PRO_CREDITS)

                real_client = httpx.Client

                def open_client(*args, **kwargs):
                    return real_client(transport=httpx.MockTransport(respond))

                scope = types.ModuleType("agent.secret_scope")

                def get_secret(name, default=None):
                    if name == "COPILOT_GITHUB_TOKEN":
                        return "gho_scoped"
                    return default

                scope.get_secret = get_secret
                package = types.ModuleType("agent")
                package.secret_scope = scope
                with tempfile.TemporaryDirectory() as tmp, \
                     patch.dict(sys.modules, {"agent": package, "agent.secret_scope": scope}), \
                     patch.dict(os.environ, {"COPILOT_GITHUB_TOKEN": "gho_process"}, clear=False), \
                     patch.object(probe, "_user_home", return_value=Path(tmp)), \
                     patch.object(probe, "_hermes_env_value", side_effect=AssertionError("dotenv")), \
                     patch.object(probe, "_read_env_file_value", side_effect=AssertionError("dotenv")), \
                     patch.object(probe.subprocess, "run", side_effect=AssertionError("gh ran")), \
                     patch.object(httpx, "Client", side_effect=open_client):
                    (Path(tmp) / ".env").write_text("COPILOT_GITHUB_TOKEN=gho_from_dotenv\n", encoding="utf-8")
                    card = probe._fetch_copilot_account_usage()
                self.assertEqual(card["plan"], "Pro")
                self.assertEqual(seen, ["token gho_scoped"])

                seen.clear()
                scope.get_secret = lambda name, default=None: default
                with tempfile.TemporaryDirectory() as tmp, \
                     patch.dict(sys.modules, {"agent": package, "agent.secret_scope": scope}), \
                     patch.dict(os.environ, {"COPILOT_GITHUB_TOKEN": "gho_process"}, clear=False), \
                     patch.object(probe, "_user_home", return_value=Path(tmp)), \
                     patch.object(probe, "_read_env_file_value", side_effect=AssertionError("dotenv")), \
                     patch.object(probe.subprocess, "run", side_effect=AssertionError("gh ran")), \
                     patch.object(httpx, "Client", side_effect=open_client):
                    editor = Path(tmp) / ".config" / "github-copilot"
                    editor.mkdir(parents=True)
                    (editor / "apps.json").write_text(
                        json.dumps({"github.com": {"oauth_token": "ghu_from_editor"}}),
                        encoding="utf-8",
                    )
                    card = probe._fetch_copilot_account_usage()
                self.assertEqual(seen, ["token ghu_from_editor"])

                def unavailable(name, default=None):
                    raise RuntimeError("no profile scope")

                scope.get_secret = unavailable
                seen.clear()
                with patch.dict(sys.modules, {"agent": package, "agent.secret_scope": scope}), \
                     patch.dict(os.environ, {"COPILOT_GITHUB_TOKEN": "gho_process"}, clear=False), \
                     patch.object(probe, "_read_env_file_value", side_effect=AssertionError("dotenv")), \
                     patch.object(probe.subprocess, "run", side_effect=AssertionError("gh ran")), \
                     patch.object(httpx, "Client", side_effect=open_client):
                    card = probe._fetch_copilot_account_usage()
                self.assertEqual(seen, ["token gho_process"])

    def test_editor_file_then_gh_cli(self):
        for filename, probe in self.probes:
            with self.subTest(file=filename):
                calls = []

                def respond(request):
                    calls.append(request.headers["Authorization"])
                    auth = request.headers["Authorization"]
                    code = 200 if auth in {"token ghu_from_hosts_file", "token gho_from_cli"} else 401
                    body = PRO_CREDITS if code == 200 else {"message": "no"}
                    return httpx.Response(code, json=body)

                real_client = httpx.Client

                def open_client(*args, **kwargs):
                    return real_client(transport=httpx.MockTransport(respond))

                def fake_run(args, **kwargs):
                    self.assertEqual(args[:3], ["gh", "auth", "token"])
                    self.assertLessEqual(kwargs["timeout"], 3)
                    self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
                    self.assertEqual(kwargs["env"].get("GH_PROMPT_DISABLED"), "1")
                    self.assertNotIn("GH_TOKEN", kwargs["env"])
                    self.assertNotIn("GITHUB_TOKEN", kwargs["env"])
                    completed = subprocess.CompletedProcess(args, 0, stdout="gho_from_cli\n", stderr="")
                    return completed

                with tempfile.TemporaryDirectory() as tmp, \
                     patch.dict(os.environ, {
                         "COPILOT_GITHUB_TOKEN": "", "GH_TOKEN": "ghp_parent", "GITHUB_TOKEN": "",
                     }, clear=False), \
                     patch.dict(sys.modules, {"agent.secret_scope": None}), \
                     patch.object(probe, "_user_home", return_value=Path(tmp)), \
                     patch.object(probe, "_hermes_env_value", side_effect=AssertionError("dotenv")), \
                     patch.object(probe, "_read_env_file_value", side_effect=AssertionError("dotenv")), \
                     patch.object(probe.subprocess, "run", side_effect=fake_run), \
                     patch.object(httpx, "Client", side_effect=open_client):
                    home = Path(tmp)
                    editor = home / ".config" / "github-copilot"
                    editor.mkdir(parents=True)
                    (editor / "apps.json").write_text(json.dumps({
                        "github.com:Iv1.example": {"user": "octocat", "oauth_token": "ghu_from_editor"},
                    }), encoding="utf-8")
                    (editor / "hosts.json").write_text(json.dumps({
                        "github.com": {"oauth_token": "ghu_from_hosts_file"},
                    }), encoding="utf-8")
                    gh_dir = home / ".config" / "gh"
                    gh_dir.mkdir(parents=True)
                    (gh_dir / "hosts.yml").write_text(
                        "github.com:\n  oauth_token: gho_from_hosts\nexample.ghe.com:\n  oauth_token: gho_enterprise\n",
                        encoding="utf-8",
                    )
                    card = probe._fetch_copilot_account_usage()
                    self.assertEqual(card["plan"], "Pro")
                    self.assertEqual(calls, ["token ghu_from_editor", "token ghu_from_hosts_file"])
                    self.assertNotIn("token gho_from_hosts", calls)

                calls.clear()
                with tempfile.TemporaryDirectory() as tmp, \
                     patch.dict(os.environ, {
                         "COPILOT_GITHUB_TOKEN": "", "GH_TOKEN": "", "GITHUB_TOKEN": "",
                     }, clear=False), \
                     patch.dict(sys.modules, {"agent.secret_scope": None}), \
                     patch.object(probe, "_user_home", return_value=Path(tmp)), \
                     patch.object(probe, "_read_env_file_value", side_effect=AssertionError("dotenv")), \
                     patch.object(probe.subprocess, "run", side_effect=fake_run), \
                     patch.object(httpx, "Client", side_effect=open_client):
                    home = Path(tmp)
                    gh_dir = home / ".config" / "gh"
                    gh_dir.mkdir(parents=True)
                    (gh_dir / "hosts.yml").write_text(
                        "github.com:\n  user: octocat\n  oauth_token: gho_from_hosts\nexample.ghe.com:\n  oauth_token: gho_enterprise\n",
                        encoding="utf-8",
                    )
                    card = probe._fetch_copilot_account_usage()
                    self.assertEqual(card["plan"], "Pro")
                    self.assertEqual(calls, ["token gho_from_cli"])
                    self.assertNotIn("token gho_from_hosts", calls)
                    self.assertNotIn("token gho_enterprise", calls)

    def test_reset_falls_back_to_the_next_month_for_paid_plans(self):
        payload = {
            "access_type_sku": "monthly_subscriber_quota",
            "token_based_billing": True,
            "quota_snapshots": {
                "premium_interactions": {
                    "entitlement": 1000,
                    "percent_remaining": 50,
                    "quota_remaining": 500,
                    "quota_reset_at": 0,
                    "unlimited": False,
                },
            },
        }
        stamped = json.loads(json.dumps(payload))
        stamped["quota_snapshots"]["premium_interactions"]["quota_reset_at"] = 1893456000
        free = json.loads(json.dumps(FREE_CURRENT))
        free.pop("quota_reset_date")
        for filename, probe in self.probes:
            with self.subTest(file=filename):
                with patch.object(probe, "_utc_now", return_value=datetime(2026, 10, 23, 15, tzinfo=timezone.utc)):
                    card = self.snapshot(probe, payload)
                self.assertEqual(card["windows"][0]["reset_at"], "2026-11-01T00:00:00+00:00")
                self.assertTrue(any("00:00 UTC" in line for line in card["details"]))
                with patch.object(probe, "_utc_now", return_value=datetime(2026, 12, 31, 23, tzinfo=timezone.utc)):
                    december = self.snapshot(probe, payload)
                self.assertEqual(december["windows"][0]["reset_at"], "2027-01-01T00:00:00+00:00")
                from_stamp = self.snapshot(probe, stamped)
                self.assertEqual(from_stamp["windows"][0]["reset_at"], "2030-01-01T00:00:00+00:00")
                self.assertFalse(any("00:00 UTC" in line for line in from_stamp["details"]))
                free_card = self.snapshot(probe, free)
                self.assertIsNone(free_card["windows"][0]["reset_at"])

    def test_stdout_never_contains_the_token(self):
        secret = "gho_stdout_secret_should_not_print"
        runner = r'''
import importlib.util, os, sys
import httpx
spec = importlib.util.spec_from_file_location("tested", sys.argv[1])
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)
transport = httpx.MockTransport(lambda request: httpx.Response(401, json={"message": "bad credentials"}))
RealClient = httpx.Client
def Client(*args, **kwargs):
    kwargs["transport"] = transport
    return RealClient(*args, **kwargs)
httpx.Client = Client
class Done(BaseException):
    pass
probe.os._exit = lambda code=0: (_ for _ in ()).throw(Done())
sys.argv = sys.argv[1:]
try:
    probe._main_inner()
except Done:
    pass
'''
        site = str(Path(httpx.__file__).resolve().parents[1])
        for filename, probe in self.probes:
            with self.subTest(file=filename), tempfile.TemporaryDirectory() as tmp:
                disabled = ",".join(name for name in probe.LIVE_PROVIDERS if name != "copilot")
                env = {key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR") if key in os.environ}
                env.update(
                    HOME=tmp, USERPROFILE=tmp, HERMES_HOME=tmp,
                    COPILOT_GITHUB_TOKEN=secret, PYTHONPATH=site, PATH=tmp,
                )
                result = subprocess.run(
                    [sys.executable, "-c", runner, str((ROOT / filename).resolve()),
                     "--cli-only", "--fresh", f"--disabled-providers={disabled}"],
                    env=env, capture_output=True, text=True, timeout=20,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn(secret, result.stdout)
                self.assertNotIn(secret, result.stderr)
                cards = json.loads(result.stdout)
                self.assertTrue(any(card.get("provider") == "copilot" and card.get("error") for card in cards))

    def test_disabled_provider_skips_discovery(self):
        for filename, probe in self.probes:
            with self.subTest(file=filename):
                with patch.object(probe, "_disabled_providers", {"copilot"}), \
                     patch.object(probe, "_copilot_token_candidates", side_effect=AssertionError("discovery")) as candidates, \
                     patch.object(probe, "_codex_pool_accounts", return_value=[]), \
                     patch.object(probe, "_codex_cli_access_context", return_value=None), \
                     patch.object(probe, "_opencode_go_api_keys", return_value=[]):
                    blocked = []
                    for name in list(vars(probe)):
                        if name.startswith("_fetch_") and name != "_fetch_copilot_account_usage":
                            blocked.append(patch.object(probe, name, return_value=None))
                    for item in blocked:
                        item.start()
                    try:
                        snapshots, complete = probe._collect_cli()
                    finally:
                        for item in blocked:
                            item.stop()
                self.assertTrue(complete)
                self.assertNotIn("copilot", [item.get("provider") for item in snapshots])
                candidates.assert_not_called()

    def test_no_login_hides_the_card(self):
        for filename, probe in self.probes:
            with self.subTest(file=filename):
                with patch.object(probe, "_copilot_token_candidates", return_value=[]), \
                     patch.object(httpx, "Client", side_effect=AssertionError("request")):
                    self.assertIsNone(probe._fetch_copilot_account_usage())

    def test_plugin_lists_copilot(self):
        for filename in ("plugin.js", "catalog/desktop/plugin.js"):
            text = (ROOT / filename).read_text(encoding="utf-8")
            self.assertIn("copilot: 'GitHub Copilot'", text)
            self.assertIn("'github-copilot': 'GitHub Copilot'", text)
            self.assertIn("key === 'github-copilot' || key === 'github'", text)
            self.assertIn("'ai-gateway', 'commandcode', 'copilot'", text)
            self.assertIn("GitHub Copilot", text.split("intro:", 1)[1].split("automatic:", 1)[0])


if __name__ == "__main__":
    unittest.main()
