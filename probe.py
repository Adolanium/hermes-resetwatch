"""Stock Hermes usage probe for Resetwatch.

Prints JSON snapshots for Claude, Codex, and OpenRouter using fetchers
the gateway already ships. If Hermes OAuth is missing, Claude Code and
Codex CLI logins fill those same cards. Cursor, Kimi, Grok, and GLM
(ZCode) come from those logins. When CLI login is missing, Kimi Coding
(KIMI_CODING_API_KEY / KIMI_API_KEY) and GLM (ZAI_API_KEY / GLM_API_KEY)
fall back to Hermes env. DeepSeek (DEEPSEEK_API_KEY), OpenCode Go
(OPENCODE_GO_API_KEY), and Ollama Cloud (OLLAMA_API_KEY) always use
Hermes env.

Read-only for Claude and Codex credentials: the probe never exchanges
those refresh tokens or writes ~/.claude / ~/.codex. Kimi and Grok may
refresh on 401 and write back only that vendor's file (re-read first so
a concurrent CLI refresh wins). It may also write a small cache under
$HERMES_HOME/cache/resetwatch, including a 5-minute probe result cache
so vendor APIs are not hit more often than that (pass --fresh to bypass).
No tokens on stdout.

Vendor usage rows call undocumented private APIs with the same client
identity those CLIs use (Claude Code, Codex, Grok CLI). Those rows are
best-effort and can break when a vendor changes its API.
"""

from __future__ import annotations

import base64
import json
import math
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional


# Cap how often probe hits vendor APIs (success or empty). Claude 429 may
# keep a longer Retry-After on top of this.
PROBE_MIN_INTERVAL_SECONDS = 5 * 60

HERMES_PROVIDERS = ("openai-codex", "openrouter")
# Anthropic/Claude is owned by _fetch_claude_cli_account_usage so we can
# honor 429 Retry-After and reuse a local cache instead of hammering OAuth usage.
USER_AGENT = "resetwatch"

CLAUDE_OAUTH_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CLAUDE_OAUTH_PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"

CODEX_DEFAULT_BASE_URL = "https://chatgpt.com/backend-api/codex"
CODEX_TOKEN_SKEW_SECONDS = 120

DEEPSEEK_BALANCE_URL = "https://api.deepseek.com/user/balance"
# Official DeepSeek peak windows (UTC). Off-peak is half price.
DEEPSEEK_PEAK_WINDOWS_UTC = ((1, 4), (6, 10))

OPENCODE_GO_DEFAULT_BASE_URL = "https://opencode.ai/zen/go/v1"

OLLAMA_CLOUD_USAGE_URL = "https://ollama.com/api/usage"
OLLAMA_CLOUD_ME_URL = "https://ollama.com/api/me"

# Official GLM Coding Plan peak window: Mon-Fri 14:00-18:00 Singapore (UTC+8).
# Off-peak credits cost 50%. Fixed +08:00 works the same on Mac and Windows.
GLM_PEAK_TZ = timezone(timedelta(hours=8))
GLM_PEAK_WEEKDAYS = frozenset({0, 1, 2, 3, 4})  # Monday-Friday
GLM_PEAK_START_HOUR = 14
GLM_PEAK_END_HOUR = 18

# Claude / Codex / Grok usage calls use those products' private APIs and
# client headers. Best-effort only; vendors can change or reject them.

CURSOR_PERIOD_USAGE_URL = "https://api2.cursor.sh/aiserver.v1.DashboardService/GetCurrentPeriodUsage"

KIMI_CODE_CLIENT_ID = "17e5f671-d194-4dfb-9706-5516cb48c098"
KIMI_CODE_OAUTH_TOKEN_URL = "https://auth.kimi.com/api/oauth/token"
KIMI_CODE_USAGE_URL = "https://api.kimi.com/coding/v1/usages"

GROK_BILLING_URL = "https://cli-chat-proxy.grok.com/v1/billing?format=credits"
GROK_SETTINGS_URL = "https://cli-chat-proxy.grok.com/v1/settings"
GROK_OAUTH_TOKEN_URL = "https://auth.x.ai/oauth2/token"
GROK_OAUTH_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"


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


def _provider_key(name: Any) -> str:
    key = str(name or "").strip().lower()
    if key == "kimi-coding":
        return "kimi"
    if key in {"xai-oauth", "xai"}:
        return "grok"
    if key in {"zai", "zcode", "zhipu", "glm-coding", "zai-coding-plan"}:
        return "glm"
    if key in {"deep-seek"}:
        return "deepseek"
    if key in {"opencode_go", "opencode-go-sub", "go"}:
        return "opencode-go"
    if key in {"ollama-cloud", "ollama_cloud"}:
        return "ollama"
    return key


def _is_claude_oauth_token(token: str) -> bool:
    if not token:
        return False
    if token.startswith("sk-ant-api"):
        return False
    if token.startswith("sk-ant-") or token.startswith("eyJ") or token.startswith("cc-"):
        return True
    return False


def _resetwatch_cache_dir() -> Path:
    for home in _hermes_homes():
        path = home / "cache" / "resetwatch"
        try:
            path.mkdir(parents=True, exist_ok=True)
            return path
        except Exception:
            continue
    fallback = Path.home() / ".hermes" / "cache" / "resetwatch"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def _anthropic_cache_path() -> Path:
    return _resetwatch_cache_dir() / "anthropic_usage.json"


def _anthropic_ratelimit_path() -> Path:
    return _resetwatch_cache_dir() / "anthropic_usage.ratelimit"


