# Quota, concurrency, and what the numbers actually mean

## Reading consumption

`metadata.batchEecuUsageSeconds` on each operation is the real meter. Sum it per repository and
per account (`scripts/quota.py` does this).

Important properties:

- **PENDING operations accrue too.** A task that has not started can already show hours.
  Do not use `state == RUNNING` as a proxy for "consuming quota".
- **An operation's value can lag its real progress**, and can resume updating later — a stalled
  counter is not proof of a dead task.
- **`listOperations` pagination can truncate** for accounts with hundreds of historical
  operations, so a "missing" task may just be on a later page. `quota.py` pages by default.

## The per-repository baseline is an envelope, not a ceiling

Measured on a live fleet (73 accounts, ~890 repositories): total consumption was **73,845
EECU·h**, averaging **1,012 h per account** — about **56%** of a naive 12 × 150 h envelope.
Fleet accounts concentrated 1,000–1,500 h on a **single hot repository**, and **12% of
repositories exceeded 150 h** (the maximum observed was ~2,225 h from just 7 operations).

Consequences:

- Do **not** stop a repository because it passed a per-repo baseline; judge the **account total**.
- Do **not** estimate fleet capacity as *repositories × per-repo baseline*; that overestimates by
  roughly an order of magnitude.
- A single operation can legitimately run for hundreds of hours. Before treating a long RUNNING
  task as stuck, use the age threshold deliberately (`cancel.py --hours`).

## Concurrency

- **One running task per account** is the practical constraint. Extra submissions to the same
  account queue behind the first; a stuck head-of-line task blocks the entire account. Prefer
  submitting to a different account's repository and racing (first result wins) over deepening a
  queue.
- Parallelism across accounts is what scales. The bottleneck is usually the proxy round trip, not
  local CPU, so a few accounts in flight is typically enough; raising worker counts mostly adds
  flakiness.
- A short-lived interactive call (`getInfo`, `sample` previews, a project listing) is not subject
  to the batch queue, but large aggregations can hit global concurrency limits — wrap them in
  try/except and fall back to local evaluation.

## Signs of a queue that is stalled rather than slow

- Many `PENDING` operations with `updateTime == createTime` and zero accrued EECU.
- `READY` for hours with no progress while a sibling repository completes quickly.
- The same task shape completing in minutes on another account.

The fix is to schedule elsewhere, not to wait: submit the same work to an idle repository or
account and let whichever finishes first carry the result. Cancelling the stalled task is optional
and, if it has accrued nothing, cheap (`cancel.py` will include it as `PENDING`).

## Proxy behavior

These endpoints must go through a SOCKS5 proxy when one is required: `socks5h://` is the correct
scheme, and `http://` fails against Google's APIs. Proxy hiccups are routine (TLS resets mid
handshake, brief total outages) and produce errors indistinguishable from permission failures if
you only look at the top-level exception. Two habits prevent misdiagnosis:

1. Retry every network call, and re-probe before concluding anything is broken.
2. Classify results as *confirmed* vs *not determined* (see `references/errors.md` B2).
