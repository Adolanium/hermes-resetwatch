"""Read-only catalog usage probe for Resetwatch.

Reads existing CLI and Hermes access tokens and calls vendor usage APIs. Never
exchanges refresh tokens or writes login files. Expired Kimi/Grok credentials
produce an error asking the user to sign in with the vendor CLI. Cursor CLI
commands and Hermes OAuth resolvers are not invoked. Result/rate-limit caches
under $HERMES_HOME/cache/resetwatch may be written. No tokens on stdout.

Private vendor APIs are best-effort and may change without notice.
"""

from __future__ import annotations

import base64
import concurrent.futures
import contextlib
import hashlib
import io
import json
import math
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode


# Cap how often probe hits vendor APIs (success or empty). Claude 429 may
# keep a longer Retry-After on top of this.
PROBE_MIN_INTERVAL_SECONDS = 5 * 60
# Floor for --fresh. The Refresh button skips the 5-minute cache, but it
# must not turn into a vendor API hammer when clicked repeatedly.
FRESH_MIN_INTERVAL_SECONDS = 60
# Hard ceiling for collecting vendor fetchers (parallel). Hung sockets still
# need per-request timeouts below; this only bounds how long we wait for results.
PROBE_TOTAL_BUDGET_SECONDS = 45
HTTP_TIMEOUT = 8.0
CURSOR_CLI_TIMEOUT = 8
# Anthropic usage cache: short when falling back without a live token,
# longer while a 429 Retry-After marker is active.
ANTHROPIC_CACHE_MAX_AGE_SECONDS = 24 * 60 * 60
ANTHROPIC_CACHE_FALLBACK_AGE_SECONDS = 15 * 60
MINIMAX_HTTP_TIMEOUT = 5.0

# Set only for --profile. Calls without it keep the existing lookup rules.
_profile_home: Optional[Path] = None
_profile_inherits_env = True
LIVE_PROVIDERS = (
    "nous", "anthropic", "openai-codex", "openrouter", "cursor", "kimi", "grok",
    "glm", "deepseek", "opencode-go", "ollama", "minimax", "novita", "deepinfra",
    "ai-gateway", "commandcode",
)
_disabled_providers: set[str] = set()


# Codex is read directly below; the Hermes resolver can refresh OAuth.
HERMES_PROVIDERS = ("openrouter",)
# Anthropic/Claude is owned by _fetch_claude_cli_account_usage so we can
# honor 429 Retry-After and reuse a local cache instead of hammering OAuth usage.
USER_AGENT = "resetwatch"

CLAUDE_OAUTH_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CLAUDE_OAUTH_PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"

CODEX_DEFAULT_BASE_URL = "https://chatgpt.com/backend-api/codex"
CODEX_TOKEN_SKEW_SECONDS = 120

DEEPSEEK_BALANCE_URL = "https://api.deepseek.com/user/balance"
# Official DeepSeek peak windows, Monday-Friday UTC. Off-peak is half price.
# https://api-docs.deepseek.com/quick_start/pricing/
DEEPSEEK_PEAK_WINDOWS_UTC = ((1, 4), (6, 10))

OPENCODE_GO_DEFAULT_BASE_URL = "https://opencode.ai/zen/go/v1"

OLLAMA_CLOUD_USAGE_URL = "https://ollama.com/api/usage"
OLLAMA_CLOUD_ME_URL = "https://ollama.com/api/me"

MINIMAX_TOKEN_PLAN_URLS = (
    "https://www.minimax.io/v1/token_plan/remains",
    "https://api.minimax.io/v1/token_plan/remains",
)
MINIMAX_CN_TOKEN_PLAN_URL = "https://api.minimaxi.com/v1/token_plan/remains"
NOVITA_BALANCE_URL = "https://api.novita.ai/openapi/v1/billing/balance/detail"
DEEPINFRA_CHECKLIST_URL = "https://api.deepinfra.com/payment/checklist"
AI_GATEWAY_CREDITS_URL = "https://ai-gateway.vercel.sh/v1/credits"
COMMANDCODE_DEFAULT_BASE_URL = "https://api.commandcode.ai"

# Official GLM Coding Plan peak window: Mon-Fri 14:00-18:00 Singapore (UTC+8).
# Off-peak credits cost 50%. Fixed +08:00 works the same on Mac and Windows.
GLM_PEAK_TZ = timezone(timedelta(hours=8))
GLM_PEAK_WEEKDAYS = frozenset({0, 1, 2, 3, 4})  # Monday-Friday
GLM_PEAK_START_HOUR = 14
GLM_PEAK_END_HOUR = 18

# Claude / Codex / Grok usage calls use those products' private APIs and
# client headers. Best-effort only; vendors can change or reject them.

CURSOR_PERIOD_USAGE_URL = "https://api2.cursor.sh/aiserver.v1.DashboardService/GetCurrentPeriodUsage"

KIMI_CODE_USAGE_URL = "https://api.kimi.com/coding/v1/usages"

