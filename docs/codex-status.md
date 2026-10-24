# Codex account status

Each Codex account is one row. The probe groups logins by the usage host, the ChatGPT account id, and the JWT `sub`. Two people in the same workspace stay two rows. The same person on two hosts stays two rows. Logins with no account id stay separate.

A healthy login covers its group. A dead or expired sibling is not shown beside it. If every login in the group is unusable, the row says why, using the same messages as before.

## The usage request

The probe sends `Authorization`, `Accept`, `User-Agent: codex-cli`, `ChatGPT-Account-Id` when the token has an account id, and `x-openai-internal-codex-residency` when the token's `https://api.openai.com/auth` claim has `chatgpt_data_residency` or `chatgpt_compute_residency`. The residency value has to be 1 to 32 characters of letters, digits, `.`, `_`, or `-`. Anything else is left off.

## Rejected logins

On 401 or 403 the probe reads an allow-listed `reason_code` from the JSON body: `token_invalidated`, `token_revoked`, `invalid_token`, `invalid_grant`, `unauthorized_client`, `refresh_token_reused`. A listed code is `sign_in`. Any other 401 or 403 is `expired`. The raw body and the token are not stored.

On 401 or 403 only, the probe tries the next healthy login in that same account. It stops after one successful check. It does not try another login on 429, on 5xx, on any other failure, or when the remaining probe time is not enough for another request.

## Quota

`quota_state` is `limited` when `limit_reached` is true, `allowed` is false, or `rate_limit_reached_type` is a non-empty string. Those fields are read from `rate_limit` first, then the top of the payload. Booleans have to be real booleans. Otherwise the quota is `available`.

## Snapshot fields

`auth_state` is `ok`, `sign_in`, `expired`, or `unknown`. `quota_state` is `available`, `limited`, or `unknown`. `reason_code` is set only for an allow-listed rejection. `last_attempt_at` and `observed_at` are the unix time of that attempt.

## What the page shows

One line per account:

- Available, plus how long ago the check ran
- Limit reached, resets at the reset time, plus the check age
- Sign in again, plus the check age, and the command `hermes auth add openai-codex --type oauth`
- Expired, not checked, plus the check age
- Unknown, plus the check age

When the reset time is already past and the check is older than that reset, the row says "Reset passed, awaiting check" and the percent bar stays hidden until a newer check arrives.

The probe does not refresh Codex tokens.
