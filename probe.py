"""Stock Hermes usage probe for Resetwatch.

Prints JSON snapshots for Claude, Codex, and OpenRouter using fetchers
the gateway already ships. No tokens. No extra RPC.
"""

from __future__ import annotations

import json
import sys


PROVIDERS = ("anthropic", "openai-codex", "openrouter")


def _window(window):
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


def main() -> int:
    try:
        from agent.account_usage import fetch_account_usage
    except Exception:
        sys.stdout.write("[]")
        return 0

    snapshots = []
    for provider in PROVIDERS:
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
                "windows": [_window(item) for item in (snap.windows or ())],
            }
        )
    json.dump(snapshots, sys.stdout, ensure_ascii=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