GROK_BILLING_URL = "https://cli-chat-proxy.grok.com/v1/billing?format=credits"
GROK_SETTINGS_URL = "https://cli-chat-proxy.grok.com/v1/settings"


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
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        stamp = float(value)
        if not math.isfinite(stamp):
            return None
        # Heuristic: large values are epoch ms (or us after one divide).
        for _ in range(2):
            if stamp > 1e12:
                stamp /= 1000.0
            else:
                break
        try:
            return datetime.fromtimestamp(stamp, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
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
    if key in {"minimax-cn", "minimax_cn", "minimax-token-plan"}:
        return "minimax"
    if key in {"novita-ai", "novitaai"}:
        return "novita"
    if key in {"deep-infra"}:
        return "deepinfra"
    if key in {"ai-gateway", "vercel", "vercel-ai-gateway"}:
        return "ai-gateway"
    return key


def _is_claude_oauth_token(token: str) -> bool:
    if not token:
        return False
    if token.startswith("sk-ant-api"):
        return False
    if token.startswith("sk-ant-") or token.startswith("eyJ") or token.startswith("cc-"):
        return True
    return False


def _json_safe(value: Any) -> Any:
    """Replace non-finite floats so JSON never emits NaN/Infinity."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


def _resetwatch_cache_dir() -> Path:
    """Return a writable cache dir. Never raises; cache is best-effort only."""
    candidates: list[Path] = []
    # Prefer homes that already exist so we do not invent ~/.hermes on Windows
    # ahead of the real %LOCALAPPDATA%\\hermes install.
    existing: list[Path] = []
    missing: list[Path] = []
    for home in _hermes_homes():
        target = home / "cache" / "resetwatch"
        if home.is_dir():
            existing.append(target)
        else:
            missing.append(target)
    candidates.extend(existing)
    candidates.extend(missing)
    fallback = Path(tempfile.gettempdir()) / "hermes-resetwatch-cache"
    if _profile_home is not None:
        # An unwritable profile must not reuse the base account's cache.
        scope = hashlib.sha256(str(_profile_home).encode("utf-8")).hexdigest()[:16]
        fallback = fallback / scope
    else:
        candidates.append(Path.home() / ".hermes" / "cache" / "resetwatch")
    candidates.append(fallback)
    seen: set[str] = set()
    for path in candidates:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        try:
            path.mkdir(parents=True, exist_ok=True)
            return path
        except Exception:
            continue
    return fallback


def _anthropic_cache_path() -> Path:
    return _resetwatch_cache_dir() / "anthropic_usage.json"


def _anthropic_ratelimit_scope(token: str) -> str:
    """Per-token key so one throttled Claude account does not hide the rest."""
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()[:12]


def _anthropic_ratelimit_path(scope: str) -> Path:
    return _resetwatch_cache_dir() / f"anthropic_usage.{scope}.ratelimit"


def _read_json_file(path: Path) -> Optional[Any]:
    def _reject_nonfinite(_name: str) -> Any:
        raise ValueError("non-finite json")

    try:
        # Reject non-standard NaN/Infinity so a poisoned cache cannot crash stdout.
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=_reject_nonfinite,
        )
    except Exception:
        return None
    return payload


def _write_cache_json(path: Path, payload: Any) -> None:
    """Atomic best-effort cache write. Never used for vendor credential files."""
    tmp: Optional[Path] = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(
            json.dumps(_json_safe(payload), ensure_ascii=True, allow_nan=False),
            encoding="utf-8",
        )
        os.replace(tmp, path)
        tmp = None
    except Exception:
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
        return


def _probe_result_cache_path(*, cli_only: bool) -> Path:
    name = "probe_snapshots.cli.json" if cli_only else "probe_snapshots.full.json"
    return _resetwatch_cache_dir() / name


def _has_dependency_error(snapshots: list) -> bool:
    return any(
        isinstance(snap, dict) and snap.get("provider") == "resetwatch"
        and any("cannot import httpx" in str(detail) for detail in (snap.get("details") or []))
        for snap in snapshots
    )


def _read_probe_result_cache(*, cli_only: bool, max_age: float = PROBE_MIN_INTERVAL_SECONDS) -> Optional[list]:
    payload = _read_json_file(_probe_result_cache_path(cli_only=cli_only))
    if not isinstance(payload, dict):
        return None
    if payload.get("disabled_providers", []) != sorted(_disabled_providers):
        return None
    fetched_at = payload.get("fetched_at")
    snapshots = payload.get("snapshots")
    if not isinstance(fetched_at, (int, float)) or not isinstance(snapshots, list):
        return None
    # Older probes cached this interpreter failure. It says nothing about
    # the interpreter running now, even inside the --fresh rate-limit floor.
    if _has_dependency_error(snapshots):
        return None
    age = datetime.now(timezone.utc).timestamp() - float(fetched_at)
    if age < 0 or age > float(max_age):
        return None
    return snapshots


def _store_probe_result_cache(snapshots: list, *, cli_only: bool) -> None:
    if _has_dependency_error(snapshots):
        return
    _write_cache_json(
        _probe_result_cache_path(cli_only=cli_only),
        {
            "fetched_at": datetime.now(timezone.utc).timestamp(),
            "snapshots": snapshots,
            "disabled_providers": sorted(_disabled_providers),
        },
    )


def _anthropic_ratelimit_remaining(scope: str) -> float:
    payload = _read_json_file(_anthropic_ratelimit_path(scope))
    if not isinstance(payload, dict):
        return 0.0
    until = payload.get("until")
    if not isinstance(until, (int, float)):
        return 0.0
    return max(0.0, float(until) - datetime.now(timezone.utc).timestamp())


def _mark_anthropic_ratelimit(scope: str, retry_after: float) -> None:
    seconds = max(30.0, min(float(retry_after or 60.0), 6 * 60 * 60))
    _write_cache_json(
        _anthropic_ratelimit_path(scope),
        {"until": datetime.now(timezone.utc).timestamp() + seconds, "retry_after": seconds},
    )


def _clear_anthropic_ratelimit(scope: str) -> None:
    try:
        _anthropic_ratelimit_path(scope).unlink(missing_ok=True)
    except Exception:
        return


def _cached_anthropic_snapshot(*, scope: Optional[str] = None, max_age: Optional[float] = None) -> Optional[dict]:
    payload = _read_json_file(_anthropic_cache_path())
    if not isinstance(payload, dict) or _provider_key(payload.get("provider")) != "anthropic":
        return None
    if not (payload.get("windows") or payload.get("details")):
        return None
    fetched_at = payload.get("fetched_at")
    if max_age is None:
        if scope and _anthropic_ratelimit_remaining(scope) > 0:
            max_age = float(ANTHROPIC_CACHE_MAX_AGE_SECONDS)
        else:
            max_age = float(ANTHROPIC_CACHE_FALLBACK_AGE_SECONDS)
    if isinstance(fetched_at, (int, float)) and math.isfinite(fetched_at):
        age = datetime.now(timezone.utc).timestamp() - float(fetched_at)
        if age < 0 or age > float(max_age):
            return None
    elif max_age < float(ANTHROPIC_CACHE_MAX_AGE_SECONDS):
        # Old cache files without fetched_at: only reuse under rate-limit.
        return None
    out = {
        "provider": payload.get("provider"),
        "plan": payload.get("plan"),
        "details": list(payload.get("details") or []),
        "windows": list(payload.get("windows") or []),
    }
    if isinstance(fetched_at, (int, float)) and math.isfinite(fetched_at):
        try:
            stamp = datetime.fromtimestamp(float(fetched_at), tz=timezone.utc).astimezone()
            as_of = stamp.strftime("%b %d, %I:%M %p").lstrip("0")
            details = list(out["details"])
            note = f"Cached as of {as_of}"
            if note not in details:
                details.append(note)
            out["details"] = details
        except (OverflowError, OSError, ValueError):
            pass
    return out


def _store_anthropic_snapshot(snap: dict) -> None:
    if not isinstance(snap, dict):
        return
    payload = {
        "provider": snap.get("provider"),
        "plan": snap.get("plan"),
        "details": list(snap.get("details") or []),
        "windows": list(snap.get("windows") or []),
        "fetched_at": datetime.now(timezone.utc).timestamp(),
    }
    _write_cache_json(_anthropic_cache_path(), payload)


def _hermes_anthropic_oauth_token() -> Optional[str]:
    """Read the saved token; Hermes token resolvers may refresh it."""
    for home in _hermes_homes():
        path = home / ".anthropic_oauth.json"
        payload = _read_json_file(path)
        if not isinstance(payload, dict):
            continue
        token = str(payload.get("accessToken") or payload.get("access_token") or "").strip()
        if token and _is_claude_oauth_token(token):
            return token
    return None


def _jwt_claims(token: str) -> dict:
    try:
        parts = str(token or "").split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1] + ("=" * (-len(parts[1]) % 4))
        data = json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _jwt_exp(token: str) -> Optional[float]:
    exp = _jwt_claims(token).get("exp")
    if isinstance(exp, (int, float)):
        return float(exp)
    return None


def _snapshot(
    provider: str,
    plan: Optional[str],
    windows: list[dict],
    details: Optional[list[str]] = None,
    account_label: Optional[str] = None,
    account_key: Optional[str] = None,
) -> dict:
    payload = {
        "provider": provider,
        "plan": plan,
        "details": list(details or ()),
        "windows": windows,
    }
    label = str(account_label or "").strip()
    if label:
        payload["account_label"] = label
    key = str(account_key or "").strip()
    if key:
        payload["account_key"] = key
    return payload


def _error_text(exc: BaseException) -> str:
    """Short, token-free reason for a failed vendor fetch."""
    try:
        import httpx
    except Exception:
        httpx = None  # type: ignore[assignment]
    if httpx is not None:
        if isinstance(exc, httpx.HTTPStatusError):
            code = exc.response.status_code
            reason = str(exc.response.reason_phrase or "").strip()
            return f"HTTP {code} {reason}".strip()
        if isinstance(exc, httpx.TimeoutException):
            return "request timed out"
        if isinstance(exc, httpx.TransportError):
            return "network error"
    name = type(exc).__name__
    text = str(exc).strip()
    text = text.splitlines()[0].strip() if text else ""
    if len(text) > 120:
        text = text[:117] + "..."
    return f"{name}: {text}" if text else name


def _error_snapshot(
    provider: str,
    message: str,
    *,
    account_label: Optional[str] = None,
    account_key: Optional[str] = None,
) -> dict:
    """A card with no windows that says why this vendor has no numbers.

    Only produced when a login exists but the fetch failed. A vendor with no
    login on this machine stays silent (fetcher returns None).
    """
    snap = _snapshot(
        provider,
        None,
        [],
        [f"Could not fetch usage: {message}"],
        account_label=account_label,
        account_key=account_key,
    )
    snap["error"] = str(message or "").strip() or "unknown error"
    return snap


def _hermes_window(window) -> dict:
    used = getattr(window, "used_percent", None)
    if isinstance(used, bool) or not isinstance(used, (int, float)) or not math.isfinite(used):
        used = None
    else:
        used = float(used)
    remaining = None if used is None else max(0.0, min(100.0, 100.0 - used))
    reset = getattr(window, "reset_at", None)
    reset_iso = None
    if reset is not None:
        if hasattr(reset, "isoformat"):
            try:
                reset_iso = reset.isoformat()
            except Exception:
                reset_iso = None
        else:
            parsed = _parse_dt(reset)
            reset_iso = parsed.isoformat() if parsed is not None else None
    label = getattr(window, "label", None)
    label_text = str(label).strip() if label is not None else ""
    return {
        "label": label_text or "Limit",
        "used_percent": used,
        "remaining_percent": remaining,
        "reset_at": reset_iso,
        "detail": getattr(window, "detail", None),
    }


def _version_key(name: str) -> tuple:
    parts: list[int] = []
    for part in str(name or "").split("."):
        digits = ""
        for ch in part:
            if ch.isdigit():
                digits += ch
            else:
                break
        parts.append(int(digits) if digits else -1)
    return tuple(parts)


def _cursor_cli_json(args: list[str]) -> Optional[dict]:
    """Do not launch a CLI that may refresh its login as a side effect."""
    return None


def _cursor_plan_cache_path() -> Path:
    return _resetwatch_cache_dir() / "cursor_plan.json"


def _cursor_cached_plan_name(*, max_age: float = 7 * 24 * 60 * 60) -> Optional[str]:
    payload = _read_json_file(_cursor_plan_cache_path())
    if not isinstance(payload, dict):
        return None
    plan = payload.get("plan")
    fetched_at = payload.get("fetched_at")
    if not isinstance(plan, str) or not plan.strip():
        return None
    if not isinstance(fetched_at, (int, float)):
        return None
    age = time.time() - float(fetched_at)
    if age < 0 or age > float(max_age):
        return None
    return plan.strip()


def _store_cursor_plan_name(plan: str) -> None:
    text = str(plan or "").strip()
    if not text:
        return
    _write_cache_json(
        _cursor_plan_cache_path(),
        {"plan": text, "fetched_at": time.time()},
    )


def _cursor_cli_plan_name() -> Optional[str]:
    cached = _cursor_cached_plan_name()
    if cached:
        return cached
    about = _cursor_cli_json(["about", "--format", "json"])
    if not about:
        return None
    plan = about.get("subscriptionTier")
    text = str(plan or "").strip()
    if text:
        _store_cursor_plan_name(text)
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
            stdin=subprocess.DEVNULL,
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


def _finite_number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


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
    with httpx.Client(timeout=HTTP_TIMEOUT) as client:
        response = client.post(CURSOR_PERIOD_USAGE_URL, headers=headers, json={})
        response.raise_for_status()
        payload = response.json() or {}
    if not isinstance(payload, dict):
        return None
    plan_usage = payload.get("planUsage") if isinstance(payload.get("planUsage"), dict) else {}
    reset_at = _parse_dt(payload.get("billingCycleEnd"))
    windows: list[dict] = []
    limit = _finite_number(plan_usage.get("limit"))
    remaining = _finite_number(plan_usage.get("remaining"))
    if limit is not None and limit > 0 and remaining is not None:
        used_percent = max(0.0, min(100.0, (1.0 - remaining / limit) * 100.0))
        windows.append(
            _win(
                "Included spend",
                used_percent,
                reset_at,
                f"{_fmt_cursor_amount(remaining)} of {_fmt_cursor_amount(limit)} left",
            )
        )
    for key, label in (("autoPercentUsed", "Auto"), ("apiPercentUsed", "API models")):
        pct = _finite_number(plan_usage.get(key))
        if pct is not None:
            windows.append(_win(label, max(0.0, min(100.0, pct)), reset_at))
    spend = payload.get("spendLimitUsage") if isinstance(payload.get("spendLimitUsage"), dict) else {}
    spend_limit = _finite_number(spend.get("limit"))
    spend_used = _finite_number(spend.get("used"))
    if spend_limit is not None and spend_limit > 0 and spend_used is not None:
        used_percent = max(0.0, min(100.0, spend_used / spend_limit * 100.0))
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


def _kimi_code_access_token(*, previous: Optional[str] = None) -> Optional[str]:
    """Read an existing access token without exchanging or saving credentials."""
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
    return None


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
    return _win(
        label,
        used_percent,
        _parse_dt(detail.get("resetTime") or detail.get("resetAt")),
        None,
    )


def _kimi_extra_usage_window(payload: dict) -> Optional[dict]:
    wallet = payload.get("boosterWallet") if isinstance(payload.get("boosterWallet"), dict) else {}
    balance = wallet.get("balance") if isinstance(wallet.get("balance"), dict) else {}
    if str(balance.get("type") or "").upper() not in {"BOOSTER", "BALANCE_BOOSTER"}:
        return None
    amount = _to_int(balance.get("amount"))
    amount_left = _to_int(balance.get("amountLeft"))
    if amount is None or amount <= 0 or amount_left is None:
        return None
    scale = 1_000_000
    total = amount / scale
    left = amount_left / scale
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
    with httpx.Client(timeout=HTTP_TIMEOUT) as client:
        response = client.get(KIMI_CODE_USAGE_URL, headers=headers)
        if response.status_code == 401:
            raise RuntimeError("Kimi login expired. Sign in with the Kimi CLI, then refresh usage. Catalog installs never refresh login tokens.")
        response.raise_for_status()
        payload = response.json() or {}
    if not isinstance(payload, dict):
        return None
    snap = _kimi_snapshot_from_payload(payload)
    return snap


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
    with httpx.Client(timeout=HTTP_TIMEOUT) as client:
        response = client.get(KIMI_CODE_USAGE_URL, headers=headers)
        response.raise_for_status()
        payload = response.json() or {}
    if not isinstance(payload, dict):
        return None
    return _kimi_snapshot_from_payload(payload)


def _fetch_kimi_account_usage() -> Optional[dict]:
    # CLI OAuth first (can refresh on 401). Hermes Coding Plan key is fallback.
    # If the CLI path failed and the fallback has nothing, surface the CLI error.
    cli_error: Optional[BaseException] = None
    try:
        snap = _fetch_kimi_cli_usage()
        if snap:
            return snap
    except Exception as exc:
        cli_error = exc
    snap = _fetch_kimi_coding_api_key_usage()
    if snap is None and cli_error is not None:
        raise cli_error
    return snap


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


def _grok_access_context(*, previous: Optional[str] = None) -> Optional[tuple[str, str]]:
    """Read an existing access token without exchanging or saving credentials."""
    loaded = _grok_read_auth()
    if not loaded:
        return None
    path, map_key, payload, entry = loaded
    token = entry.get("key") or entry.get("access_token")
    user_id = str(entry.get("user_id") or entry.get("principal_id") or "").strip()
    token = token.strip() if isinstance(token, str) and token.strip() else None
    # Token freshness and user_id are separate. A missing user_id cannot be
    # fixed by burning a rotating refresh token.
    if token and (not previous or token != previous.strip()):
        if user_id:
            return token, user_id
        return None
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
    with httpx.Client(timeout=HTTP_TIMEOUT) as client:
        response = client.get(billing_url, headers=headers)
        if response.status_code == 401:
            raise RuntimeError("Grok login expired. Sign in with the Grok CLI, then refresh usage. Catalog installs never refresh login tokens.")
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
    limit = _grok_cent(config.get("monthlyLimit"))
    used = _grok_cent(config.get("used"))
    if isinstance(used_pct, (int, float)) and math.isfinite(used_pct):
        windows.append(_win(_grok_period_label(period), max(0.0, min(100.0, float(used_pct))), reset_at))
    elif limit is not None and limit > 0 and used is not None:
        windows.append(
            _win(
                _grok_period_label(period),
                max(0.0, min(100.0, used / float(limit) * 100.0)),
                reset_at,
                f"${used / 100:.2f} of ${limit / 100:.2f} used",
            )
        )
    elif period is not None:
        # Grok leaves out creditUsagePercent until the period records usage.
        windows.append(
            _win(_grok_period_label(period), 0.0, reset_at, "No usage recorded yet this period")
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
        windows.append(_win("Prepaid", None, None, f"${prepaid / 100:.2f} left"))
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
    plan = settings.get("subscription_tier_display") or payload.get("subscriptionTier")
    if isinstance(plan, str):
        plan = plan.strip() or None
    else:
        plan = None
    if not windows:
        if not (plan or reset_at):
            return None
        windows.append(
            _win(_grok_period_label(period), None, reset_at, "No metered limits on this plan")
        )
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
    if not math.isfinite(expires_ms):
        return True
    # Claude Code usually stores ms; normalize seconds-era values.
    if expires_ms > 0 and expires_ms < 1e12:
        expires_ms *= 1000.0
    now_ms = _utc_now().timestamp() * 1000
    return now_ms < (expires_ms - 60_000)


def _expires_ms(creds: dict) -> float:
    raw = creds.get("expiresAt") or 0
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.0
    return value if math.isfinite(value) else 0.0


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
        return keychain if _expires_ms(keychain) >= _expires_ms(file_creds) else file_creds
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


def _anthropic_rate_limit_snapshot(
    remaining: float,
    *,
    account_label: Optional[str] = None,
    account_key: Optional[str] = None,
) -> dict:
    mins = max(1, int((remaining + 59) // 60))
    return _error_snapshot(
        "anthropic",
        f"usage API rate-limited, try again in ~{mins}m",
        account_label=account_label,
        account_key=account_key,
    )


_CLAUDE_LOGIN_LABELS = {
    "dashboard pkce",
    "hermes_pkce",
    "claude_code",
    "oauth",
    "anthropic",
    "claude",
    "device_code",
}


def _claude_name_from_profile(profile: Optional[dict]) -> str:
    if not isinstance(profile, dict):
        return ""
    account = profile.get("account") if isinstance(profile.get("account"), dict) else {}
    for blob in (account, profile):
        for key in ("email", "display_name", "name"):
            value = str(blob.get(key) or "").strip()
            if value:
                return value
    return ""


def _claude_card_label(entry: dict, profile: Optional[dict] = None) -> str:
    stored = str(entry.get("label") or "").strip()
    named = _claude_name_from_profile(profile)
    if stored and stored.lower() not in _CLAUDE_LOGIN_LABELS:
        return _mask_email_label(stored)
    if named:
        return _mask_email_label(named)
    if stored:
        return _mask_email_label(stored)
    return str(entry.get("id") or "").strip() or "Claude"


def _claude_account_key(profile: Optional[dict], entry: Optional[dict]) -> Optional[str]:
    # The same account can be reachable through two OAuth grants (Hermes pool
    # and the Claude Code CLI). Key on who the account is, not which token or
    # pool entry reached it, so the duplicates collapse into one card.
    identity = ""
    if isinstance(profile, dict):
        account = profile.get("account") if isinstance(profile.get("account"), dict) else {}
        for blob in (account, profile):
            for field in ("uuid", "email"):
                value = str(blob.get(field) or "").strip().lower()
                if value:
                    identity = value
                    break
            if identity:
                break
    if identity:
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:8]
    return str((entry or {}).get("id") or "").strip() or None


def _claude_pool_accounts() -> list[dict]:
    accounts: list[dict] = []
    seen_tokens: set[str] = set()
    for entry in _pool_entries("anthropic"):
        if str(entry.get("last_status") or "").strip().lower() == "dead":
            continue
        token = str(entry.get("access_token") or "").strip()
        if not token or token in seen_tokens or not _is_claude_oauth_token(token):
            continue
        seen_tokens.add(token)
        accounts.append(
            {
                "token": token,
                "label": _claude_card_label(entry),
                "entry": entry,
            }
        )
    return accounts


def _fetch_claude_usage(
    token: str,
    *,
    account_label: Optional[str] = None,
    entry: Optional[dict] = None,
    use_cache: bool = False,
) -> Optional[dict]:
    access = str(token or "").strip()
    if not access:
        return _cached_anthropic_snapshot() if use_cache else None
    scope = _anthropic_ratelimit_scope(access)
    fallback_label = account_label
    fallback_key = str((entry or {}).get("id") or "").strip() or None
    if entry is not None and not fallback_label:
        fallback_label = _claude_card_label(entry)
    remaining = _anthropic_ratelimit_remaining(scope)
    if remaining > 0:
        if use_cache:
            cached = _cached_anthropic_snapshot(scope=scope)
            if cached:
                return cached
        return _anthropic_rate_limit_snapshot(
            remaining, account_label=fallback_label, account_key=fallback_key
        )

    import httpx

    headers = {
        # Private Anthropic OAuth usage API, same shape Claude Code uses.
        # Best-effort; Anthropic may change or rate-limit this.
        "Authorization": f"Bearer {access}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": "claude-code/2.1.0",
    }
    profile: dict = {}
    try:
        with httpx.Client(timeout=HTTP_TIMEOUT) as client:
            response = client.get(CLAUDE_OAUTH_USAGE_URL, headers=headers)
            if response.status_code == 429:
                retry_after = response.headers.get("retry-after")
                try:
                    wait = float(retry_after) if retry_after else 3600.0
                except Exception:
                    wait = 3600.0
                _mark_anthropic_ratelimit(scope, wait)
                if use_cache:
                    cached = _cached_anthropic_snapshot(scope=scope)
                    if cached:
                        return cached
                return _anthropic_rate_limit_snapshot(
                    wait, account_label=fallback_label, account_key=fallback_key
                )
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
        # Serve the recent cache if we have one; otherwise let the caller
        # turn this into a visible error card instead of a missing row.
        if use_cache:
            cached = _cached_anthropic_snapshot(scope=scope)
            if cached:
                return cached
        raise

    if not isinstance(payload, dict):
        if use_cache:
            cached = _cached_anthropic_snapshot(scope=scope)
            if cached:
                return cached
        raise ValueError("usage API returned a non-object body")
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
        # Anthropic OAuth usage reports utilization as a 0-100 percentage
        # (not a 0-1 fraction). Treating it as a fraction made real usage
        # like 7 show as 100% used / 0% left.
        if not isinstance(util, (int, float)) or isinstance(util, bool) or not math.isfinite(util):
            continue
        used = max(0.0, min(100.0, float(util)))
        windows.append(_win(label, used, _parse_dt(window.get("resets_at"))))
    details: list[str] = []
    extra = payload.get("extra_usage") if isinstance(payload.get("extra_usage"), dict) else {}
    if extra.get("is_enabled"):
        used_credits = extra.get("used_credits")
        monthly_limit = extra.get("monthly_limit")
        currency = extra.get("currency") or "USD"
        if isinstance(used_credits, (int, float)) and isinstance(monthly_limit, (int, float)):
            details.append(f"Extra usage: {used_credits:.2f} / {monthly_limit:.2f} {currency}")
    if not windows and not details:
        return _cached_anthropic_snapshot(scope=scope) if use_cache else None
    _clear_anthropic_ratelimit(scope)
    label = account_label
    if entry is not None:
        label = _claude_card_label(entry, profile)
    elif not str(label or "").strip():
        named = _claude_name_from_profile(profile)
        label = _mask_email_label(named) if named else None
    snap = _snapshot(
        "anthropic",
        infer_claude_plan_name(profile, payload),
        windows,
        details,
        account_label=label,
        account_key=_claude_account_key(profile, entry),
    )
    if use_cache:
        _store_anthropic_snapshot(snap)
    return snap


def _fetch_claude_cli_account_usage() -> Optional[dict]:
    token = _claude_code_access_token() or _hermes_anthropic_oauth_token()
    if not token:
        return _cached_anthropic_snapshot(max_age=ANTHROPIC_CACHE_FALLBACK_AGE_SECONDS)
    # 429 handling and the per-token marker live inside _fetch_claude_usage.
    return _fetch_claude_usage(token, use_cache=True)


def _fetch_claude_accounts_usage() -> Optional[list]:
    """One worker: walk pooled Claude tokens in order.

    Each account has its own 429 marker, so a throttled account shows a
    rate-limit card while the others still fetch. A failed fetch becomes an
    error card for that account instead of a missing row.
    """
    accounts = _claude_pool_accounts()
    if not accounts:
        snap = _fetch_claude_cli_account_usage()
        return [snap] if snap else None
    out: list[dict] = []
    pool_tokens = {str(account.get("token") or "") for account in accounts}
    for account in accounts:
        entry = account.get("entry") or {}
        try:
            snap = _fetch_claude_usage(
                account.get("token") or "",
                account_label=account.get("label"),
                entry=entry,
                use_cache=False,
            )
        except Exception as exc:
            snap = _error_snapshot(
                "anthropic",
                _error_text(exc),
                account_label=account.get("label"),
                account_key=str(entry.get("id") or "").strip() or None,
            )
        if snap:
            out.append(snap)
    cli_token = _claude_code_access_token() or _hermes_anthropic_oauth_token()
    if cli_token and cli_token not in pool_tokens:
        try:
            extra = _fetch_claude_usage(cli_token, use_cache=False)
        except Exception as exc:
            extra = _error_snapshot("anthropic", _error_text(exc))
        if extra:
            out.append(extra)
    if out:
        return out
    snap = _fetch_claude_cli_account_usage()
    return [snap] if snap else None


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


_CODEX_LOGIN_LABELS = {"device_code", "oauth", "openai-codex", "codex", "chatgpt"}


def _codex_name_from_token(token: str) -> str:
    claims = _jwt_claims(token)
    profile = claims.get("https://api.openai.com/profile")
    if isinstance(profile, dict):
        for key in ("email", "name"):
            value = str(profile.get(key) or "").strip()
            if value:
                return value
    for key in ("email", "preferred_username", "upn"):
        value = str(claims.get(key) or "").strip()
        if value:
            return value
    return ""


def _codex_account_id_from_token(token: str) -> Optional[str]:
    claims = _jwt_claims(token)
    auth = claims.get("https://api.openai.com/auth")
    if isinstance(auth, dict):
        account_id = str(auth.get("chatgpt_account_id") or "").strip()
        if account_id:
            return account_id
    account_id = str(claims.get("chatgpt_account_id") or "").strip()
    return account_id or None


def _mask_email_label(label: str) -> str:
    text = str(label or "").strip()
    at = text.find("@")
    if at <= 0 or at == len(text) - 1 or " " in text:
        return text
    keep = text[: min(2, at)]
    return f"{keep}**{text[at:]}"


def _codex_card_label(entry: dict, token: str) -> str:
    stored = str(entry.get("label") or "").strip()
    named = _codex_name_from_token(token)
    if stored and stored.lower() not in _CODEX_LOGIN_LABELS:
        return _mask_email_label(stored)
    if named:
        return _mask_email_label(named)
    if stored:
        return _mask_email_label(stored)
    return str(entry.get("id") or "").strip() or "Codex"


_CODEX_WEEKLY_SECONDS = 7 * 24 * 3600


def _codex_window_label(window: dict, fallback: str) -> str:
    # Some plans put the weekly limit in primary_window, so position alone
    # cannot tell which window this is.
    seconds = window.get("limit_window_seconds")
    if not isinstance(seconds, (int, float)) or isinstance(seconds, bool) or seconds <= 0:
        return fallback
    return "Weekly" if seconds >= _CODEX_WEEKLY_SECONDS else "Session"


def _codex_extra_limit_prefix(item: dict) -> str:
    # Spark is the extra bucket people care about. Other extras keep their own name.
    name = str(item.get("limit_name") or "").strip()
    feature = str(item.get("metered_feature") or "").strip()
    combined = f"{name} {feature}".lower()
    if "spark" in combined or feature.lower() == "codex_bengalfox":
        return "Spark"
    if name:
        return name
    titled = _title_case_slug(feature)
    return titled or "Extra"


def _codex_windows_from_rate_limit(rate_limit: Any, *, prefix: str = "") -> list[dict]:
    if not isinstance(rate_limit, dict):
        return []
    windows: list[dict] = []
    for key, fallback in (("primary_window", "Session"), ("secondary_window", "Weekly")):
        window = rate_limit.get(key) if isinstance(rate_limit.get(key), dict) else {}
        used = window.get("used_percent")
        if not isinstance(used, (int, float)) or isinstance(used, bool) or not math.isfinite(used):
            continue
        label = _codex_window_label(window, fallback)
        if prefix:
            label = f"{prefix} {label}"
        windows.append(
            _win(
                label,
                max(0.0, min(100.0, float(used))),
                _parse_dt(window.get("reset_at")),
            )
        )
    return windows


def _codex_windows_from_payload(payload: dict) -> list[dict]:
    # Main Codex limits first. Then extra limits like Spark, if the account has them.
    rate_limit = payload.get("rate_limit") if isinstance(payload.get("rate_limit"), dict) else {}
    windows = _codex_windows_from_rate_limit(rate_limit)
    extra = payload.get("additional_rate_limits")
    if not isinstance(extra, list):
        return windows
    for item in extra:
        if not isinstance(item, dict):
            continue
        nested = item.get("rate_limit") if isinstance(item.get("rate_limit"), dict) else {}
        windows.extend(
            _codex_windows_from_rate_limit(nested, prefix=_codex_extra_limit_prefix(item))
        )
    return windows


def _fetch_codex_usage(
    token: str,
    account_id: Optional[str] = None,
    base_url: Optional[str] = None,
    account_label: Optional[str] = None,
    account_key: Optional[str] = None,
) -> Optional[dict]:
    access = str(token or "").strip()
    if not access:
        return None
    import httpx

    headers = {
        # Private Codex usage API. Best-effort; OpenAI may change or reject this.
        "Authorization": f"Bearer {access}",
        "Accept": "application/json",
        "User-Agent": "codex-cli",
    }
    chatgpt_account = str(account_id or "").strip()
    if chatgpt_account:
        headers["ChatGPT-Account-Id"] = chatgpt_account
    with httpx.Client(timeout=HTTP_TIMEOUT) as client:
        response = client.get(_codex_usage_url(base_url or CODEX_DEFAULT_BASE_URL), headers=headers)
        response.raise_for_status()
        payload = response.json() or {}
    if not isinstance(payload, dict):
        return None
    windows = _codex_windows_from_payload(payload)
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
    return _snapshot(
        "openai-codex",
        plan,
        windows,
        details,
        account_label=account_label,
        account_key=account_key,
    )


def _fetch_codex_cli_account_usage() -> Optional[dict]:
    context = _codex_cli_access_context()
    if not context:
        return None
    token, account_id = context
    return _fetch_codex_usage(token, account_id)


def _read_auth_store(path: Path) -> Optional[dict]:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _pool_entries(provider: str) -> list[dict]:
    """Read credential_pool rows from auth.json. Never writes the file."""
    name = str(provider or "").strip()
    if not name:
        return []
    for home in _hermes_homes():
        store = _read_auth_store(home / "auth.json")
        if not store:
            continue
        pool = store.get("credential_pool")
        if not isinstance(pool, dict):
            continue
        rows = pool.get(name)
        if isinstance(rows, list) and rows:
            return [row for row in rows if isinstance(row, dict)]
    return []


def _codex_pool_entries() -> list[dict]:
    return _pool_entries("openai-codex")


def _codex_pool_accounts() -> list[dict]:
    accounts: list[dict] = []
    seen_tokens: set[str] = set()
    for entry in _codex_pool_entries():
        if str(entry.get("last_status") or "").strip().lower() == "dead":
            continue
        token = str(entry.get("access_token") or "").strip()
        if not token or token in seen_tokens:
            continue
        if _codex_token_expiring(token):
            continue
        seen_tokens.add(token)
        accounts.append(
            {
                "token": token,
                "account_id": _codex_account_id_from_token(token),
                "base_url": str(entry.get("base_url") or "").strip() or None,
                "label": _codex_card_label(entry, token),
                "key": str(entry.get("id") or "").strip(),
            }
        )
    return accounts


def _codex_pool_fetcher(account: dict):
    def fetch() -> Optional[dict]:
        return _fetch_codex_usage(
            account.get("token") or "",
            account.get("account_id"),
            account.get("base_url"),
            account_label=account.get("label"),
            account_key=account.get("key"),
        )

    return fetch


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
    return _win(_glm_window_label(item), used_percent, reset_at, None)


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
    with httpx.Client(timeout=HTTP_TIMEOUT) as client:
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
    cli_error: Optional[BaseException] = None
    try:
        snap = _fetch_glm_zcode_usage()
        if snap:
            return snap
    except Exception as exc:
        cli_error = exc
    snap = _fetch_glm_hermes_api_key_usage()
    if snap is None and cli_error is not None:
        raise cli_error
    return snap


def _hermes_homes() -> list[Path]:
    if _profile_home is not None:
        return [_profile_home]
    roots: list[Path] = []
    override = (os.environ.get("HERMES_HOME") or "").strip()
    if override:
        roots.append(Path(override).expanduser())
    # Match plugin.js / README: Windows real home is %LOCALAPPDATA%\\hermes
    # before the legacy ~/.hermes path.
    if _is_windows():
        local = os.environ.get("LOCALAPPDATA") or ""
        if local:
            roots.append(Path(local) / "hermes")
    roots.append(_user_home() / ".hermes")
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
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() != name:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        else:
            # Strip unquoted inline comments: KEY=value # note
            cut = None
            for index, char in enumerate(value):
                if char == "#" and (index == 0 or value[index - 1].isspace()):
                    cut = index
                    break
            if cut is not None:
                value = value[:cut].rstrip()
        return value.strip() or None
    return None


def _hermes_env_value(name: str) -> Optional[str]:
    # A gateway serving a sibling profile still has its own API keys in env.
    direct = (os.environ.get(name) or "").strip() if _profile_inherits_env else ""
    if direct:
        return direct
    value = _secret_source_env_value(name)
    if value:
        return value
    for home in _hermes_homes():
        value = _read_env_file_value(home / ".env", name)
        if value:
            return value
    return _container_env_file_value(name)


def _secret_source_env_value(name: str) -> Optional[str]:
    """Read a matching Vaultwarden snapshot without invoking a secret source.

    Cache keys are backend-specific. Do not scan arbitrary *_cache.json files
    or guess precedence when several sources are enabled. Vaultwarden's key
    binds the snapshot to its session, item, and login-field mappings.
    Its TTL is a refetch interval, not a credential expiry: the gateway also
    retains its startup values past that interval. Never refresh/write here.
    """
    home = next((home for home in _hermes_homes() if home.is_dir()), None)
    if home is None:
        return None
    try:
        text = (home / "config.yaml").read_text(encoding="utf-8-sig")
        try:
            config = json.loads(text)
        except ValueError:
            try:
                import yaml
            except ImportError:
                return None  # The gateway runtime supplies PyYAML.
            try:
                config = yaml.safe_load(text)
            except yaml.YAMLError:
                return None
        payload = json.loads((home / "cache/vaultwarden_cache.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return None
    sources = config.get("secrets") if isinstance(config, dict) else None
    if not isinstance(sources, dict) or not isinstance(payload, dict):
        return None
    enabled = [key for key, cfg in sources.items() if isinstance(cfg, dict) and cfg.get("enabled")]
    if enabled != ["vaultwarden"]:
        return None
    cfg = sources["vaultwarden"]
    try:
        ttl = float(cfg.get("cache_ttl_seconds", 300))
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(ttl) or ttl <= 0:
        return None
    existing = _read_env_file_value(home / ".env", name)
    preserve = sources.get("preserve_existing")
    if existing and (not cfg.get("override_existing", False) or
                     (isinstance(preserve, list) and name in preserve)):
        return None
    session_name = str(cfg.get("session_env") or "BW_SESSION")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", session_name):
        return None
    session = (os.environ.get(session_name) or "").strip() if _profile_inherits_env else ""
    session = session or _read_env_file_value(home / ".env", session_name) or _container_env_file_value(session_name)
    item = str(cfg.get("item_name") or "").strip()
    if not session or not item or name == session_name:
        return None
    bindings = []
    for field in ("username_env", "password_env", "notes_env"):
        binding = str(cfg.get(field) or "").strip()
        bindings.append(binding if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", binding) else "")
    fingerprint = hashlib.sha256(session.encode("utf-8")).hexdigest()[:16]
    expected = "|".join(("vw", fingerprint, item, *bindings))
    fetched = payload.get("fetched_at")
    if (payload.get("key") != expected or isinstance(fetched, bool) or
            not isinstance(fetched, (int, float)) or
            not 0 < fetched <= time.time()):
        return None
    secrets = payload.get("secrets")
    value = secrets.get(name) if isinstance(secrets, dict) else None
    return value.strip() if isinstance(value, str) and value.strip() else None


# The official Hermes Docker image keeps container env vars as bare files
# under this directory (s6-overlay).
_S6_ENV_DIR = Path("/run/s6/container_environment")


def _container_env_file_value(name: str) -> Optional[str]:
    # Container startup values belong to the inherited gateway profile only.
    if not _profile_inherits_env or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        return None
    try:
        value = (_S6_ENV_DIR / name).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return None
    return value or None


def _deepseek_api_key() -> Optional[str]:
    return _hermes_env_value("DEEPSEEK_API_KEY")


def _deepseek_money(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(value):
        return float(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = float(value.strip().replace(",", ""))
            return parsed if math.isfinite(parsed) else None
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
        if stamp.weekday() < 5 and start_hour * 60 <= minute_of_day < end_hour * 60:
            peak = True
            break
    local = stamp.astimezone()
    local_clock = _clock_label(local)
    utc_clock = stamp.strftime("%H:%M")
    windows = "Mon-Fri 01:00-04:00 and 06:00-10:00 UTC"
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
    with httpx.Client(timeout=HTTP_TIMEOUT) as client:
        response = client.get(DEEPSEEK_BALANCE_URL, headers=headers)
        response.raise_for_status()
        payload = response.json() or {}
    if not isinstance(payload, dict):
        return None
    infos = payload.get("balance_infos")
    if not isinstance(infos, list) or not infos:
        return None
    balances = []
    for item in infos:
        if not isinstance(item, dict):
            continue
        total = _deepseek_money(item.get("total_balance"))
        if total is None:
            continue
        currency = str(item.get("currency") or "USD").strip().upper() or "USD"
        balances.append((currency, total, item))
    if not balances:
        return None
    # Keep currencies separate. Empty placeholder rows must not hide funds,
    # and amounts in different currencies cannot be ranked or added together.
    visible = [balance for balance in balances if balance[1] != 0]
    if not visible:
        visible = [next((balance for balance in balances if balance[0] == "USD"), balances[0])]
    available = payload.get("is_available")
    windows = []
    for currency, total, item in sorted(visible, key=lambda balance: balance[0]):
        granted = _deepseek_money(item.get("granted_balance"))
        topped = _deepseek_money(item.get("topped_up_balance"))
        detail_parts = [f"{_deepseek_money_text(total, currency)} left"]
        if topped is not None and topped > 0:
            detail_parts.append(f"{_deepseek_money_text(topped, currency)} topped up")
        if granted is not None and granted > 0:
            detail_parts.append(f"{_deepseek_money_text(granted, currency)} granted")
        if available is False:
            detail_parts.append("balance too low for new calls")
        windows.append(_win(f"Balance ({currency})", None, None, " · ".join(detail_parts)))
    _, peak_text = _deepseek_peak_status()
    windows.append(_win("Pricing", None, None, peak_text))
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
    with httpx.Client(timeout=HTTP_TIMEOUT) as client:
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
    """Ollama Cloud limits.usage is a 0-1 fraction. Do not also accept 0-100."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(value) and 0.0 <= float(value) <= 1.0:
        return max(0.0, min(100.0, float(value) * 100.0))
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
    with httpx.Client(timeout=HTTP_TIMEOUT) as client:
        try:
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
        except Exception:
            plan = None
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
        text = cost.strip()
        if text[:1].isdigit():
            text = f"${text}"
        windows.append(_win("Activity", None, None, f"{text} last 4 weeks"))
    elif isinstance(cost, (int, float)) and math.isfinite(cost):
        windows.append(_win("Activity", None, None, f"${float(cost):.5f} last 4 weeks"))
    if not windows:
        return None
    return _snapshot("ollama", plan, windows)


def _parse_epoch_ms(value: Any) -> Optional[datetime]:
    return _parse_dt(value)


def _minimax_percent(item: dict, *keys: str) -> Optional[float]:
    """MiniMax remaining/usage fields are 0-100 percentages, not 0-1 fractions."""
    for key in keys:
        value = item.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)) and math.isfinite(value) and 0.0 <= float(value) <= 100.0:
            return float(value)
    return None


