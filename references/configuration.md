# GEE Fleet Ops — Configuration

## Config file

Resolution order: `--config <path>` → `$GEE_FLEET_CONFIG` → `~/.gee_fleet.json` → built-in defaults.

```json
{
  "accounts_root": "~/gee_accounts",
  "index_file": "~/gee_accounts/_index.json",
  "proxy": "socks5h://127.0.0.1:7890",
  "state_dir": "_logs",
  "max_parallel_per_account": 1,
  "repo_quota_eecu_h": 150
}
```

| Key | Meaning | Notes |
|---|---|---|
| `accounts_root` | Where account directories live | `~` is expanded |
| `index_file` | JSON mapping account → repository IDs | Optional; falls back to per-account `_repos.json` |
| `proxy` | Egress proxy for Google APIs | **Must be `socks5h://`**; `http://` fails against these endpoints |
| `state_dir` | Where progress/log files are written | Relative to `accounts_root` |
| `max_parallel_per_account` | Tasks submitted concurrently per account | Keep at 1 unless you have measured otherwise |
| `repo_quota_eecu_h` | Reporting baseline per repo | An account-wide envelope, not a repo ceiling |

## Account layout

Each account is a directory containing credentials in **exactly the format the official CLI
writes**. The tools read them directly; do not invent a new format.

```text
<accounts_root>/
├── _index.json                     # {"acct-a": ["proj-1", "proj-2"], ...}
├── _logs/
│   ├── health_state.json           # health.py progress (resumable)
│   └── cancel_YYYYmmdd_HHMMSS.log  # cancel.py audit trail
├── acct-a/
│   ├── .config/earthengine/credentials     # {"redirect_uri","refresh_token","scopes"}
│   ├── _repos.json                         # optional per-account repo list
│   └── _scripts/                           # your per-account task scripts
└── acct-b/
    └── .config/earthengine/credentials
```

Credentials contain a **refresh token only** — no client ID/secret. Tools pair them with
`ee.oauth.CLIENT_ID` / `CLIENT_SECRET` when exchanging tokens.

> Keep credentials out of version control. If you host a fleet, commit only the scripts and a
> redacted index; add `.config/` and `_logs/` to `.gitignore`.

## Adding an account

```bash
# 1) authorize (writes the credential file)
python scripts/authorize.py --account new-acct --no-browser
#    click the printed link in the browser you want the account to live in

# 2) record its repositories
printf '["proj-aaaaaa-111111-a1","proj-bbbbbb-222222-b2"]' > <accounts_root>/new-acct/_repos.json
#    or add it to _index.json

# 3) check in (precheck + enable API + create asset root + verify)
python scripts/checkin.py --account new-acct
```

Then run `python scripts/health.py --accounts new-acct --force` so the new account's verdict is
recorded before anything is dispatched to it.

### Naming hazards

Account and project names beginning with a digit are dangerous in Python string literals:
`"...\accounts\a7c4e91"` interprets `\7` as BEL and silently drops characters. Real incidents:
`\c5e19ab` → `7c5e19ab`, `\b3d8f02` → `b3d8f02`, `\04_quota.md` → `_quota.md`.

Always build paths with `os.path.join` / `gee_fleet.safe_path()` / raw strings, and after any
bulk registration verify that each path's last segment equals the actual directory name.

## Adapting fleet scripts to accounts

`ee` keeps credentials per process, so per-account work runs in a subprocess with
`HOME`/`USERPROFILE` pointed at that account's directory:

```python
import gee_fleet as gf
cfg = gf.load_config()
rc, out, err = gf.run_in_account(cfg, "acct-a", "my_task.py", ["--year", 2020])
```

Alternatively, differentiate scripts per account at generation time (function names, asset names,
batch sizes, seeds) — see the differentiation guidance in the source fleet's documentation if you
are running many structurally identical jobs, since identical job shapes across accounts are a
recognizable pattern.
