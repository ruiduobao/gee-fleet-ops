#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""checkin — 预检 + 建 asset 根 + 验证（一个账号的一批仓库，并行一轮）。

用法:
  python checkin.py --account <账号> --repos pid1 pid2 ... [--config cfg.json]
  python checkin.py --account <账号>            # 仓库清单从索引读
  python checkin.py --account <账号> --dry      # 只探测不建根

退出码: 0 全绿 / 1 有异常 / 2 参数错误
"""
from __future__ import annotations

import argparse
import os
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gee_fleet as gf  # noqa: E402

API = "https://earthengine.googleapis.com/v1/projects/"
SU = "https://serviceusage.googleapis.com/v1/projects/"


def one(cfg, acct, sess, pid, dry=False):
    # 1) config 探活（也确认归属）
    try:
        r = gf.authed_get(sess, API + pid + "/config", pid=pid, retries=3)
    except Exception as e:
        return pid, "EXC", str(e)[:60]
    verdict, detail = gf.classify_http(r)
    if verdict != "OK":
        return pid, verdict, detail
    # 2) 启用 EE API（幂等；未注册项目此处也会 200）
    try:
        gf.authed_request(sess, "POST", SU + pid + "/services/earthengine.googleapis.com:enable",
                          pid=pid, retries=2)
    except Exception:
        pass
    # 3) 列 operations —— 200 即"已注册 + API 已启用"的充分证明
    try:
        r2 = gf.authed_get(sess, API + pid + "/operations?pageSize=1", pid=pid, retries=3)
    except Exception as e:
        return pid, "EXC", str(e)[:60]
    if r2.status_code != 200:
        v2, d2 = gf.classify_http(r2)
        return pid, v2, d2
    # 4) 建 asset 根（若不存在）
    root = "根已有"
    if not dry:
        try:
            ra = gf.authed_get(sess, API + pid + "/assets", pid=pid, retries=2)
            if ra.status_code != 200:
                rb = gf.authed_request(sess, "POST", API + pid + "/assets",
                                       pid=pid, json_body={"type": "FOLDER"}, retries=3)
                root = "建根%d" % rb.status_code
        except Exception as e:
            root = "建根异常:" + str(e)[:30]
    return pid, "OK", root


def main():
    ap = argparse.ArgumentParser(description="GEE 仓库预检 + 建根")
    ap.add_argument("--account", required=True)
    ap.add_argument("--repos", nargs="*", default=None)
    ap.add_argument("--config")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--dry", action="store_true", help="只探测，不建 asset 根")
    a = ap.parse_args()

    cfg = gf.load_config(a.config)
    sess, err = gf.session_for(cfg, a.account)
    if err:
        print("⛔ %s 不可用: %s（先完成授权）" % (a.account, gf.ERR_HINT.get(err, err)))
        return 1
    repos = a.repos or gf.load_index(cfg).get(a.account, [])
    if not repos:
        print("⛔ 没有仓库清单：用 --repos 指定，或写 <账号>/_repos.json")
        return 2

    print("== %s ==  预检 %d 个仓库%s" % (a.account, len(repos), "（dry）" if a.dry else ""))
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        res = list(ex.map(lambda p: one(cfg, a.account, sess, p, a.dry), repos))
    ok = [p for p, v, d in res if v == "OK"]
    for p, v, d in res:
        if v != "OK":
            print("  ❌ %-34s %-14s %s" % (p, v, d))
    print("结果: %d/%d 就位" % (len(ok), len(repos)))
    if len(ok) < len(repos):
        print("\n不可用仓库的处置：")
        for p, v, _ in res:
            if v == "UNREGISTERED":
                print("  注册: https://console.cloud.google.com/earth-engine/configuration?project=" + p)
            elif v == "EXPIRED":
                print("  复审: https://console.cloud.google.com/earth-engine/configuration/register?project=" + p)
            elif v == "AUTH_FAIL":
                print("  凭证失效 → 重新授权该账号（见 authorize 工具）")
    return 0 if len(ok) == len(repos) else 1


if __name__ == "__main__":
    sys.exit(main())
