# GEE Fleet Ops — Failure Modes

Every entry below is a real incident from operating a 100+ account, 1200+ repository Earth Engine
pool. Symptoms are listed first because they are what you will actually see.

---

## A. GEE API protocol

### A1. Cancelling with `DELETE` returns 501 and cancels nothing
- **Symptom:** every cancel request returns `HTTP 501 Unimplemented`; tasks keep running.
- **Cause:** operations are long-running operations, not plain resources. The cancel verb is
  `POST {operationName}:cancel`.
- **Fix:** `POST https://earthengine.googleapis.com/v1/{operation}:cancel`. Treat `404`/`410` as
  success (already gone). See `gee_fleet.cancel_operation()`.
- **Incident:** a first cancellation pass over 233 tasks was a total no-op; 224 succeeded
  immediately once the endpoint was corrected.

### A2. Registration vs. noncommercial re-verification are different pages
- **Symptom:** a project returns 403 `not registered to use Earth Engine`, or 403
  `noncommercial Earth Engine access has expired`.
- **Cause:** two distinct states with two distinct console pages.
- **Fix:**
  - not registered → `console.cloud.google.com/earth-engine/configuration?project=<id>`
  - expired → `console.cloud.google.com/earth-engine/configuration/register?project=<id>`
    (re-verification; a different page, easy to miss)
- **Note:** expiry can also resolve on its own; re-probe before declaring a project lost.

### A3. 403 `required permission` usually means the project belongs to another account
- **Symptom:** a whole list of projects returns 403 with "Caller does not have required permission".
- **Cause:** the index associates a project with the wrong account.
- **Fix:** probe `GET /v1/projects/<id>/config` from both accounts — the owner returns
  `200 REGISTERED_*`, the other account returns 403. Then correct the index.

### A4. Operation timestamps are `createTime` / `startTime` / `updateTime`
- **Symptom:** task ages come out as `None` or `?` for everything.
- **Cause:** `startTimestamp` / `submitTime` do not exist on these operations.
- **Fix:** dump one real operation's metadata keys before writing the parser:
  `metadata` contains `@type, state, description, priority, createTime, updateTime, startTime, type`,
  and completed operations add `endTime, destinationUris, attempt, progress, stages, batchEecuUsageSeconds`.

### A5. Asset export destination field
- **Symptom:** `POST .../table:export` or `.../image:export` returns
  `400 Invalid JSON payload ... Unknown name "assetId"`.
- **Cause:** hand-built request bodies guess the schema. The real shape is:
  ```json
  "assetExportOptions": {"earthEngineDestination": {"name": "projects/<pid>/assets/<name>"}}
  ```
- **Fix:** do not hand-build export requests. Either use the `ee` library, or read the
  serialization code in `ee/batch.py` (`_build_earth_engine_destination`) first. The `400` body's
  `fieldViolations` names the bad field — read it instead of guessing.

### A6. `ee.Initialize` re-runs OAuth and needs retries on flaky links
- **Symptom:** a batch of probes reports `init: ... SSL EOF` for half the accounts although the
  accounts are healthy.
- **Fix:** wrap `ee.Initialize(project=pid)` in 3–4 retries (≈6 s apart). If it still fails, record
  it as *not determined* — never as *unusable*.

### A7. A brief note on detection signals
- GEE reports queue state per operation (`PENDING`, `READY`, `RUNNING`, `SUCCEEDED`, `FAILED`,
  `CANCELLED`). `PENDING` with `updateTime == createTime` for a long time means it was never
  scheduled, as opposed to being slow.
- `batchEecuUsageSeconds` is the real consumption meter, and it also accrues for `PENDING`
  operations — do not use "state == RUNNING" as a proxy for "is burning quota".

---

## B. Scanner and aggregation logic

### B1. One failing repository must not abort the account **(highest-impact entry)**
- **Symptom:** an account's queued tasks are invisible to a fleet-wide scan; two cancellation
  passes miss them entirely.
- **Cause:** the per-repository scan called `return` on the first non-200 (that account had two
  unregistered repositories returning 403), so the other ten repositories were never queried.
- **Fix:** per-repository failure → skip that repository (`continue`), never return from the
  account loop. A single repo's problem must not become account-wide blindness.
- **Incident:** an account with 45 queued tasks survived two cancellation passes this way.

### B2. Network failure (exception/5xx) is not unavailability
- **Symptom:** healthy repositories get flagged "do not use".
- **Fix:** classify into *confirmed unusable* (`UNREGISTERED`, `EXPIRED`, `API_OFF`,
  `WRONG_OWNER`, `AUTH_FAIL`) and *not determined* (timeouts, 5xx, connection errors). Only the
  former belongs in a do-not-use list; the latter needs a re-probe.
- **Incident:** a repository set showed "3/11 available" and was actually 11/11.

### B3. Recompute account verdicts after repository verdicts change
- **Symptom:** an account stays flagged unusable although every repository now reports healthy.
- **Cause:** the account-level verdict was computed once (all repositories errored on a flaky
  pass → all-bad) and never recalculated.
- **Fix:** derive the account verdict from current repository verdicts every time either changes;
  centralize it in one function (`gee_fleet.recompute_account`).
