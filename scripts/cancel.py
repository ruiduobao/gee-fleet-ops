#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""cancel — 按判据取消在跑/排队任务（默认只列清单，必须 --apply 才真取消）。

判据:
  · PENDING / READY            可取消（未开始跑，不损失已烧算力）
  · RUNNING 已跑 >= --hours     可取消（默认 3 小时）
  · RUNNING < --hours           默认保留（可能只是慢）；--all-running 可强行全取消

用法:
  python cancel.py                      # dry-run：列出会被取消的任务
  python cancel.py --apply              # 按判据取消
  python cancel.py --apply --hours 1    # 放宽阈值到 1 小时
  python cancel.py --apply --all-running   # 连短时 RUNNING 也取消
  python cancel.py --accounts a b --apply

退出码: 0 正常
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gee_fleet as gf  # noqa: E402


def scan_account(cfg, acct, repos, hours, all_run):
    sess, err = gf.session_for(cfg, acct)
    if err:
        return acct, err, [], []
    todo, keep = [], []
    for pid in repos:
        ops = gf.list_operations(cfg, sess, pid)     # 单仓失败只返回部分，不中断整账号
        for op in ops:
            do, why = gf.should_cancel(op, hours=hours, all_running=all_run)
            (todo if do else keep).append((pid, op, why))
    return acct, "OK", todo, keep


def main():
    ap = argparse.ArgumentParser(description="按判据取消 GEE 任务")
    ap.add_argument("--config")
    ap.add_argument("--accounts", nargs="*", default=None)
    ap.add_argument("--hours", type=float, default=3.0,
                    help="RUNNING 超过该时长才取消（默认 3）")
    ap.add_argument("--all-running", action="store_true", help="连短时 RUNNING 也取消")
    ap.add_argument("--apply", action="store_true", help="真正执行取消（默认只列清单）")
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()

    cfg = gf.load_config(a.config)
    idx = gf.load_index(cfg)
    accts = a.accounts or sorted(idx) or gf.list_accounts(cfg)
    if not accts:
        print("没有账号（检查 accounts_root / index_file 配置）")
        return 2

    print("扫描 %d 个账号（阈值 %.0f 小时%s）…" % (len(accts), a.hours,
                                            "，含短时 RUNNING" if a.all_running else ""))
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        results = list(ex.map(lambda x: scan_account(cfg, x, idx.get(x) or [], a.hours, a.all_running),
                              accts))

    tot_todo = tot_keep = 0
    plan = []
    for acct, err, todo, keep in results:
        if err == "NO_CRED":
            print("  %-16s ⛔ 无凭证（其队列无法扫描）" % acct)
            continue
        if todo:
            by = defaultdict(lambda: [0, 0, 0.0])   # run, pend, eecu
            for pid, op, why in todo:
                run = 1 if why.startswith("RUNNING") else 0
                by[pid][0] += run
                by[pid][1] += 0 if run else 1
                md = op.get("metadata", {}) or {}
                try:
                    by[pid][2] += float(md.get("batchEecuUsageSeconds", 0) or 0) / 3600.0
                except Exception:
                    pass
            tot_todo += len(todo)
            print("  [取消] %-16s %3d 个（RUN %d / PEND %d） 占 %d 个仓"
                  % (acct, len(todo), sum(v[0] for v in by.values()),
                     sum(v[1] for v in by.values()), len(by)))
            plan.append((acct, todo))
        if keep:
            tot_keep += len(keep)
            print("  [保留] %-16s %3d 个（<%.0fh，可能只是慢）" % (acct, len(keep), a.hours))

    print("\n可取消 %d 个，保留 %d 个" % (tot_todo, tot_keep))
    if not tot_todo:
        print("没有需要取消的任务。")
        return 0
    if not a.apply:
        print("（dry-run，未执行。加 --apply 真正取消）")
        return 0

    logs = gf.state_path(cfg, "cancel_%s.log" % time.strftime("%Y%m%d_%H%M%S"))
    ok = fail = 0
    with open(logs, "w", encoding="utf-8") as fh:
        for acct, todo in plan:
            sess, err = gf.session_for(cfg, acct)
            if err:
                continue
            for pid, op, why in todo:
                name = op.get("name", "")
                fh.write("%s\t%s\t%s\t%s\n" % (acct, pid, name, why))
                good, msg = gf.cancel_operation(sess, name, pid)
                if good:
                    ok += 1
                else:
                    fail += 1
                    print("  ✗ %-14s %s  %s" % (acct, name.rsplit("/", 1)[-1][:26], msg))
    print("\n取消完成：成功 %d，失败 %d" % (ok, fail))
    print("明细: %s" % logs)
    if fail:
        print("失败的多数是代理抖动 —— 原样重跑一次本命令即可收敛。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