def _read_json_file(path: Path) -> Optional[Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload


def _write_cache_json(path: Path, payload: Any) -> None:
    """Best-effort cache write. Never used for vendor credential files."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=True), encoding="utf-8")
    except Exception:
        return


def _write_secret_json(path: Path, payload: dict) -> None:
    """Atomic write for Kimi/Grok credential files only."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _probe_result_cache_path(*, cli_only: bool) -> Path:
    name = "probe_snapshots.cli.json" if cli_only else "probe_snapshots.full.json"
    return _resetwatch_cache_dir() / name


def _read_probe_result_cache(*, cli_only: bool, max_age: float = PROBE_MIN_INTERVAL_SECONDS) -> Optional[list]:
    payload = _read_json_file(_probe_result_cache_path(cli_only=cli_only))
    if not isinstance(payload, dict):
        return None
    fetched_at = payload.get("fetched_at")
    snapshots = payload.get("snapshots")
    if not isinstance(fetched_at, (int, float)) or not isinstance(snapshots, list):
        return None
    age = datetime.now(timezone.utc).timestamp() - float(fetched_at)
    if age < 0 or age > float(max_age):
        return None
    return snapshots


def _store_probe_result_cache(snapshots: list, *, cli_only: bool) -> None:
    _write_cache_json(
        _probe_result_cache_path(cli_only=cli_only),
        {
            "fetched_at": datetime.now(timezone.utc).timestamp(),
            "snapshots": snapshots,
        },
    )


def _anthropic_ratelimit_remaining() -> float:
    payload = _read_json_file(_anthropic_ratelimit_path())
    if not isinstance(payload, dict):
        return 0.0
    until = payload.get("until")
    if not isinstance(until, (int, float)):
        return 0.0
    return max(0.0, float(until) - datetime.now(timezone.utc).timestamp())


def _mark_anthropic_ratelimit(retry_after: float) -> None:
    seconds = max(30.0, min(float(retry_after or 60.0), 6 * 60 * 60))
    _write_cache_json(
        _anthropic_ratelimit_path(),
        {"until": datetime.now(timezone.utc).timestamp() + seconds, "retry_after": seconds},
    )


def _clear_anthropic_ratelimit() -> None:
    try:
        _anthropic_ratelimit_path().unlink(missing_ok=True)
    except Exception:
        return


def _cached_anthropic_snapshot() -> Optional[dict]:
    payload = _read_json_file(_anthropic_cache_path())
    if not isinstance(payload, dict) or _provider_key(payload.get("provider")) != "anthropic":
        return None
    if not (payload.get("windows") or payload.get("details")):
        return None
    return payload


def _store_anthropic_snapshot(snap: dict) -> None:
    if not isinstance(snap, dict):
        return
    _write_cache_json(_anthropic_cache_path(), snap)


def _hermes_anthropic_oauth_token() -> Optional[str]:
    try:
        from agent.anthropic_adapter import resolve_anthropic_token

        token = (resolve_anthropic_token() or "").strip()
        if token and _is_claude_oauth_token(token):
            return token
    except Exception:
        pass
    for home in _hermes_homes():
        path = home / ".anthropic_oauth.json"
        payload = _read_json_file(path)
        if not isinstance(payload, dict):
            continue
        token = str(payload.get("accessToken") or payload.get("access_token") or "").strip()
        if token and _is_claude_oauth_token(token):
            return token
    return None


def _jwt_exp(token: str) -> Optional[float]:
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return None
        payload = parts[1] + ("=" * (-len(parts[1]) % 4))
        data = json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return None
    exp = data.get("exp") if isinstance(data, dict) else None
    if isinstance(exp, (int, float)):
        return float(exp)
    return None


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
    # GetCurrentPeriodUsage planUsage / spendLimitUsage amounts are USD cents.
    return f"${float(value) / 100.0:.2f}"


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


def _kimi_code_access_token(*, previous: Optional[str] = None, allow_refresh: bool = False) -> Optional[str]:
    """Return a Kimi access token. Refresh+write only when allow_refresh and disk still has previous."""
    path = _kimi_code_credentials_path()
    if not path:
        return None
    creds = _kimi_code_read_credentials(path)
    if not creds:
        return None
    token = creds.get("access_token")
    token = token.strip() if isinstance(token, str) and token.strip() else None
    if token and (not previous or token != previous.strip()):
        return token
    if not allow_refresh:
        return None
    refreshed = _kimi_code_refresh_tokens(creds)
    if not refreshed:
        return None
    try:
        _write_secret_json(path, refreshed)
    except OSError:
        pass
    access = refreshed.get("access_token")
    return access.strip() if isinstance(access, str) and access.strip() else None


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


def _kimi_snapshot_from_payload(payload: dict) -> Optional[dict]:
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


def _fetch_kimi_cli_usage() -> Optional[dict]:
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
            token = _kimi_code_access_token(previous=token, allow_refresh=True)
            if not token:
                return None
            headers["Authorization"] = f"Bearer {token}"
            response = client.get(KIMI_CODE_USAGE_URL, headers=headers)
        response.raise_for_status()
        payload = response.json() or {}
    if not isinstance(payload, dict):
        return None
    return _kimi_snapshot_from_payload(payload)


def _kimi_coding_api_key() -> Optional[str]:
    for name in ("KIMI_CODING_API_KEY", "KIMI_API_KEY"):
        value = _hermes_env_value(name)
        if value:
            return value
    return None


def _fetch_kimi_coding_api_key_usage() -> Optional[dict]:
    token = _kimi_coding_api_key()
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
        response.raise_for_status()
        payload = response.json() or {}
    if not isinstance(payload, dict):
        return None
    return _kimi_snapshot_from_payload(payload)


def _fetch_kimi_account_usage() -> Optional[dict]:
    # CLI OAuth first (can refresh on 401). Hermes Coding Plan key is fallback.
    try:
        snap = _fetch_kimi_cli_usage()
        if snap:
            return snap
    except Exception:
        pass
    return _fetch_kimi_coding_api_key_usage()


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


def _grok_access_context(
    *, previous: Optional[str] = None, allow_refresh: bool = False
) -> Optional[tuple[str, str]]:
    """Return Grok access token + user id. Refresh+write only when allow_refresh and disk still has previous."""
    loaded = _grok_read_auth()
    if not loaded:
        return None
    path, map_key, payload, entry = loaded
    token = entry.get("key") or entry.get("access_token")
    user_id = str(entry.get("user_id") or entry.get("principal_id") or "").strip()
    token = token.strip() if isinstance(token, str) and token.strip() else None
    if token and user_id and (not previous or token != previous.strip()):
        return token, user_id
    if not allow_refresh:
        return None
    refreshed = _grok_refresh_entry(entry)
    if not refreshed:
        return None
    payload[map_key] = refreshed
    try:
        _write_secret_json(path, payload)
    except OSError:
        pass
    access = refreshed.get("key")
    user_id = str(refreshed.get("user_id") or refreshed.get("principal_id") or user_id or "").strip()
    if isinstance(access, str) and access.strip() and user_id:
        return access.strip(), user_id
    return None


def _grok_proxy_headers(token: str, user_id: str) -> dict[str, str]:
    # Private Grok CLI billing API. Best-effort; xAI may change or reject this.
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
            context = _grok_access_context(previous=token, allow_refresh=True)
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


def _claude_home() -> Path:
    override = (os.environ.get("CLAUDE_CONFIG_DIR") or "").strip()
    if override:
        return Path(override).expanduser()
    return _user_home() / ".claude"


def _claude_credentials_path() -> Path:
    return _claude_home() / ".credentials.json"


def _claude_oauth_from_payload(payload: Any, *, source: str) -> Optional[dict]:
    if not isinstance(payload, dict):
        return None
    oauth = payload.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        return None
    token = oauth.get("accessToken")
    if not isinstance(token, str) or not token.strip():
        return None
    return {
        "accessToken": token.strip(),
        "refreshToken": str(oauth.get("refreshToken") or "").strip(),
        "expiresAt": oauth.get("expiresAt") or 0,
        "source": source,
        "raw": payload,
    }


def _read_claude_code_file() -> Optional[dict]:
    path = _claude_credentials_path()
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return None
    return _claude_oauth_from_payload(payload, source="file")


def _read_generic_windows_credential(target: str) -> Optional[str]:
    if not _is_windows():
        return None
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return None

    class FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

    class CREDENTIAL(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.c_void_p),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    cred_read = advapi32.CredReadW
    cred_read.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.POINTER(CREDENTIAL)),
    ]
    cred_read.restype = wintypes.BOOL
    cred_free = advapi32.CredFree
    cred_free.argtypes = [ctypes.c_void_p]
    cred_ptr = ctypes.POINTER(CREDENTIAL)()
    if not cred_read(target, 1, 0, ctypes.byref(cred_ptr)):
        return None
    try:
        blob = ctypes.string_at(cred_ptr.contents.CredentialBlob, cred_ptr.contents.CredentialBlobSize)
    finally:
        cred_free(cred_ptr)
    if not blob:
        return None
    for encoding in ("utf-16-le", "utf-8"):
        try:
            text = blob.decode(encoding).rstrip("\x00").strip()
        except UnicodeError:
            continue
        if text:
            return text
    return None