def _minimax_remaining_percent(item: dict, *keys: str) -> Optional[float]:
    return _minimax_percent(item, *keys)


def _minimax_used_from_counts(item: dict, remaining_key: str, total_key: str) -> Optional[float]:
    remaining = _to_int(item.get(remaining_key))
    total = _to_int(item.get(total_key))
    if remaining is None or total is None or total <= 0:
        return None
    used = max(0, total - remaining)
    return max(0.0, min(100.0, used / float(total) * 100.0))


def _minimax_pick_row(rows: list) -> Optional[dict]:
    general = None
    fallback = None
    for item in rows:
        if not isinstance(item, dict):
            continue
        name = str(item.get("model_name") or item.get("modelName") or "").strip().lower()
        if name == "general":
            general = item
            break
        if fallback is None and name not in {"video"}:
            fallback = item
    return general or fallback


def _minimax_credentials() -> Optional[tuple[str, list[str]]]:
    global_key = _hermes_env_value("MINIMAX_API_KEY")
    if global_key:
        return global_key, list(MINIMAX_TOKEN_PLAN_URLS)
    cn_key = _hermes_env_value("MINIMAX_CN_API_KEY")
    if cn_key:
        return cn_key, [MINIMAX_CN_TOKEN_PLAN_URL]
    return None


