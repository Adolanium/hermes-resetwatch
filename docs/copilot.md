# GitHub Copilot

Resetwatch shows the Copilot plan, how much of the included allowance is used, and when that allowance resets.

## What the card shows

The numbers come from the response. Nothing is filled in from a price sheet.

- The plan name comes from `access_type_sku` when that field is one Resetwatch knows, and otherwise from `copilot_plan`.
- On current plans the included row is labeled **AI credits**. On older plans it is labeled **Premium requests**. The share used is `100 - percent_remaining`. If that field is missing, the share is taken from `quota_remaining` divided by `entitlement` (`remaining` is the fallback). The detail line is how many of the entitlement are used.
- Unlimited rows, rows with entitlement `-1`, and rows with entitlement `0` are left off the card. They are not a finite allowance.
- Extra paid usage is a separate **Additional usage** row, and only when extra usage is turned on and the count is above zero. It is not folded into the included bar.
- The free plan uses the chat and completions allowances. Older free responses use `limited_user_quotas` (what is left) against `monthly_quotas` (the allowance).
- An organization seat with no personal allowance shows `credits_used` when that count is present. Otherwise the card says the allowance is managed by the organization. It does not invent a percentage.

## When it resets

The first field that is present wins:

1. `quota_reset_date_utc`
2. `quota_reset_date`
3. `limited_user_reset_date`
4. a snapshot `quota_reset_at` that is greater than zero
5. for a paid plan with none of those, 00:00 UTC on the 1st of the next month

GitHub documents that credits and legacy premium requests both reset at 00:00:00 UTC on the first day of the month. A free plan with no date in the response does not get a guessed reset.

## Where the login comes from

The probe does not start a login, exchange a token, or write one back. It tries each saved login until one is accepted.

1. `COPILOT_GITHUB_TOKEN`, then `GH_TOKEN`, then `GITHUB_TOKEN`, through Hermes `get_secret` when that helper can be imported. A value it returns is the one that is used, including when it reports that the active profile has no such value. If the helper cannot be imported, or it cannot answer in this process, the same names are read from the process environment.
2. The Copilot editor files `~/.config/github-copilot/apps.json` and then `hosts.json` (`%LOCALAPPDATA%\github-copilot\` on Windows). Only an `oauth_token` under a `github.com` key is used.
3. `gh auth token`, last, with prompts disabled and a 3 second limit. `GH_TOKEN` and `GITHUB_TOKEN` are removed from that command's environment so it does not simply repeat a value that was already rejected.

Classic `ghp_` personal access tokens are skipped. They are not accepted by Copilot.

The probe does not open Hermes `.env`, `auth.json`, or the gh `hosts.yml` file. A token that exists only inside a profile `.env` is invisible here unless Hermes has already put it in the process environment or `get_secret` can see it. The shell that runs the probe does not carry Hermes's in-process profile scope. Set the variable in the environment the probe inherits, sign in with `gh auth login`, or sign in with the Copilot editor.

The Copilot CLI keychain entry is not read. GitHub Enterprise hosts are not queried.

## The request

`GET https://api.github.com/copilot_internal/user`

This endpoint is not in GitHub's published REST docs. The Copilot editor and the Copilot CLI call it. GitHub can change or reject it. The request sends `Authorization: token`, `Accept: application/json`, `Editor-Version`, `Editor-Plugin-Version`, `User-Agent`, and `X-Github-Api-Version`. No token is written to the probe output. If every saved login is rejected, the card stays on the page as unavailable. If no login is saved, the card is hidden.