def _claude_windows_cred_targets() -> list[str]:
    targets = ["Claude Code-credentials"]
    try:
        import hashlib

        digest = hashlib.sha256(str(_claude_home().resolve()).encode("utf-8")).hexdigest()[:8]
        targets.append(f"Claude Code-credentials-{digest}")
    except Exception:
        pass
    return targets


def _read_claude_code_os_store() -> Optional[dict]:
    if _is_macos():
        try:
            result = subprocess.run(
                ["/usr/bin/security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
                stdin=subprocess.DEVNULL,
            )
        except Exception:
            return None
        raw = (result.stdout or "").strip()
        if result.returncode != 0 or not raw:
            return None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return _claude_oauth_from_payload(payload, source="keychain")
    if _is_windows():
        for target in _claude_windows_cred_targets():
            raw = _read_generic_windows_credential(target)
            if not raw:
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue
            creds = _claude_oauth_from_payload(payload, source="windows_credential")
            if creds:
                return creds
    return None


def _claude_token_valid(creds: dict) -> bool:
    token = creds.get("accessToken")
    if not isinstance(token, str) or not token.strip():
        return False
    expires_at = creds.get("expiresAt") or 0
    if not expires_at:
        return True
    try:
        expires_ms = float(expires_at)
    except (TypeError, ValueError):
        return True
    now_ms = _utc_now().timestamp() * 1000
    return now_ms < (expires_ms - 60_000)


def _read_claude_code_credentials() -> Optional[dict]:
    keychain = _read_claude_code_os_store()
    file_creds = _read_claude_code_file()
    if keychain and file_creds:
        key_ok = _claude_token_valid(keychain)
        file_ok = _claude_token_valid(file_creds)
        if key_ok and not file_ok:
            return keychain
        if file_ok and not key_ok:
            return file_creds
        key_exp = keychain.get("expiresAt") or 0
        file_exp = file_creds.get("expiresAt") or 0
        return keychain if key_exp >= file_exp else file_creds
    return keychain or file_creds


def _claude_code_access_token() -> Optional[str]:
    """Return a live Claude Code access token. Never refreshes or writes creds."""
    creds = _read_claude_code_credentials()
    if not creds:
        return None
    token = str(creds.get("accessToken") or "").strip()
    if token and _claude_token_valid(creds) and _is_claude_oauth_token(token):
        return token
    return None


def infer_claude_plan_name(profile: Optional[dict] = None, usage_payload: Optional[dict] = None) -> Optional[str]:
    profile = profile or {}
    usage_payload = usage_payload or {}
    org = profile.get("organization") if isinstance(profile.get("organization"), dict) else {}
    account = profile.get("account") if isinstance(profile.get("account"), dict) else {}
    tier = str(org.get("rate_limit_tier") or "").strip().lower()
    org_type = str(org.get("organization_type") or "").strip().lower()
    if "max_20x" in tier or tier.endswith("_20x"):
        return "Max 20x"
    if "max_5x" in tier or tier.endswith("_5x"):
        return "Max 5x"
    if "max" in tier or org_type == "claude_max" or account.get("has_claude_max"):
        return "Max"
    if "pro" in tier or org_type in {"claude_pro", "claude_ai"} or account.get("has_claude_pro"):
        return "Pro"
    if "team" in tier or org_type == "claude_team":
        return "Team"
    if "enterprise" in tier or org_type == "claude_enterprise":
        return "Enterprise"
    if usage_payload.get("seven_day_opus"):
        return "Max"
    extra = usage_payload.get("extra_usage") if isinstance(usage_payload.get("extra_usage"), dict) else {}
    if extra.get("is_enabled"):
        return "Max"
    if usage_payload.get("five_hour") or usage_payload.get("seven_day"):
        return "Pro"
    return None


def _anthropic_rate_limit_snapshot(remaining: float) -> dict:
    mins = max(1, int((remaining + 59) // 60))
    return _snapshot(
        "anthropic",
        None,
        [],
        [f"Usage API rate-limited · try again in ~{mins}m"],
    )


def _fetch_claude_cli_account_usage() -> Optional[dict]:
    remaining = _anthropic_ratelimit_remaining()
    if remaining > 0:
        return _cached_anthropic_snapshot() or _anthropic_rate_limit_snapshot(remaining)

    token = _claude_code_access_token() or _hermes_anthropic_oauth_token()
    if not token:
        return _cached_anthropic_snapshot()

    import httpx

    headers = {
        # Private Anthropic OAuth usage API, same shape Claude Code uses.
        # Best-effort; Anthropic may change or rate-limit this.
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": "claude-code/2.1.0",
    }
    profile: dict = {}
    try:
        with httpx.Client(timeout=15.0) as client:
            response = client.get(CLAUDE_OAUTH_USAGE_URL, headers=headers)
            if response.status_code == 429:
                retry_after = response.headers.get("retry-after")
                try:
                    wait = float(retry_after) if retry_after else 3600.0
                except Exception:
                    wait = 3600.0
                _mark_anthropic_ratelimit(wait)
                return _cached_anthropic_snapshot() or _anthropic_rate_limit_snapshot(wait)
            response.raise_for_status()
            payload = response.json() or {}
            try:
                profile_resp = client.get(CLAUDE_OAUTH_PROFILE_URL, headers=headers)
                if profile_resp.status_code < 400:
                    loaded = profile_resp.json() or {}
                    if isinstance(loaded, dict):
                        profile = loaded
            except Exception:
                profile = {}
    except Exception:
        return _cached_anthropic_snapshot()

    if not isinstance(payload, dict):
        return _cached_anthropic_snapshot()
    windows: list[dict] = []
    mapping = (
        ("five_hour", "Current session"),
        ("seven_day", "Current week"),
        ("seven_day_opus", "Opus week"),
        ("seven_day_sonnet", "Sonnet week"),
    )
    for key, label in mapping:
        window = payload.get(key) if isinstance(payload.get(key), dict) else {}
        util = window.get("utilization")
        if util is None:
            continue
        used = float(util) * 100 if float(util) <= 1 else float(util)
        windows.append(_win(label, max(0.0, min(100.0, used)), _parse_dt(window.get("resets_at"))))
    details: list[str] = []
    extra = payload.get("extra_usage") if isinstance(payload.get("extra_usage"), dict) else {}
    if extra.get("is_enabled"):
        used_credits = extra.get("used_credits")
        monthly_limit = extra.get("monthly_limit")
        currency = extra.get("currency") or "USD"
        if isinstance(used_credits, (int, float)) and isinstance(monthly_limit, (int, float)):
            details.append(f"Extra usage: {used_credits:.2f} / {monthly_limit:.2f} {currency}")
    if not windows and not details:
        return _cached_anthropic_snapshot()
    _clear_anthropic_ratelimit()
    snap = _snapshot("anthropic", infer_claude_plan_name(profile, payload), windows, details)
    _store_anthropic_snapshot(snap)
    return snap


def _codex_home() -> Path:
    override = (os.environ.get("CODEX_HOME") or "").strip()
    if override:
        return Path(override).expanduser()
    return _user_home() / ".codex"


def _codex_auth_path() -> Path:
    return _codex_home() / "auth.json"


def _codex_token_expiring(token: str, *, skew: int = CODEX_TOKEN_SKEW_SECONDS) -> bool:
    exp = _jwt_exp(token)
    if exp is None:
        return False
    return exp <= (_utc_now().timestamp() + skew)


def _read_codex_cli_auth() -> Optional[tuple[Path, dict, dict]]:
    path = _codex_auth_path()
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return None
    if not isinstance(payload, dict):
        return None
    tokens = payload.get("tokens")
    if not isinstance(tokens, dict):
        return None
    access = tokens.get("access_token")
    if not isinstance(access, str) or not access.strip():
        return None
    return path, payload, tokens


def _codex_cli_access_context() -> Optional[tuple[str, Optional[str]]]:
    """Return Codex CLI access token. Never refreshes or writes ~/.codex."""
    loaded = _read_codex_cli_auth()
    if not loaded:
        return None
    _path, _payload, tokens = loaded
    access = str(tokens.get("access_token") or "").strip()
    account_id = str(tokens.get("account_id") or "").strip() or None
    if access and not _codex_token_expiring(access):
        return access, account_id
    return None


def _codex_usage_url(base_url: str) -> str:
    normalized = (base_url or "").strip().rstrip("/")
    if not normalized:
        normalized = CODEX_DEFAULT_BASE_URL
    if normalized.endswith("/codex"):
        normalized = normalized[: -len("/codex")]
    prefix = normalized + ("/wham" if "/backend-api" in normalized else "/api/codex")
    return prefix + "/usage"


def _fetch_codex_cli_account_usage() -> Optional[dict]:
    context = _codex_cli_access_context()
    if not context:
        return None
    token, account_id = context
    if not token:
        return None
    import httpx

    headers = {
        # Private Codex usage API. Best-effort; OpenAI may change or reject this.
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": "codex-cli",
    }
    if account_id:
        headers["ChatGPT-Account-Id"] = account_id
    with httpx.Client(timeout=15.0) as client:
        response = client.get(_codex_usage_url(CODEX_DEFAULT_BASE_URL), headers=headers)
        response.raise_for_status()
        payload = response.json() or {}
    if not isinstance(payload, dict):
        return None
    rate_limit = payload.get("rate_limit") if isinstance(payload.get("rate_limit"), dict) else {}
    windows: list[dict] = []
    for key, label in (("primary_window", "Session"), ("secondary_window", "Weekly")):
        window = rate_limit.get(key) if isinstance(rate_limit.get(key), dict) else {}
        used = window.get("used_percent")
        if used is None:
            continue
        windows.append(_win(label, float(used), _parse_dt(window.get("reset_at"))))
    details: list[str] = []
    reset_credits = payload.get("rate_limit_reset_credits") if isinstance(payload.get("rate_limit_reset_credits"), dict) else {}
    banked = reset_credits.get("available_count")
    if isinstance(banked, (int, float)) and int(banked) > 0:
        count = int(banked)
        plural = "s" if count != 1 else ""
        details.append(f"You have {count} reset{plural} banked")
    credits = payload.get("credits") if isinstance(payload.get("credits"), dict) else {}
    if credits.get("has_credits"):
        balance = credits.get("balance")
        if isinstance(balance, (int, float)):
            details.append(f"Credits balance: ${float(balance):.2f}")
        elif credits.get("unlimited"):
            details.append("Credits balance: unlimited")
    if not windows and not details:
        return None
    plan = _title_case_slug(payload.get("plan_type"))
    return _snapshot("openai-codex", plan, windows, details)


ZCODE_CODING_PROVIDER_IDS = ("builtin:zai-coding-plan", "builtin:bigmodel-coding-plan")


def _zcode_roots() -> list[Path]:
    roots: list[Path] = []
    override = (os.environ.get("ZCODE_HOME") or "").strip()
    if override:
        roots.append(Path(override).expanduser())
    roots.append(_user_home() / ".zcode")
    if _is_macos():
        roots.append(_user_home() / "Library" / "Application Support" / "ZCode")
    if _is_windows():
        for env_name in ("APPDATA", "LOCALAPPDATA"):
            base = os.environ.get(env_name) or ""
            if base:
                roots.append(Path(base) / "ZCode")
    unique: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = str(root)
        if key in seen:
            continue
        seen.add(key)
        unique.append(root)
    return unique


def _zcode_read_json(path: Path) -> Optional[dict]:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _zcode_selected_provider_id(settings: dict) -> Optional[str]:
    selected_map = settings.get("modelProviderFamilySelectedKeys")
    if not isinstance(selected_map, dict):
        return None
    selected = selected_map.get("zai") or selected_map.get("glm") or selected_map.get("zhipu")
    text = str(selected or "").strip()
    if not text:
        return None
    if text.startswith("coding-plan:"):
        text = text.split(":", 1)[1].strip()
    return text or None


def _zcode_provider_key(providers: dict, provider_id: str) -> Optional[tuple[str, str]]:
    entry = providers.get(provider_id)
    if not isinstance(entry, dict):
        return None
    options = entry.get("options") if isinstance(entry.get("options"), dict) else {}
    token = options.get("apiKey")
    if not isinstance(token, str) or not token.strip():
        return None
    base = str(options.get("baseURL") or "").strip()
    return token.strip(), base


def _zcode_coding_credentials() -> Optional[tuple[str, str]]:
    """Return (api_key, base_url) from a ZCode Coding Plan login. Never other providers."""
    for root in _zcode_roots():
        config = _zcode_read_json(root / "v2" / "config.json") or _zcode_read_json(root / "cli" / "config.json")
        if not config:
            continue
        providers = config.get("provider") if isinstance(config.get("provider"), dict) else {}
        if not providers:
            continue
        settings = _zcode_read_json(root / "v2" / "setting.json") or {}
        ordered: list[str] = []
        selected = _zcode_selected_provider_id(settings)
        if selected:
            ordered.append(selected)
        ordered.extend(ZCODE_CODING_PROVIDER_IDS)
        seen: set[str] = set()
        for provider_id in ordered:
            if provider_id in seen:
                continue
            seen.add(provider_id)
            if provider_id not in ZCODE_CODING_PROVIDER_IDS:
                continue
            loaded = _zcode_provider_key(providers, provider_id)
            if loaded:
                return loaded
    return None


def _zai_monitor_base(base_url: str) -> str:
    text = (base_url or "").lower()
    if "bigmodel.cn" in text:
        return "https://open.bigmodel.cn"
    return "https://api.z.ai"


def _glm_window_label(item: dict) -> str:
    kind = str(item.get("type") or "").strip().upper()
    unit = _to_int(item.get("unit"))
    number = _to_int(item.get("number"))
    if kind in {"CREDIT_LIMIT", "TOKENS_LIMIT"}:
        if unit == 3:
            return "5h" if number in {None, 5} else f"{number}h"
        if unit == 6:
            return "Weekly" if number in {None, 1, 7} else f"{number}w"
        return "Credits"
    if kind == "TIME_LIMIT":
        return "MCP"
    return _title_case_slug(kind) or "Limit"


def _glm_usage_window(item: Any) -> Optional[dict]:
    if not isinstance(item, dict):
        return None
    kind = str(item.get("type") or "").strip().upper()
    if kind not in {"CREDIT_LIMIT", "TOKENS_LIMIT", "TIME_LIMIT"}:
        return None
    pct = item.get("percentage")
    if not isinstance(pct, (int, float)) or not math.isfinite(pct):
        current = item.get("currentValue")
        limit = item.get("usage")
        if isinstance(current, (int, float)) and isinstance(limit, (int, float)) and float(limit) > 0:
            pct = max(0.0, min(100.0, float(current) / float(limit) * 100.0))
        else:
            return None
    used_percent = max(0.0, min(100.0, float(pct)))
    reset_at = _parse_dt(item.get("nextResetTime"))
    remaining = item.get("remaining")
    limit = item.get("usage")
    detail = None
    if isinstance(remaining, (int, float)) and isinstance(limit, (int, float)) and float(limit) > 0:
        if float(limit) >= 1000:
            detail = f"{float(remaining):,.0f} of {float(limit):,.0f} left"
        else:
            detail = f"{float(remaining):.0f} of {float(limit):.0f} left"
    return _win(_glm_window_label(item), used_percent, reset_at, detail)


def _clock_label(stamp: datetime) -> str:
    return stamp.strftime("%I:%M %p").lstrip("0")


def _glm_peak_status(now: Optional[datetime] = None) -> tuple[bool, str]:
    """Peak / off-peak from the machine clock, in Singapore time (UTC+8).

    Z.AI docs: Monday to Friday, 14:00-18:00 Singapore Standard Time (UTC+8).
    Uses the system clock on Mac and Windows via datetime.now(timezone.utc).
    """
    stamp = now or datetime.now(timezone.utc)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    else:
        stamp = stamp.astimezone(timezone.utc)
    sg = stamp.astimezone(GLM_PEAK_TZ)
    local = stamp.astimezone()
    minute_of_day = sg.hour * 60 + sg.minute
    peak = (
        sg.weekday() in GLM_PEAK_WEEKDAYS
        and GLM_PEAK_START_HOUR * 60 <= minute_of_day < GLM_PEAK_END_HOUR * 60
    )
    windows = "Mon-Fri 14:00-18:00 UTC+8"
    local_clock = _clock_label(local)
    sg_clock = _clock_label(sg)
    if peak:
        return True, f"Peak pricing now · {local_clock} local · {sg_clock} UTC+8 · {windows}"
    return False, f"Off-peak now · {local_clock} local · {sg_clock} UTC+8 · peak is {windows}"


def _glm_snapshot_from_payload(payload: dict) -> Optional[dict]:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    if not isinstance(data, dict):
        return None
    limits = data.get("limits")
    windows: list[dict] = []
    if isinstance(limits, list):
        for item in limits:
            row = _glm_usage_window(item)
            if row:
                windows.append(row)
    if not windows:
        return None
    _peak, peak_text = _glm_peak_status()
    windows.append(_win("Pricing", None, None, peak_text))
    plan = _title_case_slug(data.get("level"))
    return _snapshot("glm", plan, windows)


def _fetch_glm_quota(token: str, base_url: str) -> Optional[dict]:
    import httpx

    url = f"{_zai_monitor_base(base_url)}/api/monitor/usage/quota/limit"
    headers = {
        "Authorization": token,
        "Accept": "application/json",
        "Accept-Language": "en-US,en",
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }
    with httpx.Client(timeout=15.0) as client:
        response = client.get(url, headers=headers)
        if response.status_code in {401, 403}:
            headers["Authorization"] = f"Bearer {token}"
            response = client.get(url, headers=headers)
        response.raise_for_status()
        payload = response.json() or {}
    if not isinstance(payload, dict):
        return None
    return _glm_snapshot_from_payload(payload)


def _glm_hermes_api_key() -> Optional[str]:
    for name in ("ZAI_API_KEY", "GLM_API_KEY", "Z_AI_API_KEY"):
        value = _hermes_env_value(name)
        if value:
            return value
    return None


def _fetch_glm_zcode_usage() -> Optional[dict]:
    creds = _zcode_coding_credentials()
    if not creds:
        return None
    token, base_url = creds
    return _fetch_glm_quota(token, base_url)


def _fetch_glm_hermes_api_key_usage() -> Optional[dict]:
    token = _glm_hermes_api_key()
    if not token:
        return None
    return _fetch_glm_quota(token, "https://api.z.ai")


def _fetch_glm_zcode_account_usage() -> Optional[dict]:
    # ZCode login first. Hermes Z.AI / GLM Coding Plan key is fallback.
    try:
        snap = _fetch_glm_zcode_usage()
        if snap:
            return snap
    except Exception:
        pass
    return _fetch_glm_hermes_api_key_usage()


def _hermes_homes() -> list[Path]:
    roots: list[Path] = []
    override = (os.environ.get("HERMES_HOME") or "").strip()
    if override:
        roots.append(Path(override).expanduser())
    roots.append(_user_home() / ".hermes")
    if _is_windows():
        local = os.environ.get("LOCALAPPDATA") or ""
        if local:
            roots.append(Path(local) / "hermes")
    unique: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = str(root)
        if key in seen:
            continue
        seen.add(key)
        unique.append(root)
    return unique


def _read_env_file_value(path: Path, name: str) -> Optional[str]:
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        try:
            text = path.read_text(encoding="latin-1")
        except OSError:
            return None
    prefix = f"{name}="
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if not line.startswith(prefix):
            continue
        value = line[len(prefix) :].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        return value.strip() or None
    return None


def _hermes_env_value(name: str) -> Optional[str]:
    direct = (os.environ.get(name) or "").strip()
    if direct:
        return direct
    for home in _hermes_homes():
        value = _read_env_file_value(home / ".env", name)
        if value:
            return value
    return None


def _deepseek_api_key() -> Optional[str]:
    return _hermes_env_value("DEEPSEEK_API_KEY")


def _deepseek_money(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(value):
        return float(value)
    if isinstance(value, str) and value.strip():
        try:
            return float(value.strip().replace(",", ""))
        except ValueError:
            return None
    return None


def _deepseek_money_text(amount: float, currency: str) -> str:
    code = (currency or "USD").strip().upper() or "USD"
    if code == "USD":
        return f"${amount:,.2f}"
    if code == "CNY":
        return f"¥{amount:,.2f}"
    return f"{amount:,.2f} {code}"


def _deepseek_peak_status(now: Optional[datetime] = None) -> tuple[bool, str]:
    """Peak / off-peak from the machine clock, converted to UTC.

    Uses datetime.now(timezone.utc) so Mac and Windows both follow the
    system clock. DeepSeek publishes peak windows in UTC.
    """
    stamp = now or datetime.now(timezone.utc)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    else:
        stamp = stamp.astimezone(timezone.utc)
    minute_of_day = stamp.hour * 60 + stamp.minute
    peak = False
    for start_hour, end_hour in DEEPSEEK_PEAK_WINDOWS_UTC:
        if start_hour * 60 <= minute_of_day < end_hour * 60:
            peak = True
            break
    local = stamp.astimezone()
    local_clock = _clock_label(local)
    utc_clock = stamp.strftime("%H:%M")
    windows = "01:00-04:00 and 06:00-10:00 UTC"
    if peak:
        return True, f"Peak pricing now · {local_clock} local · {utc_clock} UTC · {windows}"
    return False, f"Off-peak now · {local_clock} local · {utc_clock} UTC · peak is {windows}"


def _fetch_deepseek_account_usage() -> Optional[dict]:
    token = _deepseek_api_key()
    if not token:
        return None
    import httpx

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    with httpx.Client(timeout=15.0) as client:
        response = client.get(DEEPSEEK_BALANCE_URL, headers=headers)
        response.raise_for_status()
        payload = response.json() or {}
    if not isinstance(payload, dict):
        return None
    infos = payload.get("balance_infos")
    if not isinstance(infos, list) or not infos:
        return None
    chosen = None
    for item in infos:
        if not isinstance(item, dict):
            continue
        total = _deepseek_money(item.get("total_balance"))
        if total is None:
            continue
        currency = str(item.get("currency") or "USD").strip().upper() or "USD"
        if chosen is None or currency == "USD":
            chosen = (currency, total, item)
            if currency == "USD":
                break
    if not chosen:
        return None
    currency, total, item = chosen
    granted = _deepseek_money(item.get("granted_balance"))
    topped = _deepseek_money(item.get("topped_up_balance"))
    money = _deepseek_money_text(total, currency)
    detail_parts = [f"{money} left"]
    if topped is not None and topped > 0:
        detail_parts.append(f"{_deepseek_money_text(topped, currency)} topped up")
    if granted is not None and granted > 0:
        detail_parts.append(f"{_deepseek_money_text(granted, currency)} granted")
    available = payload.get("is_available")
    if available is False:
        detail_parts.append("balance too low for new calls")
    peak, peak_text = _deepseek_peak_status()
    windows = [
        _win("Balance", None, None, " · ".join(detail_parts)),
        _win("Pricing", None, None, peak_text),
    ]
    return _snapshot("deepseek", None, windows)


def _opencode_go_api_key() -> Optional[str]:
    return _hermes_env_value("OPENCODE_GO_API_KEY")


def _opencode_go_base_url() -> str:
    override = (_hermes_env_value("OPENCODE_GO_BASE_URL") or "").strip().rstrip("/")
    if not override:
        return OPENCODE_GO_DEFAULT_BASE_URL
    # Hermes may store either .../zen/go or .../zen/go/v1.
    if override.endswith("/v1"):
        return override
    if override.rstrip("/").endswith("/go"):
        return override.rstrip("/") + "/v1"
    return override


def _opencode_go_usage_url() -> str:
    return f"{_opencode_go_base_url().rstrip('/')}/usage"


def _fetch_opencode_go_account_usage() -> Optional[dict]:
    """OpenCode Go plan windows from GET /zen/go/v1/usage (Hermes API key)."""
    token = _opencode_go_api_key()
    if not token:
        return None
    import httpx

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    with httpx.Client(timeout=15.0) as client:
        response = client.get(_opencode_go_usage_url(), headers=headers)
        response.raise_for_status()
        payload = response.json() or {}
    if not isinstance(payload, dict):
        return None
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else payload
    if not isinstance(usage, dict):
        return None
    windows: list[dict] = []
    mapping = (
        ("rolling", "5h"),
        ("weekly", "Weekly"),
        ("monthly", "Monthly"),
    )
    for key, label in mapping:
        item = usage.get(key)
        if not isinstance(item, dict):
            continue
        pct = item.get("percent")
        if not isinstance(pct, (int, float)) or not math.isfinite(pct):
            continue
        used = max(0.0, min(100.0, float(pct)))
        status = str(item.get("status") or "").strip()
        detail = None if not status or status.lower() == "ok" else status
        windows.append(_win(label, used, _parse_dt(item.get("resetsAt")), detail))
    if not windows:
        return None
    return _snapshot("opencode-go", "Go", windows)


def _ollama_api_key() -> Optional[str]:
    return _hermes_env_value("OLLAMA_API_KEY")


def _ollama_used_percent(value: Any) -> Optional[float]:
    """Ollama Cloud limits.usage is a 0-1 fraction; accept 0-100 too."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(value):
        n = float(value)
        if 0.0 <= n <= 1.0:
            return max(0.0, min(100.0, n * 100.0))
        if 0.0 <= n <= 100.0:
            return max(0.0, min(100.0, n))
    return None


def _ollama_plan_name(payload: Optional[dict]) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    for key in ("Plan", "plan"):
        plan = payload.get(key)
        if isinstance(plan, str) and plan.strip():
            return _title_case_slug(plan.strip()) or plan.strip()
    return None


def _fetch_ollama_cloud_account_usage() -> Optional[dict]:
    """Ollama Cloud session/weekly from GET /api/usage (Hermes OLLAMA_API_KEY).

    Undocumented private endpoint the web settings page uses. Best-effort;
    no reset timestamps in the payload (session ~5h, weekly ~7d on pricing).
    """
    token = _ollama_api_key()
    if not token:
        return None
    import httpx

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    plan = None
    with httpx.Client(timeout=15.0) as client:
        me = client.post(
            OLLAMA_CLOUD_ME_URL,
            headers={**headers, "Content-Type": "application/json"},
            json={},
        )
        if me.status_code == 200:
            try:
                body = me.json()
            except Exception:
                body = None
            plan = _ollama_plan_name(body if isinstance(body, dict) else None)
        response = client.get(OLLAMA_CLOUD_USAGE_URL, headers=headers)
        response.raise_for_status()
        payload = response.json() or {}
    if not isinstance(payload, dict):
        return None
    limits = payload.get("limits") if isinstance(payload.get("limits"), dict) else {}
    windows: list[dict] = []
    for key, label, hint in (
        ("session", "5h", "resets about every 5h"),
        ("weekly", "Weekly", "resets about every 7 days"),
    ):
        item = limits.get(key) if isinstance(limits, dict) else None
        if not isinstance(item, dict):
            continue
        used = _ollama_used_percent(item.get("usage"))
        if used is None:
            continue
        windows.append(_win(label, used, None, hint))
    activity = payload.get("activity") if isinstance(payload.get("activity"), dict) else {}
    cost = activity.get("cost") if isinstance(activity, dict) else None
    if isinstance(cost, str) and cost.strip():
        windows.append(_win("Activity", None, None, f"${cost.strip()} last 4 weeks"))
    elif isinstance(cost, (int, float)) and math.isfinite(cost):
        windows.append(_win("Activity", None, None, f"${float(cost):.5f} last 4 weeks"))
    if not windows:
        return None
    return _snapshot("ollama", plan, windows)


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
    for fetch in (
        _fetch_claude_cli_account_usage,
        _fetch_codex_cli_account_usage,
        _fetch_cursor_account_usage,
        _fetch_kimi_account_usage,
        _fetch_grok_account_usage,
        _fetch_glm_zcode_account_usage,
        _fetch_deepseek_account_usage,
        _fetch_opencode_go_account_usage,
        _fetch_ollama_cloud_account_usage,
    ):
        try:
            snap = fetch()
        except Exception:
            continue
        if snap and (snap.get("windows") or snap.get("details")):
            snapshots.append(snap)
    return snapshots


def main() -> int:
    cli_only = "--cli-only" in sys.argv
    fresh = "--fresh" in sys.argv
    if not fresh:
        cached = _read_probe_result_cache(cli_only=cli_only)
        if cached is not None:
            json.dump(cached, sys.stdout, ensure_ascii=True)
            return 0
    snapshots = []
    have = set()
    if not cli_only:
        for snap in _collect_hermes():
            key = _provider_key(snap.get("provider"))
            if not key or key in have:
                continue
            snapshots.append(snap)
            have.add(key)
    for snap in _collect_cli():
        key = _provider_key(snap.get("provider"))
        if not key or key in have:
            continue
        snapshots.append(snap)
        have.add(key)
    _store_probe_result_cache(snapshots, cli_only=cli_only)
    json.dump(snapshots, sys.stdout, ensure_ascii=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