def _fetch_minimax_account_usage() -> Optional[dict]:
    """MiniMax Token Plan windows from GET /v1/token_plan/remains."""
    creds = _minimax_credentials()
    if not creds:
        return None
    token, urls = creds
    import httpx

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }
    payload = None
    with httpx.Client(timeout=MINIMAX_HTTP_TIMEOUT) as client:
        for url in urls:
            try:
                response = client.get(url, headers=headers)
            except Exception:
                continue
            if response.status_code >= 400:
                continue
            try:
                body = response.json() or {}
            except Exception:
                continue
            if not isinstance(body, dict):
                continue
            base = body.get("base_resp") if isinstance(body.get("base_resp"), dict) else {}
            code = base.get("status_code")
            if code not in (None, 0, "0"):
                continue
            payload = body
            break
    if not isinstance(payload, dict):
        return None
    rows = payload.get("model_remains")
    if not isinstance(rows, list):
        return None
    item = _minimax_pick_row(rows)
    if not item:
        return None
    windows: list[dict] = []
    # *_remaining_percent fields are remaining. usage_percent is used (field name).
    interval_left = _minimax_remaining_percent(
        item,
        "current_interval_remaining_percent",
        "currentIntervalRemainingPercent",
    )
    interval_used = None
    if interval_left is not None:
        interval_used = max(0.0, min(100.0, 100.0 - interval_left))
    else:
        usage_pct = _minimax_percent(item, "usage_percent", "usagePercent")
        if usage_pct is not None:
            interval_used = usage_pct
        else:
            interval_used = _minimax_used_from_counts(
                item, "current_interval_usage_count", "current_interval_total_count"
            )
    if interval_used is not None:
        windows.append(
            _win("5h", interval_used, _parse_epoch_ms(item.get("end_time") or item.get("endTime")))
        )
    weekly_left = _minimax_remaining_percent(
        item,
        "current_weekly_remaining_percent",
        "currentWeeklyRemainingPercent",
    )
    weekly_used = None
    if weekly_left is not None:
        weekly_used = max(0.0, min(100.0, 100.0 - weekly_left))
    else:
        weekly_usage = _minimax_percent(item, "weekly_usage_percent", "weeklyUsagePercent")
        if weekly_usage is not None:
            weekly_used = weekly_usage
        else:
            weekly_used = _minimax_used_from_counts(
                item, "current_weekly_usage_count", "current_weekly_total_count"
            )
    if weekly_used is not None:
        windows.append(
            _win(
                "Weekly",
                weekly_used,
                _parse_epoch_ms(item.get("weekly_end_time") or item.get("weeklyEndTime")),
            )
        )
    if not windows:
        return None
    return _snapshot("minimax", "Token Plan", windows)


