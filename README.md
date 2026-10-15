# gee-fleet-ops

Operate a fleet of **Google Earth Engine accounts** as a pool: bulk repository health checks,
EECU quota inventory, criteria-based task cancellation, and OAuth authorization — with the
credential isolation and failure-classification rules that make it safe at 100+ accounts.

Built from a real production fleet (100+ accounts, 1200+ repositories) where every rule in
[`references/errors.md`](references/errors.md) was learned from an actual incident.

## What it does

| Script | Purpose |
|---|---|
| `scripts/health.py` | Health-check all accounts/repositories; resumable, attributes "running" vs "usable" in one pass |
| `scripts/cancel.py` | List and cancel queued/stale tasks by criteria (dry-run by default) |
| `scripts/checkin.py` | Precheck registration, enable the API, create asset roots, verify |
| `scripts/quota.py` | Per-repository and per-account EECU·h inventory |
| `scripts/authorize.py` | OAuth authorization with `state` validation and token-exchange retries |

## Install

```bash
pip install earthengine-api google-auth requests
git clone https://github.com/<you>/gee-fleet-ops
```

This repository is also usable as an agent **skill**: `SKILL.md` carries the operating procedure,
and `references/` carries the failure-mode catalog and configuration details.

## Quick start

```bash
# 1) point at your account pool
cat > ~/.gee_fleet.json <<'JSON'
{
  "accounts_root": "~/gee_accounts",
  "index_file": "~/gee_accounts/_index.json",
  "proxy": "socks5h://127.0.0.1:7890"
}
JSON

# 2) check the fleet (resumable; running accounts are deferred to a later pass)
python scripts/health.py --status
python scripts/health.py

# 3) write per-account verdicts you can read before dispatching work
python scripts/health.py --annotate ./verdicts

# 4) see what is queued, then cancel deliberately
python scripts/cancel.py                 # dry-run
python scripts/cancel.py --apply --hours 3
```

## The rules that matter

1. **One account = one subprocess** — `ee` mixes credentials within a process; use
   `gee_fleet.run_in_account()`.
2. **A single failing repository must never abort the account scan.**
3. **Network failure ≠ unavailability** — keep "confirmed unusable" apart from "not determined".
4. **Recompute account verdicts when repository verdicts change.**
5. **Cancel with `POST {operation}:cancel`**, never `DELETE` (returns 501, cancels nothing).
6. **Operation times are `createTime` / `startTime` / `updateTime`.**
7. **Never write Windows path literals containing backslashes** (`\a`, `\7`, `\1` corrupt names).

Full details, symptoms, and root causes: [`references/errors.md`](references/errors.md).

## Notes on quota semantics

`references/quota-and-limits.md` documents why a per-repository hour budget behaves as an
*account-wide envelope*: on the measured fleet, 73 accounts consumed 73,845 EECU·h with a
1,012 h/account average and single repositories exceeding 2,200 h. Reading the budget as a
per-repository ceiling overestimates fleet capacity by roughly an order of magnitude.

## License

MIT — see [LICENSE](LICENSE).
