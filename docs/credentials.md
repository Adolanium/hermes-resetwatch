# Where Resetwatch reads Hermes logins

The probe asks Hermes for credentials through Hermes's own helpers. It does not refresh tokens and it does not write login files.

## Pooled logins

`read_credential_pool` in `hermes_cli.auth` returns the saved pool for one provider, including Hermes's own fallback to the base home when a profile has no rows for that provider. If the helper cannot be imported, or it raises, the probe skips the pool and does not open `auth.json` directly.

## Standalone Codex login

`resolve_codex_runtime_credentials(read_only=True)` in `hermes_cli.auth_codex` returns Hermes's own Codex login outside the pool without Codex CLI adoption, token refresh, or auth-store writes. If the helper cannot be imported, does not accept `read_only=True` (older Hermes), or raises, the probe skips that source and does not open `auth.json` directly.

## Profile env files

`get_secret` from `agent.secret_scope` is the helper for `$HERMES_HOME/.env`. The probe runs in its own process, so a bare `get_secret` call would only see the process environment. When the module imports, the probe loads the profile file with `load_env_file`, installs that map with `set_secret_scope`, then calls `get_secret`. Names missing from that file stay missing. They are not filled from another profile's environment. If the module cannot be imported, or the read raises, the probe skips `.env` and does not parse it directly. Process environment and the existing secret-source cache still come first when the profile inherits them. The probe does not refresh secret sources and does not write the cache.

## The Anthropic OAuth seat

`read_hermes_oauth_credentials` in `agent.anthropic_credentials` returns Hermes's saved Anthropic OAuth credentials without refreshing or writing the token. If the helper cannot be imported, or it raises, the probe skips that source and does not open `.anthropic_oauth.json` directly. Pooled `anthropic` rows still go through `read_credential_pool`.

## The Nous portal login

A Nous plan with usage windows, such as Hermes Plus, reports no dollar bars, so the gateway's `usage.bars` can't show it. The probe calls `stored_token_account_info` from `hermes_cli.nous_account`, which sends the saved Nous token to the Nous portal as it is and never refreshes it, then turns the answer into windows with `build_nous_credits_snapshot` from `agent.account_usage`. A token that has expired gives no card until Hermes refreshes it on its own. Dollar plans keep coming from the gateway.

## model.options

`host.request('model.options')` is reachable from the desktop page, not from `probe.py`. Its usage accounts are a cache, the labels are not masked, and the rows have no login id and no per-account check time. The page does not call it on the poll. Codex status stays on the probe.

## Vendor app files

Logins that live in a vendor app's own directory are not Hermes `auth.json`, `.anthropic_oauth.json`, or `.env`. Those reads stay as they are. Copilot already uses `get_secret` and does not open Hermes `.env` or `auth.json`.

The probe does not refresh or write tokens while it checks status. Renew is a separate button in the standalone page; the catalog build has none. It asks Hermes to refresh one Codex login, and the probe is not involved.