def _novita_api_key() -> Optional[str]:
    return _hermes_env_value("NOVITA_API_KEY")


def _novita_money(value: Any) -> Optional[float]:
    """Novita balances are 1/10000 USD strings or numbers."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(value):
        return float(value) / 10000.0
    if isinstance(value, str) and value.strip():
        try:
            return float(value.strip().replace(",", "")) / 10000.0
        except ValueError:
            return None
    return None


def _fetch_novita_account_usage() -> Optional[dict]:
    token = _novita_api_key()
    if not token:
        return None
    import httpx

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }
    with httpx.Client(timeout=HTTP_TIMEOUT) as client:
        response = client.get(NOVITA_BALANCE_URL, headers=headers)
        response.raise_for_status()
        payload = response.json() or {}
    if not isinstance(payload, dict):
        return None
    available = _novita_money(payload.get("availableBalance"))
    if available is None:
        return None
    parts = [f"${available:,.2f} left"]
    cash = _novita_money(payload.get("cashBalance"))
    if cash is not None and abs(cash - available) > 0.009:
        parts.append(f"${cash:,.2f} cash")
    owed = _novita_money(payload.get("outstandingInvoices"))
    if owed is not None and owed > 0:
        parts.append(f"${owed:,.2f} owed")
    return _snapshot("novita", None, [_win("Balance", None, None, " · ".join(parts))])


def _deepinfra_api_key() -> Optional[str]:
    return _hermes_env_value("DEEPINFRA_API_KEY") or _hermes_env_value("DEEPINFRA_TOKEN")


def _fetch_deepinfra_account_usage() -> Optional[dict]:
    token = _deepinfra_api_key()
    if not token:
        return None
    import httpx

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    with httpx.Client(timeout=HTTP_TIMEOUT) as client:
        response = client.get(
            DEEPINFRA_CHECKLIST_URL,
            headers=headers,
            params={"compute_owed": "true"},
        )
        response.raise_for_status()
        payload = response.json() or {}
    if not isinstance(payload, dict):
        return None
    balance_raw = payload.get("stripe_balance")
    if not isinstance(balance_raw, (int, float)) or not math.isfinite(balance_raw):
        return None
    # Negative stripe_balance = prepaid funds ready to spend.
    available = max(0.0, -float(balance_raw)) if float(balance_raw) < 0 else 0.0
    owed = float(balance_raw) if float(balance_raw) > 0 else 0.0
    parts = [f"${available:,.2f} left"] if available > 0 or owed <= 0 else []
    if owed > 0:
        parts.append(f"${owed:,.2f} owed")
    recent = payload.get("recent")
    if isinstance(recent, (int, float)) and math.isfinite(recent) and float(recent) > 0:
        parts.append(f"${float(recent):,.2f} recent spend")
    limit = payload.get("limit")
    used_percent = None
    if (
        isinstance(limit, (int, float))
        and math.isfinite(limit)
        and float(limit) > 0
        and isinstance(recent, (int, float))
        and math.isfinite(recent)
    ):
        used_percent = max(0.0, min(100.0, float(recent) / float(limit) * 100.0))
        parts.append(f"${float(limit):,.2f} spend limit")
    if payload.get("suspended") is True:
        reason = str(payload.get("suspend_reason") or "suspended").strip()
        parts.append(f"account {reason}")
    if not parts:
        return None
    return _snapshot(
        "deepinfra",
        None,
        [_win("Balance" if used_percent is None else "Spend", used_percent, None, " · ".join(parts))],
    )


def _ai_gateway_api_key() -> Optional[str]:
    return _hermes_env_value("AI_GATEWAY_API_KEY")


def _fetch_ai_gateway_account_usage() -> Optional[dict]:
    token = _ai_gateway_api_key()
    if not token:
        return None
    import httpx

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }
    with httpx.Client(timeout=HTTP_TIMEOUT) as client:
        response = client.get(AI_GATEWAY_CREDITS_URL, headers=headers)
        response.raise_for_status()
        payload = response.json() or {}
    if not isinstance(payload, dict):
        return None
    balance = payload.get("balance")
    amount = None
    if isinstance(balance, (int, float)) and math.isfinite(balance):
        amount = float(balance)
    elif isinstance(balance, str) and balance.strip():
        try:
            amount = float(balance.strip().replace(",", "").replace("$", ""))
        except ValueError:
            amount = None
    if amount is None:
        return None
    parts = [f"${amount:,.2f} left"]
    used = payload.get("total_used")
    used_amount = None
    if isinstance(used, (int, float)) and math.isfinite(used):
        used_amount = float(used)
    elif isinstance(used, str) and used.strip():
        try:
            used_amount = float(used.strip().replace(",", "").replace("$", ""))
        except ValueError:
            used_amount = None
    if used_amount is not None and used_amount > 0:
        parts.append(f"${used_amount:,.2f} used")
    return _snapshot("ai-gateway", None, [_win("Credits", None, None, " · ".join(parts))])


def _commandcode_auth_path() -> Path:
    override = (os.environ.get("COMMANDCODE_HOME") or "").strip()
    root = Path(override).expanduser() if override else _user_home() / ".commandcode"
    return root / "auth.json"


def _commandcode_api_key() -> Optional[str]:
    # Hermes' own Command Code provider uses COMMANDCODE_API_KEY. Accept the
    # underscored spelling too, then fall back to the `cmd` CLI login file.
    for name in ("COMMANDCODE_API_KEY", "COMMAND_CODE_API_KEY"):
        value = _hermes_env_value(name)
        if value:
            return value
    payload = _read_json_file(_commandcode_auth_path())
    if not isinstance(payload, dict):
        return None
    for key in ("apiKey", "api_key", "token", "accessToken"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _commandcode_number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(value) and value >= 0:
        return float(value)
    return None


def _commandcode_url(path: str, *, org_id: Optional[str] = None, since: Optional[str] = None) -> str:
    params = {}
    if org_id:
        params["orgId"] = org_id
    if since:
        params["since"] = since
    query = urlencode(params)
    return f"{COMMANDCODE_DEFAULT_BASE_URL}{path}{'?' + query if query else ''}"


def _commandcode_get(client: Any, url: str, headers: dict[str, str]) -> Optional[dict]:
    """Optional endpoint: any failure is a missing section, not a missing card."""
    try:
        response = client.get(url, headers=headers)
        if getattr(response, "status_code", 500) >= 400:
            return None
        payload = response.json() or {}
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _commandcode_get_required(client: Any, url: str, headers: dict[str, str]) -> dict:
    """Required endpoint: raise so a bad key shows as an error card, not silence."""
    response = client.get(url, headers=headers)
    response.raise_for_status()
    payload = response.json() or {}
    if not isinstance(payload, dict):
        raise ValueError("Command Code returned a non-object body")
    return payload


def _commandcode_account(payload: Optional[dict]) -> Optional[dict]:
    if not isinstance(payload, dict):
        return None
    org = payload.get("org") if isinstance(payload.get("org"), dict) else {}
    user = payload.get("user") if isinstance(payload.get("user"), dict) else {}
    login = str(org.get("login") or user.get("userName") or user.get("name") or "").strip()
    if not login:
        return None
    org_id = str(org.get("id") or "").strip() or None
    key_name = str(user.get("keyName") or user.get("displayName") or "").strip() or None
    return {"login": login, "org_id": org_id, "key_name": key_name}


def _commandcode_snapshot(
    account: dict,
    credits_payload: Optional[dict],
    subscription_payload: Optional[dict],
    summary_payload: Optional[dict],
) -> Optional[dict]:
    credits = credits_payload.get("credits") if isinstance(credits_payload, dict) else None
    credits = credits if isinstance(credits, dict) else {}
    monthly = _commandcode_number(credits.get("monthlyCredits")) or 0.0
    purchased = _commandcode_number(credits.get("purchasedCredits")) or 0.0
    free = _commandcode_number(credits.get("freeCredits")) or 0.0
    has_credit_balance = any(
        _commandcode_number(credits.get(name)) is not None
        for name in ("monthlyCredits", "purchasedCredits", "freeCredits")
    )

    windows: list[dict] = []
    window_limits = credits_payload.get("windowLimits") if isinstance(credits_payload, dict) else None
    if isinstance(window_limits, dict):
        for key, label in (("fiveHour", "5-hour"), ("weekly", "Weekly")):
            item = window_limits.get(key)
            if not isinstance(item, dict):
                continue
            used = _commandcode_number(item.get("used"))
            cap = _commandcode_number(item.get("cap"))
            if used is None or cap is None or cap <= 0:
                continue
            used = min(used, cap)
            windows.append(
                _win(
                    label,
                    used / cap * 100.0,
                    _parse_dt(item.get("resetAt")),
                    f"${max(0.0, cap - used):,.2f} of ${cap:,.2f} credits left",
                )
            )

    subscription_data = (
        subscription_payload.get("data") if isinstance(subscription_payload, dict) else None
    )
    subscription_data = subscription_data if isinstance(subscription_data, dict) else {}
    plan_raw = str(subscription_data.get("planId") or "").strip()
    plan = _title_case_slug(plan_raw) if plan_raw else None

    summary = summary_payload if isinstance(summary_payload, dict) else {}
    total_cost = _commandcode_number(summary.get("totalCost"))
    total_count = _commandcode_number(summary.get("totalCount"))
    total_tokens = _commandcode_number(summary.get("totalTokens"))
    if total_tokens is None:
        total_tokens = _commandcode_number(summary.get("tokens"))
    has_summary = total_cost is not None and total_count is not None

    details: list[str] = []
    if has_credit_balance:
        remaining = monthly + purchased + free
        sources = [f"monthly ${monthly:,.2f}", f"purchased ${purchased:,.2f}"]
        if free > 0:
            sources.append(f"free ${free:,.2f}")
        details.append(f"${remaining:,.2f} credits left · " + " / ".join(sources))
    if has_summary:
        usage = f"${total_cost:,.2f} used · {int(total_count):,} requests"
        if total_tokens is not None:
            usage += f" · {int(total_tokens):,} tokens"
        details.append(usage)
    if not windows and not details:
        return None
    return _snapshot(
        "commandcode",
        plan,
        windows,
        details,
        account_label=account.get("key_name") or account.get("login"),
        account_key=account.get("org_id") or account.get("login"),
    )


def _fetch_commandcode_account_usage() -> Optional[dict]:
    """Command Code credits, usage windows, and billing-period spend."""
    token = _commandcode_api_key()
    if not token:
        return None
    import httpx

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    with httpx.Client(timeout=HTTP_TIMEOUT) as client:
        # whoami is required: a 401 here means the key is bad, and that
        # should show on the card. The billing calls below are optional.
        account = _commandcode_account(
            _commandcode_get_required(client, _commandcode_url("/alpha/whoami"), headers)
        )
        if not account:
            raise ValueError("whoami did not include an org or user login")
        org_id = account.get("org_id")
        subscription = _commandcode_get(
            client,
            _commandcode_url("/alpha/billing/subscriptions", org_id=org_id),
            headers,
        )
        credits = _commandcode_get(
            client,
            _commandcode_url("/alpha/billing/credits", org_id=org_id),
            headers,
        )
        period_start = None
        if isinstance(subscription, dict) and isinstance(subscription.get("data"), dict):
            period_start = str(subscription["data"].get("currentPeriodStart") or "").strip() or None
        # Without a period start the summary would be all-time spend dressed
        # up as this period's. Skip it rather than mislabel it.
        summary = None
        if period_start:
            summary = _commandcode_get(
                client,
                _commandcode_url("/alpha/usage/summary", org_id=org_id, since=period_start),
                headers,
            )
    return _commandcode_snapshot(account, credits, subscription, summary)


@contextlib.contextmanager
def _hermes_usage_env():
    """Give upstream usage helpers the selected profile's keys and endpoints.

    Keep their normal config and pool lookup, including older Hermes versions.
    This runs before the CLI worker threads start.
    """
    names = ("OPENROUTER_API_KEY", "OPENAI_API_KEY", "HERMES_API_KEY",
             "OPENROUTER_BASE_URL", "OPENAI_BASE_URL", "HERMES_BASE_URL", "CUSTOM_BASE_URL")
    # Resolve missing credentials for scrubbed shell children too, preserving
    # the distinction between an absent variable and an existing blank value.
    previous = {name: os.environ.get(name) for name in names}
    try:
        for name in names:
            value = _hermes_env_value(name)
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _collect_hermes() -> list[dict]:
    with _hermes_usage_env():
        return _collect_hermes_usage()


def _collect_hermes_usage() -> list[dict]:
    try:
        from agent.account_usage import fetch_account_usage
    except Exception:
        return []
    snapshots = []
    for provider in HERMES_PROVIDERS:
        if _provider_key(provider) in _disabled_providers:
            continue
        try:
            snap = fetch_account_usage(provider)
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
        except Exception:
            continue
    return snapshots


def _collect_cli() -> tuple[list[dict], bool]:
    """Run vendor fetchers in parallel. Returns (snapshots, complete).

    complete is False when the time budget cut the run short. Caller should
    not cache incomplete results.
    """
    if not (set(LIVE_PROVIDERS) - {"nous", "openrouter"} - _disabled_providers):
        return [], True
    try:
        import httpx  # noqa: F401
    except Exception as exc:
        return [_snapshot("resetwatch", None, [], [f"probe cannot import httpx: {exc}"])], False

    pool_accounts = _codex_pool_accounts() if "openai-codex" not in _disabled_providers else []
    cli_token = ""
    cli_context = _codex_cli_access_context() if "openai-codex" not in _disabled_providers else None
    if cli_context:
        cli_token = str(cli_context[0] or "").strip()
    # (provider, label, key, fetcher). provider/label/key name the error card
    # when a fetcher raises or runs past the budget.
    fetchers: list[tuple[str, Optional[str], Optional[str], Any]] = [
        ("anthropic", None, None, _fetch_claude_accounts_usage)
    ]
    if pool_accounts:
        fetchers.extend(
            (
                "openai-codex",
                account.get("label"),
                account.get("key") or None,
                _codex_pool_fetcher(account),
            )
            for account in pool_accounts
        )
        pool_tokens = {str(account.get("token") or "") for account in pool_accounts}
        if cli_token and cli_token not in pool_tokens:
            fetchers.append(("openai-codex", None, None, _fetch_codex_cli_account_usage))
    else:
        fetchers.append(("openai-codex", None, None, _fetch_codex_cli_account_usage))
    fetchers.extend(
        (
            ("cursor", None, None, _fetch_cursor_account_usage),
            ("kimi", None, None, _fetch_kimi_account_usage),
            ("grok", None, None, _fetch_grok_account_usage),
            ("glm", None, None, _fetch_glm_zcode_account_usage),
            ("deepseek", None, None, _fetch_deepseek_account_usage),
            ("opencode-go", None, None, _fetch_opencode_go_account_usage),
            ("ollama", None, None, _fetch_ollama_cloud_account_usage),
            ("minimax", None, None, _fetch_minimax_account_usage),
            ("novita", None, None, _fetch_novita_account_usage),
            ("deepinfra", None, None, _fetch_deepinfra_account_usage),
            ("ai-gateway", None, None, _fetch_ai_gateway_account_usage),
            ("commandcode", None, None, _fetch_commandcode_account_usage),
        )
    )
    fetchers = [item for item in fetchers if item[0] not in _disabled_providers]
    if not fetchers:
        return [], True
    results: dict[tuple[int, int], dict] = {}
    complete = True

    def _failed(index: int, message: str) -> None:
        provider, label, key, _fetch = fetchers[index]
        results[(index, 0)] = _error_snapshot(provider, message, account_label=label, account_key=key)

    # One worker per fetcher so the tail of the list is never starved behind
    # a slow peer. Do not wait on hung sockets after the budget; process exit
    # reaps those threads.
    pool = ThreadPoolExecutor(max_workers=len(fetchers))
    try:
        futures = {pool.submit(item[3]): index for index, item in enumerate(fetchers)}
        pending = set(futures)
        deadline = time.monotonic() + PROBE_TOTAL_BUDGET_SECONDS
        while pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                complete = False
                break
            done, pending = wait(pending, timeout=remaining, return_when=FIRST_COMPLETED)
            if not done:
                complete = False
                break
            for fut in done:
                index = futures[fut]
                try:
                    snap = fut.result(timeout=0)
                except Exception as exc:
                    # A login exists but the call failed. Say so instead of
                    # silently dropping the vendor from the page.
                    _failed(index, _error_text(exc))
                    continue
                items = snap if isinstance(snap, list) else [snap]
                for sub, item in enumerate(items):
                    if item and (item.get("windows") or item.get("details")):
                        results[(index, sub)] = item
        if pending:
            complete = False
            for fut in pending:
                fut.cancel()
                # Cancel only works if the thread never started. Either way the
                # vendor gave nothing in time; show that rather than nothing.
                _failed(futures[fut], f"no reply within {PROBE_TOTAL_BUDGET_SECONDS}s")
    finally:
        try:
            pool.shutdown(wait=False, cancel_futures=True)
        except TypeError:
            pool.shutdown(wait=False)
    snapshots = [results[key] for key in sorted(results)]
    return snapshots, complete


def _snap_account_label(snap: dict) -> str:
    return str(snap.get("account_label") or "").strip()


def _snap_account_key(snap: dict) -> str:
    key = str(snap.get("account_key") or "").strip()
    if key:
        return key
    return _snap_account_label(snap)


def _keep_snapshot(snapshots: list[dict], have: set[str], labelled: set[str], snap: dict) -> None:
    key = _provider_key(snap.get("provider"))
    if not key:
        return
    account = _snap_account_key(snap)
    identity = f"{key}:{account.lower()}" if account else key
    if identity in have:
        return
    if account:
        kept: list[dict] = []
        for old in snapshots:
            old_key = _provider_key(old.get("provider"))
            if old_key == key and not _snap_account_label(old):
                have.discard(key)
                continue
            kept.append(old)
        snapshots[:] = kept
        labelled.add(key)
        snapshots.append(snap)
        have.add(identity)
        return
    if key in labelled:
        return
    snapshots.append(snap)
    have.add(identity)


def _emit_json(payload: Any, stream) -> None:
    text = json.dumps(_json_safe(payload), ensure_ascii=True, allow_nan=False)
    stream.write(text)
    stream.flush()


def _resolve_profile_home(name: str, here: Optional[Path] = None) -> Optional[Path]:
    """Resolve a profile beside this plugin install, including the base home."""
    name = name.strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", name):
        raise ValueError("Invalid Hermes profile name")
    start = (here or Path(__file__)).resolve()
    directory = start.parent
    if directory.parent.name == "desktop-plugins":
        base = directory.parent.parent
    elif directory.name == "desktop" and directory.parent.parent.name == "plugins":
        base = directory.parent.parent.parent
    else:
        return None
    if base.parent.name == "profiles":
        base = base.parent.parent
    candidate = base if name == "default" else base / "profiles" / name
    return candidate.resolve() if candidate.is_dir() else None


def _gateway_python_candidates(proc_root: Path = Path("/proc")) -> list[str]:
    """Discover on this backend, without resolving venv interpreter symlinks.

    On Linux shell.exec is a child of the Python gateway. Walk only our own
    ancestors, reading argv[0] (never returning or logging command arguments).
    /proc/<pid>/exe would lose the venv by resolving to the system binary.
    Other platforms and restricted proc mounts retain the environment and
    Desktop's existing home/PATH fallbacks.
    """
    candidates: list[str] = []
    explicit = os.environ.get("HERMES_PYTHON", "").strip()
    if explicit and Path(explicit).is_absolute():
        candidates.append(explicit)
    venv = os.environ.get("VIRTUAL_ENV", "").strip()
    if venv and Path(venv).is_absolute():
        candidates.append(str(Path(venv) / ("Scripts/python.exe" if os.name == "nt" else "bin/python")))
    pid = os.getppid()
    seen: set[int] = set()
    for _ in range(12):
        if pid <= 0 or pid in seen:
            break
        seen.add(pid)
        folder = proc_root / str(pid)
        try:
            with (folder / "cmdline").open("rb") as stream:
                executable = os.fsdecode(stream.read(4096).split(b"\0", 1)[0])
            if executable.startswith("/") and re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", executable.rsplit("/", 1)[-1]):
                candidates.append(executable)
            parent = re.search(r"^PPid:\s*(\d+)", (folder / "status").read_text(), re.MULTILINE)
            if parent is None:
                break
            pid = int(parent.group(1))
        except (OSError, ValueError):
            break
    return list(dict.fromkeys(candidates))


def _use_gateway_python() -> None:
    """Bootstrap with stdlib Python, then replace it with a capable runtime.

    The caller consumes --gateway-runtime before re-exec, bounding this to
    one handoff. All remaining probe flags and the backend environment survive.
    """
    deadline = time.monotonic() + 5
    for executable in _gateway_python_candidates():
        if os.path.normcase(executable) == os.path.normcase(sys.executable):
            try:
                import httpx  # noqa: F401
                return
            except Exception:
                continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            checked = subprocess.run(
                [executable, "-c", "import httpx"], stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=min(3, remaining),
            )
            if checked.returncode == 0:
                os.execv(executable, [executable, str(Path(__file__).absolute()), *sys.argv[1:]])
        except (OSError, subprocess.TimeoutExpired):
            continue


def _main_inner() -> int:
    global _profile_home, _profile_inherits_env, _disabled_providers
    if "--gateway-runtime" in sys.argv:
        sys.argv.remove("--gateway-runtime")
        _use_gateway_python()
    for arg in sys.argv[1:]:
        if arg.startswith("--disabled-providers="):
            _disabled_providers = set(filter(None, arg.split("=", 1)[1].split(",")))
            if not _disabled_providers <= set(LIVE_PROVIDERS):
                raise ValueError("Unknown disabled provider")
    if "--profile" in sys.argv:
        index = sys.argv.index("--profile")
        if index + 1 >= len(sys.argv):
            raise ValueError("--profile requires a name")
        target = _resolve_profile_home(sys.argv[index + 1].strip())
        if target is None:
            raise ValueError("Hermes profile not found beside this probe")
        inherited = (os.environ.get("HERMES_HOME") or "").strip()
        inherited_home = Path(inherited).expanduser() if inherited else next(
            (home for home in _hermes_homes() if home.is_dir()), None
        )
        _profile_inherits_env = inherited_home is not None and inherited_home.resolve() == target
        _profile_home = target
        os.environ["HERMES_HOME"] = str(target)
    cli_only = "--cli-only" in sys.argv
    fresh = "--fresh" in sys.argv
    real_stdout = sys.stdout
    # --fresh skips the 5-minute cache but still honours a short floor, so
    # repeated Refresh clicks cannot hammer vendor APIs.
    max_age = FRESH_MIN_INTERVAL_SECONDS if fresh else PROBE_MIN_INTERVAL_SECONDS
    cached = _read_probe_result_cache(cli_only=cli_only, max_age=max_age)
    if cached is not None:
        _emit_json(cached, real_stdout)
        # Non-daemon pool threads must not keep this process alive.
        os._exit(0)
    snapshots: list[dict] = []
    have: set[str] = set()
    labelled: set[str] = set()
    cli_complete = True
    # Keep gateway import/print noise off the JSON stdout contract.
    with contextlib.redirect_stdout(io.StringIO()):
        if not cli_only:
            for snap in _collect_hermes():
                _keep_snapshot(snapshots, have, labelled, snap)
        cli_snaps, cli_complete = _collect_cli()
        for snap in cli_snaps:
            _keep_snapshot(snapshots, have, labelled, snap)
    if cli_complete and snapshots:
        _store_probe_result_cache(snapshots, cli_only=cli_only)
    _emit_json(snapshots, real_stdout)
    # Non-daemon pool threads must not keep this process alive after JSON is out.
    os._exit(0)


def main() -> int:
    try:
        return _main_inner()
    except BaseException as exc:
        # Never leave stdout empty: plugin contract is always a JSON array.
        payload = [
            _snapshot(
                "resetwatch",
                None,
                [],
                [f"probe failed: {type(exc).__name__}: {exc}"],
            )
        ]
        try:
            _emit_json(payload, sys.stdout)
        except Exception:
            pass
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
