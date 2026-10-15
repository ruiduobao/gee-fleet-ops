---
name: gee-fleet-ops
description: Manage a fleet of Google Earth Engine accounts — bulk repository health checks, quota inventory, task cancellation, and OAuth authorization across many accounts, each isolated in its own subprocess. Use when work spans multiple GEE projects/accounts (batch exports, quota accounting, cleaning up queued tasks) rather than a single project.
---

# GEE Fleet Ops

Tools for operating **many** Earth Engine accounts as a pool: check which accounts/repos are
usable, see what is running or queued, cancel by criteria, inventory consumed EECU·h, and
authorize new accounts. Built from a real 100+ account / 1200+ repository production fleet.

## When this applies

Use it when the task is about the **account pool** itself — health, quota, queue, credentials —
across multiple GEE projects. For classifying imagery or developing an Earth Engine script in a
single project, this skill is not needed.

## Setup (once)

```bash
pip install earthengine-api google-auth requests
```

Accounts live as one directory each, with credentials inside (same layout the official CLI writes):

```text
<accounts_root>/
├── <account-a>/.config/earthengine/credentials   # {redirect_uri, refresh_token, scopes}
├── <account-b>/.config/earthengine/credentials
└── _logs/                                        # state & logs written by these tools
```

Point the tools at it via `~/.gee_fleet.json` (see `references/configuration.md`) or
`--config path.json`:

```json
{
  "accounts_root": "~/gee_accounts",
  "index_file": "~/gee_accounts/_index.json",
  "proxy": "socks5h://127.0.0.1:7890"
}
```

`index_file` maps account → repository (GEE project) IDs: `{"acct-a": ["proj-1", "proj-2"]}`.
Without it, the tools read `<account>/_repos.json`. See `references/configuration.md` for the
full schema and for **adding a new account** (three commands).

## The four workflows

All scripts take `--config` and `--account(s)`; run them from anywhere.

### 1. Check in new repositories — `scripts/checkin.py`

Precheck registration status, enable the Earth Engine API, create the asset root folder, verify.

```bash
python scripts/checkin.py --account acct-a --repos proj-1 proj-2 proj-3
python scripts/checkin.py --account acct-a          # repos from the index
python scripts/checkin.py --account acct-a --dry    # probe only, create nothing
```

It prints the exact console URL to fix anything unavailable (registration vs. noncommercial
re-verification are **different pages** — see `references/errors.md` E-API-2).

### 2. Health check the fleet — `scripts/health.py`

One probe per repo yields both "is this repo usable" and "does it have running tasks", so
discovery and health are one pass. Resumable: state lands in `_logs/health_state.json`.

```bash
python scripts/health.py                  # check accounts not yet checked (default)
python scripts/health.py --limit 20       # cap accounts this round
python scripts/health.py --accounts a b   # specific accounts
python scripts/health.py --force          # re-check everything
python scripts/health.py --retry-unknown  # re-probe only the "not determined" repos (cheap)
python scripts/health.py --mark-only      # offline: recompute account verdicts from state
python scripts/health.py --status         # progress only, no network
python scripts/health.py --annotate out/  # write one verdict .md per account (read before dispatching)
```

**Accounts that currently have running tasks are deferred, not marked checked** — that is
deliberate: each round measures accounts while they are idle. Re-run later and it continues.

### 3. Cancel tasks by criteria — `scripts/cancel.py`

```bash
python scripts/cancel.py                        # dry-run: list what would be cancelled
python scripts/cancel.py --apply                # cancel PENDING/READY + RUNNING over 3h
python scripts/cancel.py --apply --hours 1      # tighten the age threshold
python scripts/cancel.py --apply --all-running  # ignore the age rule
```

Cancellation is irreversible and burns already-spent EECU·h. **Always dry-run first and report
the list** (account / task / EECU / age) before `--apply`. Keep tasks younger than the threshold:
a short RUNNING task may simply be slow.

### 4. Quota inventory — `scripts/quota.py`

```bash
python scripts/quota.py --account acct-a
```

Reads real `batchEecuUsageSeconds` per repository. **The baseline is an account-wide envelope,
not a per-repo ceiling** — see `references/quota-and-limits.md`; reading it as a per-repo cap
overestimates fleet capacity by roughly an order of magnitude.

### 5. Authorize an account — `scripts/authorize.py`

```bash
python scripts/authorize.py --account new-acct          # opens a browser
python scripts/authorize.py --account new-acct --no-browser   # print the link only
```

Writes credentials in the official format. Prefer this over `earthengine authenticate`: the
official flow has no `state` parameter, so a callback from a stale browser tab is accepted and
then fails with `invalid_grant: Invalid code verifier`; it also does not retry the token
exchange, so a flaky proxy aborts the whole flow.

## Rules that this skill exists to enforce

These were learned the hard way; each has a documented incident in `references/errors.md`.

1. **One account = one subprocess.** `ee` state is per-process and keyed off `HOME`/`USERPROFILE`;
   switching accounts inside one process silently mixes credentials (`permission denied` /
   `asset not found`). `gee_fleet.run_in_account()` is the supported way to run a script as an
   account. REST calls are safe in-process **only** because the session is built explicitly per
   account and passed as an argument.
2. **A single failing repo must never abort the account.** Skip that repo and continue — one 403
   (e.g. an unregistered project) otherwise hides every other repo's tasks in that account.
3. **Network failure is not unavailability.** Keep "confirmed unusable" (unregistered, expired,
   API off, wrong owner, auth failed) separate from "not determined" (network/proxy). Only the
   former belongs in a do-not-use list.
4. **Recompute account verdicts after repo verdicts change.** Otherwise an account that was
   all-errors on a flaky first pass stays flagged unusable even after every repo reports healthy.
5. **Cancellation endpoint is `POST {operation}:cancel`**, not `DELETE` (which returns 501 and
   cancels nothing).
6. **Operation timestamps are `createTime` / `startTime` / `updateTime`.** There is no
   `startTimestamp`; guessing it silently yields `None` ages.
7. **Never write backslash path literals in Python.** `"...\accounts\a7c4e91"` turns `\7` into
   BEL and eats characters (`\a`, `\1`, `\v`, `\t`, `\0` behave similarly). Use
   `gee_fleet.safe_path()` / `os.path.join` / raw strings, and verify that the last path segment
   equals the real directory name.

## References

- `references/errors.md` — 23 field-tested failure modes with symptoms, root causes, and fixes.
  Read it before writing new tooling against the GEE API.
- `references/configuration.md` — full config schema, account layout, adding an account.
- `references/quota-and-limits.md` — EECU semantics, per-account concurrency, quota reading.
