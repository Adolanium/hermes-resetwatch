# Where Resetwatch reads Hermes logins

The probe asks Hermes for credentials through Hermes's own helpers. It does not refresh tokens and it does not write login files.

## Pooled logins

`read_credential_pool` in `hermes_cli.auth` returns the saved pool for one provider. The probe uses that list when the helper imports. An empty list is the answer: the probe does not open `auth.json` after that. If the helper cannot be imported, or it raises, the probe reads `credential_pool` from the saved `auth.json` and still does not write the file. A profile with no rows for a provider keeps the existing fallback to the base home.

## Profile env files

`get_secret` from `agent.secret_scope` is the helper for `$HERMES_HOME/.env`. The probe runs in its own process, so a bare `get_secret` call would only see the process environment. When the module imports, the probe loads the profile file with `load_env_file`, installs that map with `set_secret_scope`, then calls `get_secret`. Names missing from that file stay missing. They are not filled from another profile's environment. If the module cannot be imported, or the read raises, the probe uses its own `.env` parser. Process environment and the existing secret-source cache still come first when the profile inherits them. The probe does not refresh secret sources and does not write the cache.

## The Anthropic OAuth file

`.anthropic_oauth.json` stays a direct read. Hermes's resolver for that file can refresh the token, and the catalog build removes that resolver. No read-only helper returns this one file without a refresh. The probe does not refresh it. Pooled `anthropic` rows still go through `read_credential_pool`.

## model.options

`host.request('model.options')` is reachable from the desktop page, not from `probe.py`. Its usage accounts are a cache, the labels are not masked, and the rows have no login id and no per-account check time. The page does not call it on the poll. Codex status stays on the probe.

## Vendor app files

Logins that live in a vendor app's own directory are not Hermes `auth.json`, `.anthropic_oauth.json`, or `.env`. Those reads stay as they are. Copilot already uses `get_secret` and does not open Hermes `.env` or `auth.json`.

The probe does not refresh or write tokens while it checks status.
