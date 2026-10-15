#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""quota — 逐仓 EECU 盘点（每仓已耗 EECU·h，对照基准）。

用法:
  python quota.py --account <账号> [--repos ...] [--baseline 150]

说明: EECU 值来自 listOperations 的 metadata.batchEecuUsageSeconds（真实值）。
⚠️ **基准语义**：`--baseline` 是"账号总量包络"而不是单仓硬上限。实测中舰队账号
   会把 1000-1500 h 压在单个热仓上（单仓可到 2200 h），按"每仓 150h"估产能会高估约 10 倍。
   判读请按账号合计。
"""
from __future__ import annotations

import argparse
import os
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gee_fleet as gf  # noqa: E402

API = "https://earthengine.googleapis.com/v1/projects/"


def scan(cfg, sess, pid, pages=12):
    eecu = 0.0
    ops = run = pend = 0
    token = None
    for _ in range(pages):
        url = API + pid + "/operations?pageSize=100" + ("&pageToken=" + token if token else "")
        try:
            r = gf.authed_get(sess, url, pid=pid, retries=2)
        except Exception as e:
            return pid, eecu / 3600.0, ops, run, pend, "EXC:" + str(e)[:40]
        if r.status_code != 200:
            return pid, eecu / 3600.0, ops, run, pend, "HTTP%d" % r.status_code
        j = r.json()
        for op in (j.get("operations") or []):
            ops += 1
            md = op.get("metadata", {}) or {}
            try:
                eecu += float(md.get("batchEecuUsageSeconds", 0) or 0)
            except Exception:
                pass
            st = (md.get("state") or "").upper()
            if st == "RUNNING" or op.get("done") is False:
                run += 1
            elif st in ("PENDING", "READY"):
                pend += 1
        token = j.get("nextPageToken")
        if not token:
            break
    return pid, eecu / 3600.0, ops, run, pend, ""


def main():
    ap = argparse.ArgumentParser(description="GEE 逐仓 EECU 盘点")
    ap.add_argument("--account", required=True)
    ap.add_argument("--repos", nargs="*", default=None)
    ap.add_argument("--config")
    ap.add_argument("--baseline", type=float, default=150.0)
    ap.add_argument("--workers", type=int, default=6)
    a = ap.parse_args()

    cfg = gf.load_config(a.config)
    sess, err = gf.session_for(cfg, a.account)
    if err:
        print("⛔ %s: %s" % (a.account, gf.ERR_HINT.get(err, err)))
        return 1
    repos = a.repos or gf.load_index(cfg).get(a.account, [])
    if not repos:
        print("⛔ 没有仓库清单（--repos 或 <账号>/_repos.json）")
        return 2

    print("== %s ==  盘点 %d 个仓（基准 %.0f h/仓，注意这是账号包络语义）"
          % (a.account, len(repos), a.baseline))
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        res = list(ex.map(lambda p: scan(cfg, sess, p), repos))
    total = sum(r[1] for r in res)
    for pid, h, ops, run, pend, note in sorted(res, key=lambda x: -x[1]):
        flag = "🚫" if h >= a.baseline else ("⚠️" if h >= a.baseline * 0.8 else "  ")
        print("  %s %-34s %8.2f h  ops=%-5d run=%-3d pend=%-3d %s"
              % (flag, pid, h, ops, run, pend, note))
    print("\n合计 %.1f EECU·h / %d 仓  平均值 %.1f h/仓" % (total, len(res), total / len(res)))
    print("（该账号若接近 %d h 的总包络，应考虑停派；单仓超基准属常态）"
          % int(a.baseline * len(res)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
