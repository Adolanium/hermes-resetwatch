"""Hermetic checks for the Command Code Resetwatch probe integration."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any


PROBE_PATH = Path(__file__).with_name("probe.py")
SPEC = importlib.util.spec_from_file_location("resetwatch_probe", PROBE_PATH)
assert SPEC and SPEC.loader
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


class FakeResponse:
    def __init__(self, payload: Any, status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code

    def json(self) -> Any:
        return self.payload


class FakeClient:
    calls: list[tuple[str, dict[str, str]]] = []
    responses: dict[str, FakeResponse] = {}

    def __init__(self, **_kwargs: Any) -> None:
        pass

    def __enter__(self) -> "FakeClient":
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def get(self, url: str, *, headers: dict[str, str]) -> FakeResponse:
        self.calls.append((url, headers))
        for needle, response in self.responses.items():
            if needle in url:
                return response
        raise AssertionError(f"unexpected URL: {url}")


def fake_httpx() -> ModuleType:
    module = ModuleType("httpx")
    module.Client = FakeClient  # type: ignore[attr-defined]
    return module


def test_snapshot_parses_windows_and_usage() -> None:
    snapshot = probe._commandcode_snapshot(
        {"login": "acme", "org_id": "org_1", "key_name": "Hermes"},
        {
            "credits": {"monthlyCredits": 40, "purchasedCredits": 10, "freeCredits": 5},
            "windowLimits": {
                "fiveHour": {"used": 8, "cap": 16, "resetAt": 1_700_000_000_000},
                "weekly": {"used": 20, "cap": 40, "resetAt": None},
            },
        },
        {"data": {"planId": "pro", "currentPeriodStart": "2026-01-01T00:00:00Z"}},
        {"totalCost": 12.34, "totalCount": 1500, "totalTokens": 74_200_000},
    )

    assert snapshot is not None
    assert snapshot["provider"] == "commandcode"
    assert snapshot["plan"] == "Pro"
    assert snapshot["account_label"] == "Hermes"
    assert snapshot["account_key"] == "org_1"
    assert snapshot["windows"][0]["label"] == "5-hour"
    assert snapshot["windows"][0]["used_percent"] == 50.0
    assert snapshot["windows"][0]["remaining_percent"] == 50.0
    assert snapshot["windows"][0]["reset_at"] == "2023-11-14T22:13:20+00:00"
    assert "$55.00 credits left" in snapshot["details"][0]
    assert "$12.34 used · 1,500 requests · 74,200,000 tokens" in snapshot["details"][1]


def test_fetch_uses_bearer_auth_and_alpha_endpoints() -> None:
    FakeClient.calls = []
    FakeClient.responses = {
        "whoami": FakeResponse({"user": {"userName": "alice"}, "org": {"id": "org_1", "login": "alice-inc"}}),
        "billing/subscriptions": FakeResponse({"data": {"planId": "pro", "currentPeriodStart": "2026-01-01T00:00:00Z"}}),
        "billing/credits": FakeResponse({"credits": {"monthlyCredits": 5}, "windowLimits": {}}),
        "usage/summary": FakeResponse({"totalCost": 1, "totalCount": 2}),
    }
    original_httpx = sys.modules.get("httpx")
    original_key = probe._commandcode_api_key
    sys.modules["httpx"] = fake_httpx()
    setattr(probe, "_commandcode_api_key", lambda: "unit-test-key")
    try:
        snapshot = probe._fetch_commandcode_account_usage()
    finally:
        setattr(probe, "_commandcode_api_key", original_key)
        if original_httpx is None:
            sys.modules.pop("httpx", None)
        else:
            sys.modules["httpx"] = original_httpx

    assert snapshot is not None
    assert len(FakeClient.calls) == 4
    assert all(headers["Authorization"] == "Bearer unit-test-key" for _url, headers in FakeClient.calls)
    urls = [url for url, _headers in FakeClient.calls]
    assert any(url.endswith("/alpha/whoami") for url in urls)
    assert any("/alpha/billing/credits?orgId=org_1" in url for url in urls)
    assert any("/alpha/billing/subscriptions?orgId=org_1" in url for url in urls)
    assert any("/alpha/usage/summary?orgId=org_1&since=2026-01-01T00%3A00%3A00Z" in url for url in urls)


def test_optional_endpoint_failures_leave_summary_available() -> None:
    snapshot = probe._commandcode_snapshot(
        {"login": "alice", "org_id": None, "key_name": None},
        None,
        None,
        {"totalCost": 3.0, "totalCount": 10},
    )
    assert snapshot is not None
    assert snapshot["windows"] == []
    assert snapshot["details"] == ["$3.00 used · 10 requests"]


def test_unrecognized_account_is_rejected() -> None:
    assert probe._commandcode_account({"user": {}, "org": {}}) is None


if __name__ == "__main__":
    for test in (
        test_snapshot_parses_windows_and_usage,
        test_fetch_uses_bearer_auth_and_alpha_endpoints,
        test_optional_endpoint_failures_leave_summary_available,
        test_unrecognized_account_is_rejected,
    ):
        test()
    print("ok")
