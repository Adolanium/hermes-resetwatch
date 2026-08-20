"""Stock Hermes usage probe for Resetwatch.

Prints JSON snapshots for Claude, Codex, and OpenRouter using fetchers
the gateway already ships, plus Cursor, Kimi, and Grok from those apps'
own CLI or desktop logins. No tokens on stdout. No extra RPC.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


HERMES_PROVIDERS = ("anthropic", "openai-codex", "openrouter")
USER_AGENT = "resetwatch"

CURSOR_PERIOD_USAGE_URL = "https://api2.cursor.sh/aiserver.v1.DashboardService/GetCurrentPeriodUsage"

KIMI_CODE_CLIENT_ID = "17e5f671-d194-4dfb-9706-5516cb48c098"
KIMI_CODE_OAUTH_TOKEN_URL = "https://auth.kimi.com/api/oauth/token"
KIMI_CODE_USAGE_URL = "https://api.kimi.com/coding/v1/usages"
KIMI_CODE_TOKEN_SKEW_SECONDS = 60

GROK_BILLING_URL = "https://cli-chat-proxy.grok.com/v1/billing?format=credits"
GROK_SETTINGS_URL = "https://cli-chat-proxy.grok.com/v1/settings"
GROK_OAUTH_TOKEN_URL = "https://auth.x.ai/oauth2/token"
GROK_OAUTH_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
GROK_TOKEN_SKEW_SECONDS = 60


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _user_home() -> Path:
    return Path.home()


def _is_macos() -> bool:
    return sys.platform == "darwin"


def _is_windows() -> bool:
    return sys.platform == "win32"


def _title_case_slug(value: Optional[str]) -> Optional[str]:
    cleaned = str(value or "").strip()
    if not cleaned:
        return None
    return cleaned.replace("_", " ").replace("-", " ").title()


def _parse_dt(value: Any) -> Optional[datetime]:
    if value in {None, ""}:
        return None
    if isinstance(value, (int, float)):
        stamp = float(value)
        if stamp > 1e12:
            stamp /= 1000.0
        return datetime.fromtimestamp(stamp, tz=timezone.utc)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        if "." in text:
            head, rest = text.split(".", 1)
            digits = []
            tz_part = ""
            for index, char in enumerate(rest):
                if char.isdigit():
                    digits.append(char)
                else:
                    tz_part = rest[index:]
                    break
            frac = "".join(digits)[:6].ljust(6, "0")
            text = f"{head}.{frac}{tz_part}"
        try:
            parsed = datetime.fromisoformat(text)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _to_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(value):
        return int(value)
    if isinstance(value, str) and value.strip():
        try:
            return int(float(value))
        except ValueError:
            return None
    return None


def _win(
    label: str,
    used_percent: Optional[float],
    reset_at: Optional[datetime] = None,
    detail: Optional[str] = None,
) -> dict:
    remaining = None if used_percent is None else max(0.0, min(100.0, 100.0 - float(used_percent)))
    return {
        "label": label,
        "used_percent": used_percent,
        "remaining_percent": remaining,
        "reset_at": reset_at.isoformat() if reset_at is not None else None,
        "detail": detail,
    }


def _snapshot(provider: str, plan: Optional[str], windows: list[dict], details: Optional[list[str]] = None) -> dict:
    return {
        "provider": provider,
        "plan": plan,
        "details": list(details or ()),
        "windows": windows,
    }


def _hermes_window(window) -> dict:
    used = window.used_percent
    remaining = None if used is None else max(0.0, min(100.0, 100.0 - float(used)))
    reset = getattr(window, "reset_at", None)
    return {
        "label": window.label,
        "used_percent": used,
        "remaining_percent": remaining,
        "reset_at": reset.isoformat() if reset is not None else None,
        "detail": window.detail,
    }


def _write_json_file(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _cursor_agent_executable() -> Optional[str]:
    override = (os.environ.get("CURSOR_AGENT") or os.environ.get("CURSOR_USAGE_AGENT") or "").strip()
    if override:
        path = Path(override).expanduser()
        return str(path) if path.is_file() else override
    candidates: list[Path] = []
    if _is_windows():
        local_app = os.environ.get("LOCALAPPDATA") or ""
        if local_app:
            root = Path(local_app) / "cursor-agent"
            candidates.extend(root / name for name in ("agent.cmd", "cursor-agent.cmd", "agent.exe"))
    if _is_macos():
        home = _user_home()
        candidates.append(home / ".local" / "bin" / "cursor-agent")
        versions = home / ".local" / "share" / "cursor-agent" / "versions"
        if versions.is_dir():
            version_bins = sorted(
                (item / "cursor-agent" for item in versions.iterdir() if item.is_dir()),
                key=lambda item: item.parent.name,
                reverse=True,
            )
            candidates.extend(version_bins)
        candidates.extend(
            (
                Path("/opt/homebrew/bin/cursor-agent"),
                Path("/usr/local/bin/cursor-agent"),
            )
        )
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    try:
        import shutil

        # Never fall back to a bare `agent` on PATH: that name collides with other CLIs.
        return shutil.which("cursor-agent")
    except Exception:
        return None


def _cursor_cli_json(args: list[str]) -> Optional[dict]:
    exe = _cursor_agent_executable()
    if not exe:
        return None
    try:
        result = subprocess.run(
            [exe, *args],
            capture_output=True,
            text=True,
            timeout=12,
            check=False,
        )
    except Exception:
        return None
    try:
        payload = json.loads(result.stdout or "")
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _cursor_cli_plan_name() -> Optional[str]:
    about = _cursor_cli_json(["about", "--format", "json"])
    if not about:
        return None
    plan = about.get("subscriptionTier")
    text = str(plan or "").strip()
    return text or None


def _cursor_ide_state_db_paths() -> list[Path]:
    paths: list[Path] = []
    if _is_windows():
        app_data = os.environ.get("APPDATA") or ""
        local_app = os.environ.get("LOCALAPPDATA") or ""
        roots = []
        if app_data:
            roots.append(Path(app_data))
        if local_app:
            roots.append(Path(local_app))
        for root in roots:
            for product in ("Cursor", "Cursor - Insiders"):
                paths.append(root / product / "User" / "globalStorage" / "state.vscdb")
        return paths
    if _is_macos():
        support = _user_home() / "Library" / "Application Support"
        for product in ("Cursor", "Cursor - Insiders"):
            paths.append(support / product / "User" / "globalStorage" / "state.vscdb")
    return paths


def _read_cursor_token_from_vscdb(path: Path) -> Optional[str]:
    if not path.is_file():
        return None
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            row = connection.execute(
                "SELECT value FROM ItemTable WHERE key = ? LIMIT 1",
                ("cursorAuth/accessToken",),
            ).fetchone()
        finally:
            connection.close()
    except sqlite3.Error:
        return None
    token = row[0] if row else None
    if isinstance(token, str) and token.strip():
        return token.strip()
    return None


def _cursor_ide_access_token() -> Optional[str]:
    for path in _cursor_ide_state_db_paths():
        token = _read_cursor_token_from_vscdb(path)
        if token:
            return token
    return None


def _cursor_macos_keychain_token() -> Optional[str]:
    if not _is_macos():
        return None
    try:
        result = subprocess.run(
            ["/usr/bin/security", "find-generic-password", "-s", "cursor-access-token", "-w"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception:
        return None
    token = (result.stdout or "").strip()
    if result.returncode != 0 or not token:
        return None
    return token


def _cursor_access_token() -> Optional[str]:
    status = _cursor_cli_json(["status", "--format", "json"])
    if status:
        auth = status.get("auth") if isinstance(status.get("auth"), dict) else None
        token = auth.get("accessToken") if auth else None
        if isinstance(token, str) and token.strip():
            return token.strip()
    keychain = _cursor_macos_keychain_token()
    if keychain:
        return keychain
    return _cursor_ide_access_token()


def _fmt_cursor_amount(value: float) -> str:
    if float(value) == int(value) and abs(value) >= 100:
        return f"${int(value) / 100:.2f}"
    return f"${float(value):.2f}"


def _fetch_cursor_account_usage() -> Optional[dict]:
    token = _cursor_access_token()
    if not token:
        return None
    import httpx

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Connect-Protocol-Version": "1",
        "User-Agent": USER_AGENT,
    }
    with httpx.Client(timeout=15.0) as client:
        response = client.post(CURSOR_PERIOD_USAGE_URL, headers=headers, json={})
        response.raise_for_status()
        payload = response.json() or {}
    if not isinstance(payload, dict):
        return None
    plan_usage = payload.get("planUsage") if isinstance(payload.get("planUsage"), dict) else {}
    reset_at = _parse_dt(payload.get("billingCycleEnd"))
    windows: list[dict] = []
    limit = plan_usage.get("limit")
    remaining = plan_usage.get("remaining")
    if isinstance(limit, (int, float)) and float(limit) > 0 and isinstance(remaining, (int, float)):
        used_percent = max(0.0, min(100.0, (1.0 - float(remaining) / float(limit)) * 100.0))
        windows.append(
            _win(
                "Included spend",
                used_percent,
                reset_at,
                f"{_fmt_cursor_amount(float(remaining))} of {_fmt_cursor_amount(float(limit))} left",
            )
        )
    for key, label in (("autoPercentUsed", "Auto"), ("apiPercentUsed", "API models")):
        pct = plan_usage.get(key)
        if isinstance(pct, (int, float)):
            windows.append(_win(label, max(0.0, min(100.0, float(pct))), reset_at))
    spend = payload.get("spendLimitUsage") if isinstance(payload.get("spendLimitUsage"), dict) else {}
    spend_limit = spend.get("limit")
    spend_used = spend.get("used")
    if isinstance(spend_limit, (int, float)) and float(spend_limit) > 0 and isinstance(spend_used, (int, float)):
        used_percent = max(0.0, min(100.0, float(spend_used) / float(spend_limit) * 100.0))
        windows.append(_win("Spend limit", used_percent, reset_at))
    if not windows:
        return None
    return _snapshot("cursor", _cursor_cli_plan_name(), windows)


def _kimi_code_home() -> Path:
    override = (os.environ.get("KIMI_CODE_HOME") or "").strip()
    if override:
        return Path(override).expanduser()
    return _user_home() / ".kimi-code"


def _kimi_code_credentials_path() -> Optional[Path]:
    explicit = (os.environ.get("KIMI_CODE_CREDENTIALS") or os.environ.get("KIMI_CREDENTIALS") or "").strip()
    if explicit:
        path = Path(explicit).expanduser()
        return path if path.is_file() else None
    path = _kimi_code_home() / "credentials" / "kimi-code.json"
    return path if path.is_file() else None


def _kimi_code_read_credentials(path: Path) -> Optional[dict]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _kimi_code_token_expired(creds: dict, *, skew: int = KIMI_CODE_TOKEN_SKEW_SECONDS) -> bool:
    expires_at = creds.get("expires_at")
    if isinstance(expires_at, (int, float)) and math.isfinite(expires_at):
        return float(expires_at) <= (_utc_now().timestamp() + skew)
    return True


def _kimi_code_refresh_tokens(creds: dict) -> Optional[dict]:
    refresh = creds.get("refresh_token")
    if not isinstance(refresh, str) or not refresh.strip():
        return None
    import httpx

    with httpx.Client(timeout=15.0) as client:
        response = client.post(
            KIMI_CODE_OAUTH_TOKEN_URL,
            data={
                "client_id": KIMI_CODE_CLIENT_ID,
                "grant_type": "refresh_token",
                "refresh_token": refresh.strip(),
            },
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
        )
        if response.status_code >= 400:
            return None
        body = response.json() or {}
    if not isinstance(body, dict):
        return None
    access = body.get("access_token")
    if not isinstance(access, str) or not access.strip():
        return None
    updated = dict(creds)
    updated["access_token"] = access.strip()
    new_refresh = body.get("refresh_token")
    if isinstance(new_refresh, str) and new_refresh.strip():
        updated["refresh_token"] = new_refresh.strip()
    expires_in = _to_int(body.get("expires_in")) or 900
    updated["expires_in"] = expires_in
    updated["expires_at"] = int(_utc_now().timestamp()) + expires_in
    token_type = body.get("token_type")
    if isinstance(token_type, str) and token_type.strip():
        updated["token_type"] = token_type.strip()
    scope = body.get("scope")
    if isinstance(scope, str) and scope.strip():
        updated["scope"] = scope.strip()
    return updated


def _kimi_code_access_token(*, force_refresh: bool = False) -> Optional[str]:
    path = _kimi_code_credentials_path()
    if not path:
        return None
    creds = _kimi_code_read_credentials(path)
    if not creds:
        return None
    token = creds.get("access_token")
    if (
        isinstance(token, str)
        and token.strip()
        and not force_refresh
        and not _kimi_code_token_expired(creds)
    ):
        return token.strip()
    if force_refresh:
        creds = _kimi_code_read_credentials(path) or creds
        token = creds.get("access_token")
        if isinstance(token, str) and token.strip() and not _kimi_code_token_expired(creds):
            return token.strip()
    refreshed = _kimi_code_refresh_tokens(creds)
    if not refreshed:
        return None
    try:
        _write_json_file(path, refreshed)
    except OSError:
        pass
    access = refreshed.get("access_token")
    return access.strip() if isinstance(access, str) else None


def infer_kimi_plan_name(payload: Optional[dict] = None) -> Optional[str]:
    payload = payload or {}
    user = payload.get("user") if isinstance(payload.get("user"), dict) else {}
    membership = user.get("membership") if isinstance(user.get("membership"), dict) else {}
    level = str(membership.get("level") or "").strip()
    if not level:
        return None
    if level.upper().startswith("LEVEL_"):
        level = level[6:]
    return _title_case_slug(level)


def _kimi_window_label(window: Optional[dict], *, fallback: str) -> str:
    if not isinstance(window, dict):
        return fallback
    duration = _to_int(window.get("duration"))
    unit = str(window.get("timeUnit") or window.get("unit") or "").strip()
    if duration is None or duration <= 0:
        return fallback
    if unit in {"TIME_UNIT_MINUTE", "minute"}:
        if duration >= 60 and duration % 60 == 0:
            return f"{duration // 60}h"
        return f"{duration}m"
    if unit in {"TIME_UNIT_HOUR", "hour"}:
        return f"{duration}h"
    if unit in {"TIME_UNIT_DAY", "day"}:
        return f"{duration}d"
    if unit in {"TIME_UNIT_WEEK", "week"}:
        return "Weekly" if duration == 1 else f"{duration}w"
    return fallback


def _kimi_usage_window(detail: Any, *, label: str) -> Optional[dict]:
    if not isinstance(detail, dict):
        return None
    limit = _to_int(detail.get("limit"))
    remaining = _to_int(detail.get("remaining"))
    used = _to_int(detail.get("used"))
    if used is None and limit is not None and remaining is not None:
        used = max(0, limit - remaining)
    if limit is None or limit <= 0 or used is None:
        return None
    used_percent = max(0.0, min(100.0, used / float(limit) * 100.0))
    left = remaining if remaining is not None else max(0, limit - used)
    return _win(
        label,
        used_percent,
        _parse_dt(detail.get("resetTime") or detail.get("resetAt")),
        f"{left} of {limit} left",
    )


def _kimi_extra_usage_window(payload: dict) -> Optional[dict]:
    wallet = payload.get("boosterWallet") if isinstance(payload.get("boosterWallet"), dict) else {}
    balance = wallet.get("balance") if isinstance(wallet.get("balance"), dict) else {}
    if str(balance.get("type") or "").upper() not in {"BOOSTER", "BALANCE_BOOSTER"}:
        return None
    amount = _to_int(balance.get("amount"))
    amount_left = _to_int(balance.get("amountLeft"))
    if amount is None or amount <= 0:
        return None
    scale = 1_000_000
    total = amount / scale
    left = (amount_left / scale) if amount_left is not None else 0.0
    if 0 < total < 0.01:
        total = 0.01
    if 0 < left < 0.01:
        left = 0.01
    used_percent = max(0.0, min(100.0, (1.0 - left / total) * 100.0))
    currency = "USD"
    monthly_limit = wallet.get("monthlyChargeLimit") if isinstance(wallet.get("monthlyChargeLimit"), dict) else {}
    monthly_used = wallet.get("monthlyUsed") if isinstance(wallet.get("monthlyUsed"), dict) else {}
    for bag in (monthly_limit, monthly_used):
        code = bag.get("currency")
        if isinstance(code, str) and code.strip():
            currency = code.strip()
            break
    symbol = "$" if currency.upper() == "USD" else f"{currency} "
    return _win("Extra usage", used_percent, None, f"{symbol}{left:.2f} of {symbol}{total:.2f} left")


def _fetch_kimi_account_usage() -> Optional[dict]:
    token = _kimi_code_access_token()
    if not token:
        return None
    import httpx

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    with httpx.Client(timeout=15.0) as client:
        response = client.get(KIMI_CODE_USAGE_URL, headers=headers)
        if response.status_code == 401:
            token = _kimi_code_access_token(force_refresh=True)
            if not token:
                return None
            headers["Authorization"] = f"Bearer {token}"
            response = client.get(KIMI_CODE_USAGE_URL, headers=headers)
        response.raise_for_status()
        payload = response.json() or {}
    if not isinstance(payload, dict):
        return None
    windows: list[dict] = []
    weekly = _kimi_usage_window(payload.get("usage"), label="Weekly")
    if weekly:
        windows.append(weekly)
    raw_limits = payload.get("limits")
    if isinstance(raw_limits, list):
        for item in raw_limits:
            if not isinstance(item, dict):
                continue
            label = _kimi_window_label(
                item.get("window") if isinstance(item.get("window"), dict) else None,
                fallback="Limit",
            )
            row = _kimi_usage_window(item.get("detail"), label=label)
            if row:
                windows.append(row)
    extra = _kimi_extra_usage_window(payload)
    if extra:
        windows.append(extra)
    if not windows:
        return None
    return _snapshot("kimi", infer_kimi_plan_name(payload), windows)


def _grok_home() -> Path:
    override = (os.environ.get("GROK_HOME") or "").strip()
    if override:
        return Path(override).expanduser()
    return _user_home() / ".grok"


def _grok_auth_path() -> Optional[Path]:
    path = _grok_home() / "auth.json"
    return path if path.is_file() else None


def _grok_client_version() -> str:
    path = _grok_home() / "version.json"
    if path.is_file():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeError):
            payload = {}
        if isinstance(payload, dict):
            version = payload.get("version") or payload.get("stable_version")
            if isinstance(version, str) and version.strip():
                return version.strip()
    return "1.0.5"


def _grok_read_auth() -> Optional[tuple[Path, str, dict, dict]]:
    path = _grok_auth_path()
    if not path:
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return None
    if not isinstance(payload, dict):
        return None
    chosen_key = None
    chosen = None
    for key, entry in payload.items():
        if not isinstance(entry, dict):
            continue
        token = entry.get("key") or entry.get("access_token")
        if not isinstance(token, str) or not token.strip():
            continue
        issuer = str(entry.get("oidc_issuer") or key or "")
        if chosen is None or "auth.x.ai" in issuer:
            chosen_key = str(key)
            chosen = entry
            if "auth.x.ai" in issuer:
                break
    if not chosen_key or not chosen:
        return None
    return path, chosen_key, payload, chosen


def _grok_token_expired(entry: dict, *, skew: int = GROK_TOKEN_SKEW_SECONDS) -> bool:
    expires = _parse_dt(entry.get("expires_at"))
    if expires is None:
        return True
    return expires.timestamp() <= (_utc_now().timestamp() + skew)


def _grok_refresh_entry(entry: dict) -> Optional[dict]:
    refresh = entry.get("refresh_token")
    if not isinstance(refresh, str) or not refresh.strip():
        return None
    import httpx

    client_id = str(entry.get("oidc_client_id") or GROK_OAUTH_CLIENT_ID).strip() or GROK_OAUTH_CLIENT_ID
    with httpx.Client(timeout=15.0) as client:
        response = client.post(
            GROK_OAUTH_TOKEN_URL,
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
            data={
                "grant_type": "refresh_token",
                "client_id": client_id,
                "refresh_token": refresh.strip(),
            },
        )
        if response.status_code >= 400:
            return None
        body = response.json() or {}
    if not isinstance(body, dict):
        return None
    access = body.get("access_token")
    if not isinstance(access, str) or not access.strip():
        return None
    updated = dict(entry)
    updated["key"] = access.strip()
    new_refresh = body.get("refresh_token")
    if isinstance(new_refresh, str) and new_refresh.strip():
        updated["refresh_token"] = new_refresh.strip()
    expires_in = _to_int(body.get("expires_in")) or 21600
    updated["expires_at"] = (
        datetime.fromtimestamp(_utc_now().timestamp() + expires_in, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )
    return updated


def _grok_access_context(*, force_refresh: bool = False) -> Optional[tuple[str, str]]:
    loaded = _grok_read_auth()
    if not loaded:
        return None
    path, map_key, payload, entry = loaded
    token = entry.get("key") or entry.get("access_token")
    user_id = str(entry.get("user_id") or entry.get("principal_id") or "").strip()
    if isinstance(token, str) and token.strip() and user_id and not force_refresh and not _grok_token_expired(entry):
        return token.strip(), user_id
    loaded = _grok_read_auth() or loaded
    path, map_key, payload, entry = loaded
    token = entry.get("key") or entry.get("access_token")
    user_id = str(entry.get("user_id") or entry.get("principal_id") or "").strip()
    if isinstance(token, str) and token.strip() and user_id and not _grok_token_expired(entry):
        return token.strip(), user_id
    refreshed = _grok_refresh_entry(entry)
    if not refreshed:
        return None
    payload[map_key] = refreshed
    try:
        _write_json_file(path, payload)
    except OSError:
        pass
    access = refreshed.get("key")
    user_id = str(refreshed.get("user_id") or refreshed.get("principal_id") or user_id or "").strip()
    if isinstance(access, str) and access.strip() and user_id:
        return access.strip(), user_id
    return None


def _grok_proxy_headers(token: str, user_id: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
        "X-XAI-Token-Auth": "xai-grok-cli",
        "x-userid": user_id,
        "x-grok-client-version": _grok_client_version(),
        "x-grok-client-mode": "cli",
    }


def _grok_cent(value: Any) -> Optional[int]:
    if isinstance(value, dict):
        return _to_int(value.get("val"))
    return _to_int(value)


def _grok_period_label(period: Optional[dict]) -> str:
    if not isinstance(period, dict):
        return "Weekly"
    kind = str(period.get("type") or period.get("period_type") or "").upper()
    if "MONTH" in kind:
        return "Monthly"
    if "DAY" in kind:
        return "Daily"
    return "Weekly"


def _grok_product_label(name: str) -> str:
    text = str(name or "").strip()
    mapping = {
        "GrokBuild": "Build",
        "PRODUCT_GROK_BUILD": "Build",
        "GrokChat": "Chat",
        "PRODUCT_GROK_CHAT": "Chat",
    }
    if text in mapping:
        return mapping[text]
    if text.lower().startswith("grok"):
        text = text[4:]
    return _title_case_slug(text) or "Usage"


def _fetch_grok_account_usage() -> Optional[dict]:
    import httpx

    context = _grok_access_context()
    if not context:
        return None
    token, user_id = context
    headers = _grok_proxy_headers(token, user_id)
    base = (os.environ.get("GROK_CLI_CHAT_PROXY_BASE_URL") or "").strip().rstrip("/")
    billing_url = f"{base}/billing?format=credits" if base else GROK_BILLING_URL
    settings_url = f"{base}/settings" if base else GROK_SETTINGS_URL
    with httpx.Client(timeout=15.0) as client:
        response = client.get(billing_url, headers=headers)
        if response.status_code == 401:
            context = _grok_access_context(force_refresh=True)
            if not context:
                return None
            token, user_id = context
            headers = _grok_proxy_headers(token, user_id)
            response = client.get(billing_url, headers=headers)
        response.raise_for_status()
        payload = response.json() or {}
        settings: dict = {}
        try:
            settings_resp = client.get(settings_url, headers=headers)
            if settings_resp.status_code < 400:
                loaded = settings_resp.json() or {}
                if isinstance(loaded, dict):
                    settings = loaded
        except Exception:
            settings = {}
    if not isinstance(payload, dict):
        return None
    config = payload.get("config") if isinstance(payload.get("config"), dict) else payload
    windows: list[dict] = []
    period = config.get("currentPeriod") if isinstance(config.get("currentPeriod"), dict) else None
    reset_at = _parse_dt((period or {}).get("end") or config.get("billingPeriodEnd"))
    used_pct = config.get("creditUsagePercent")
    if isinstance(used_pct, (int, float)) and math.isfinite(used_pct):
        windows.append(_win(_grok_period_label(period), max(0.0, min(100.0, float(used_pct))), reset_at))
    else:
        limit = _grok_cent(config.get("monthlyLimit"))
        used = _grok_cent(config.get("used"))
        if limit is not None and limit > 0 and used is not None:
            windows.append(
                _win(
                    _grok_period_label(period),
                    max(0.0, min(100.0, used / float(limit) * 100.0)),
                    reset_at,
                    f"${used / 100:.2f} of ${limit / 100:.2f} used",
                )
            )
    products = config.get("productUsage")
    if isinstance(products, list):
        for item in products:
            if not isinstance(item, dict):
                continue
            pct = item.get("usagePercent")
            if not isinstance(pct, (int, float)) or not math.isfinite(pct):
                continue
            windows.append(
                _win(
                    _grok_product_label(str(item.get("product") or "")),
                    max(0.0, min(100.0, float(pct))),
                    reset_at,
                )
            )
    prepaid = _grok_cent(config.get("prepaidBalance"))
    if prepaid is not None and prepaid > 0:
        windows.append(_win("Prepaid", 0.0, None, f"${prepaid / 100:.2f} left"))
    demand_cap = _grok_cent(config.get("onDemandCap"))
    demand_used = _grok_cent(config.get("onDemandUsed"))
    if demand_cap is not None and demand_cap > 0 and demand_used is not None:
        windows.append(
            _win(
                "On demand",
                max(0.0, min(100.0, demand_used / float(demand_cap) * 100.0)),
                reset_at,
                f"${demand_used / 100:.2f} of ${demand_cap / 100:.2f} used",
            )
        )
    if not windows:
        return None
    plan = settings.get("subscription_tier_display") or payload.get("subscriptionTier")
    if isinstance(plan, str):
        plan = plan.strip() or None
    else:
        plan = None
    return _snapshot("grok", plan, windows)


def _collect_hermes() -> list[dict]:
    try:
        from agent.account_usage import fetch_account_usage
    except Exception:
        return []
    snapshots = []
    for provider in HERMES_PROVIDERS:
        try:
            snap = fetch_account_usage(provider)
        except Exception:
            continue
        if not snap or not getattr(snap, "available", False):
            continue
        snapshots.append(
            {
                "provider": snap.provider,
                "plan": snap.plan,
                "details": list(snap.details or ()),
                "windows": [_hermes_window(item) for item in (snap.windows or ())],
            }
        )
    return snapshots


def _collect_cli() -> list[dict]:
    snapshots = []
    for fetch in (_fetch_cursor_account_usage, _fetch_kimi_account_usage, _fetch_grok_account_usage):
        try:
            snap = fetch()
        except Exception:
            continue
        if snap and snap.get("windows"):
            snapshots.append(snap)
    return snapshots


def main() -> int:
    cli_only = "--cli-only" in sys.argv
    snapshots = []
    if not cli_only:
        snapshots.extend(_collect_hermes())
    snapshots.extend(_collect_cli())
    json.dump(snapshots, sys.stdout, ensure_ascii=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