- **Precedence:** `AUTH_FAIL` outranks "all repos bad" — a credential failure makes every repo
  unavailable at once, and the fix is one re-authorization, not per-project action.

### B4. Counters need a lifecycle
- **Symptom:** a "deferred" count only grows; a report claims 17 deferred accounts when 12 remain.
- **Fix:** remove entries when they are resolved; report only what is still outstanding at report
  time.

### B5. Enumerations change shape at boundaries — cover every form
- **Symptom:** after an index passed item 99, four accounts silently disappeared from tooling.
- **Cause:** section numbering switched from spelled-out numerals to digits at 100, and parsers
  only matched the spelled-out form.
- **Fix:** match every form the scheme can produce (e.g. `[零一二三四五六七八九十\d]+`); after a
  numbering-scheme boundary, grep the whole codebase for every parser.
- **Related trap:** when fixing, the old pattern can be a *prefix* of the new one — a blind
  replace then corrupts already-fixed files. Use a negative lookahead or test for the new form first.

### B6. Fallback parsers must not scan whole documents
- **Symptom:** dates (`2026-09-02`, `09-07`) entered a repository list and were probed forever.
- **Fix:** scope fallback parsing to the specific table, require plausible ID shapes, and purge
  state entries that no longer appear in the current index before re-probing.

### B7. Changing a sentinel marker turns "replace" into "append"
- **Symptom:** a generated block appears twice in the same document.
- **Cause:** the sync script searched for the old marker text and, not finding it, appended.
- **Fix:** migrate explicitly (match both old and new markers, delete then insert), and assert the
  marker occurs exactly once after writing.

---

## C. Shell and file operations

### C1. Heredocs and `python -c` corrupt backslashes
- **Symptom:** patched code gains real newlines inside a regex, or `\d` loses its backslash, or
  backtick content executes as a shell command.
- **Fix:** write patches as files (editor/Write tool) and execute the file; avoid inline
  strings containing escapes.

### C2. `A && B` in a backgrounded command can drop `B`
- **Symptom:** stage A ran, stage B silently never did — a cancellation pass appeared to run and
  changed nothing.
- **Fix:** run significant stages as separate commands, and verify with an independent check
  (artifacts present, counters at zero) rather than trusting the chain.

### C3. Background stdout is block-buffered
- **Symptom:** a running job's log stays empty; the URL/prompt it printed never appears.
- **Fix:** `PYTHONUNBUFFERED=1`, or `sys.stdout.reconfigure(line_buffering=True)` in the script;
  a child process's own log file is unbuffered and can be read directly.

### C4. Shared documents plus interactive editors create read-modify-write races
- **Symptom:** "file has been modified since read" loops when several sessions write one index.
- **Fix:** perform edits to shared indexes with a script that reads, modifies, and writes in one
  pass, with anchor assertions and a post-write check.

### C5. A script that defines `main()` without calling it exits 0 silently
- **Symptom:** command succeeds, exit code 0, nothing happened.
- **Fix:** verify observable effects (file mtime/content assertions), not exit codes.

### C6. Control-character scanning must count TAB and CR separately
- **Symptom:** `\t` and `\r` escape corruption survives routine hygiene scans.
- **Cause:** the usual pattern `[\x00-\x08\x0b\x0c\x0e-\x1f]` deliberately excludes tab and
  carriage return.
- **Fix:** count `\t` and `\r` explicitly, plus U+FFFD; see `gee_fleet.scan_control()`.

### C7. Your own sanitizer can create corruption
- **Symptom:** a cleanup step rewrites `chr(7)` to `\7`, producing a truncated account name
  (`c5e19ab` → `7c5e19ab`).
- **Fix:** pick the most probable original (`\a` for chr(7) — 4/4 observed cases), and let a
  disk-verified repair pass (matching real directory names) resolve remaining ambiguity.

---

## D. Process and self-check

### D1. An untested feature is not a feature
- **Symptom:** a flag written to make reports honest had never been executed end-to-end.
- **Fix:** run new registry-writing code against a copy of real documents in a scratch directory
  and assert the produced text before using it on the live index.

### D2. Resending an identical request is a re-check, not a re-onboard
- **Fix:** look up each account in the index first; if present, verify it live (probe config and
  asset root) and report, rather than appending duplicate rows/sections.
- **Related:** when taking a "my section" slice, bound it at the next section header — an unbounded
  slice fed every later account's repositories into each account's probe (all 403, all misleading).

### D3. Referenced artifacts must exist
- **Symptom:** documentation refers to tooling that does not exist, or to 45 per-account scripts
  of which 29 exist.
- **Fix:** after bulk operations, verify references vs. reality (stat the referenced paths). A
  template that generates text does not generate the thing the text names.

### D4. Long index files get partially loaded
- **Fix:** keep index entries short and push detail into per-topic files; enforce a size budget and
  compress (archive detail first, then shorten) before it is exceeded.

---

## E. Cross-cutting

> **Normal-looking signals are not proof of success.** Every serious issue in section B had a
> benign surface: exit code 0, tasks "being cancelled", accounts "being checked". The only way they
> surfaced was deliberate cross-checking — assertions, disk reconciliation, and a second
> independent measurement. Budget for that verification in any bulk operation.
