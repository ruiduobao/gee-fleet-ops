#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""health — 账号/仓库体检（分批 · 可续跑 · 结论可标注）。

一次探测两用：每个仓只探一次，同时得出「仓是否可用」与「有没有在跑任务」。
因此"普查在跑任务"和"体检可用性"不需要跑两遍。

用法:
  python health.py --status                 # 只看进度，不发请求
  python health.py                          # 体检尚未体检的账号（默认续跑语义）
  python health.py --limit 20               # 本轮最多 20 个账号
  python health.py --accounts a b           # 指定账号
  python health.py --force                  # 重测（含已体检）
  python health.py --retry-unknown          # 只复测"未测出"的仓（成本极低）
  python health.py --mark-only              # 不联网，只按 state 重算账号态并输出结论
  python health.py --annotate <目录>        # 把结论写入 <目录>/<账号>.md（供派活前查看）

退出码: 0 正常
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gee_fleet as gf  # noqa: E402

API = "https://earthengine.googleapis.com/v1/projects/"
UNKNOWN = gf.UNKNOWN


def probe_repo(cfg, sess, pid):
    """返回 (verdict, running, pending, eecu_h)。单仓失败不影响其它仓。"""
    try:
        r = gf.authed_get(sess, API + pid + "/config", pid=pid, retries=3)
    except Exception as e:
        return ("AUTH_FAIL" if "invalid_grant" in str(e) else "EXC"), 0, 0, 0.0, str(e)[:50]
    verdict, _ = gf.classify_http(r)
    if verdict != "OK":
        return verdict, 0, 0, 0.0, ""
    try:
        r2 = gf.authed_get(sess, API + pid + "/operations?pageSize=100", pid=pid, retries=2)
    except Exception as e:
        return "OK", 0, 0, 0.0, "listOps异常:" + str(e)[:30]
    if r2.status_code != 200:
        return "OK", 0, 0, 0.0, "listOps HTTP%d" % r2.status_code
    run = pend = 0
    eecu = 0.0
    for op in (r2.json().get("operations") or []):
        md = op.get("metadata", {}) or {}
        st = (md.get("state") or "").upper()
        try:
            eecu += float(md.get("batchEecuUsageSeconds", 0) or 0) / 3600.0
        except Exception:
            pass
        if st == "RUNNING" or op.get("done") is False:
            run += 1
        elif st in ("PENDING", "READY"):
            pend += 1
    return "OK", run, pend, eecu, ""


def do_account(cfg, acct, idx):
    sess, err = gf.session_for(cfg, acct)
    repos = idx.get(acct) or []
    if err:
        return acct, {"ts": time.strftime("%Y-%m-%d %H:%M"), "account": err, "repos": {},
                      "running": 0, "pending": 0, "eecu_h": 0.0}, "⛔ 无凭证/无 SDK"
    if not repos:
        return acct, None, "⚠️ 索引里没有该账号的仓库清单"
    out = {}
    run_t = pend_t = 0
    eecu = 0.0
    with ThreadPoolExecutor(max_workers=6) as ex:
        res = list(ex.map(lambda p: (p, probe_repo(cfg, sess, p)), repos))
    for pid, (v, r_, p_, e_, note) in res:
        out[pid] = v
        run_t += r_
        pend_t += p_
        eecu += e_
    state = gf.recompute_account(out)
    info = {"ts": time.strftime("%Y-%m-%d %H:%M"), "account": state, "repos": out,
            "running": run_t, "pending": pend_t, "eecu_h": round(eecu, 2)}
    nb = sum(1 for v in out.values() if not gf.is_unknown(v) and v != "OK")
    nu = sum(1 for v in out.values() if gf.is_unknown(v))
    avail = "仓可用 %d/%d" % (len(out) - nb - nu, len(out))
    if nu:
        avail += "  ⚠️未测出 %d" % nu
    flag = "⛔" if state != "OK" else ("🔵" if run_t else "✅")
    return acct, info, "%s %s  在跑 %d / 待跑 %d  EECU %.1f h" % (flag, avail, run_t, pend_t, eecu)


def annotate(cfg, st, outdir):
    """把体检结论写成每个账号一份的 Markdown（派活前先看它）。"""
    os.makedirs(outdir, exist_ok=True)
    n = 0
    for acct, v in sorted(st.get("checked", {}).items()):
        bad = {p: k for p, k in v.get("repos", {}).items()
               if k != "OK" and not gf.is_unknown(k)}
        unk = {p: k for p, k in v.get("repos", {}).items() if gf.is_unknown(k)}
        L = ["# %s 体检结论" % acct, "",
             "> 体检时间：%s ｜ 来源：gee-fleet-ops health" % v.get("ts", "?"), ""]
        if v["account"] == "OK":
            L.append("> ✅ **账号可用** —— %d/%d 仓可用；在跑 %d / 待跑 %d。"
                     % (len(v.get("repos", {})) - len(bad) - len(unk), len(v.get("repos", {})),
                        v.get("running", 0), v.get("pending", 0)))
        elif v["account"] == "AUTH_FAIL":
            L.append("> ⛔ **账号不可用，禁止派活** —— 凭证失效（invalid_grant）。")
            L.append("> 名下 %d 个仓随之不可用，但**无需逐个处理**：重新授权后全部恢复。" % len(v.get("repos", {})))
        else:
            L.append("> ⛔ **账号不可用，禁止派活** —— 名下仓库全部不可用。")
        if bad:
            L += ["", "> 🚫 **以下仓库确认不可用，禁止派活**：", ""]
            for p, k in sorted(bad.items()):
                L.append(">    - `%s` —— %s" % (p, gf.ERR_HINT.get(k, k)))
        if unk:
            L += ["", "> ⚠️ **以下仓库本次未测出结论**（网络抖动，非判定不可用），"
                      "派活前需复核（`health.py --retry-unknown`）：", ""]
            for p, k in sorted(unk.items()):
                L.append(">    - `%s` —— %s" % (p, k))
        with open(os.path.join(outdir, acct + ".md"), "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(L) + "\n")
        n += 1
    print("已写出 %d 份体检结论到 %s" % (n, outdir))


def main():
    ap = argparse.ArgumentParser(description="GEE 账号/仓库体检（可续跑）")
    ap.add_argument("--config")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--accounts", nargs="*", default=None)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--retry-unknown", action="store_true")
    ap.add_argument("--mark-only", action="store_true")
    ap.add_argument("--annotate", metavar="目录", help="把结论写成 <目录>/<账号>.md")
    a = ap.parse_args()

    cfg = gf.load_config(a.config)
    st_path = gf.state_path(cfg, "health_state.json")
    st = {"version": 1, "checked": {}, "rounds": []}
    if os.path.exists(st_path):
        try:
            st = json.load(open(st_path, encoding="utf-8"))
        except Exception:
            pass
    st.setdefault("checked", {})
    st.setdefault("rounds", [])

    def save():
        json.dump(st, open(st_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    if a.annotate:
        annotate(cfg, st, a.annotate)
        return 0

    if a.status:
        ck = st["checked"]
        print("账号池: %s" % cfg["accounts_root"])
        print("已体检: %d 个" % len(ck))
        from collections import Counter
        for k, v in Counter(v["account"] for v in ck.values()).most_common():
            print("   %-12s %d" % (k, v))
        print("轮次: %d" % len(st["rounds"]))
        return 0

    if a.mark_only:
        n = 0
        for acct, v in st["checked"].items():
            before = v.get("account")
            v["account"] = gf.recompute_account(v.get("repos", {}))
            if v["account"] != before:
                print("  重判 %s: %s -> %s" % (acct, before, v["account"]))
                n += 1
        save()
        print("已按 state 重算 %d 个账号的状态（改动 %d 个）" % (len(st["checked"]), n))
        return 0

    idx = gf.load_index(cfg)
    if a.retry_unknown:
        todo = {}
        for acct, v in st["checked"].items():
            if v["account"] == "AUTH_FAIL":
                continue
            cur = set(idx.get(acct) or [])
            # 剔除幽灵条目（已不在索引里）
            for p in list(v.get("repos", {})):
                if cur and p not in cur:
                    del v["repos"][p]
            ps = [p for p, k in v.get("repos", {}).items() if gf.is_unknown(k)]
            if ps:
                todo[acct] = ps
        print("待复测: %d 个账号 / %d 个仓" % (len(todo), sum(len(x) for x in todo.values())))
        fixed = 0
        for acct, ps in sorted(todo.items()):
            sess, err = gf.session_for(cfg, acct)
            if err:
                print("  %-14s ⛔ 无凭证" % acct)
                continue
            v = st["checked"][acct]
            with ThreadPoolExecutor(max_workers=6) as ex:
                res = list(ex.map(lambda p: (p, probe_repo(cfg, sess, p)), ps))
            for pid, (ver, r_, p_, e_, note) in res:
                v["repos"][pid] = ver
                if ver not in UNKNOWN and ver != "EXC":
                    fixed += 1
            v["account"] = gf.recompute_account(v["repos"])
            left = sum(1 for k in v["repos"].values() if gf.is_unknown(k))
            print("  %-14s 复测 %2d，仍有结论 %2d 未测出 %d" % (acct, len(ps), len(ps) - left, left))
        save()
        print("复测得出结论 %d 个" % fixed)
        return 0

    accts = a.accounts or sorted(idx) or gf.list_accounts(cfg)
    if not a.force:
        accts = [x for x in accts if x not in st["checked"]]
    if a.limit:
        accts = accts[:a.limit]
    print("本轮体检 %d 个账号%s" % (len(accts), "（--force）" if a.force else ""))
    if not accts:
        print("没有待体检账号。用 --force 重测全部，或 --status 看进度。")
        return 0

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=3) as ex:      # 账号级并行；瓶颈在代理
        for acct, info, msg in ex.map(lambda x: do_account(cfg, x, idx), accts):
            print("  %-16s %s" % (acct, msg), flush=True)
            if info is None:
                continue
            # 有在跑任务的账号：不计入"已体检"，留待下一轮（保证每轮测的都是空闲账号）
            if info.get("running", 0) > 0 and info["account"] == "OK":
                st.setdefault("deferred_busy", {})[acct] = {
                    "ts": info["ts"], "running": info["running"], "pending": info["pending"]}
                st["checked"].pop(acct, None)
            else:
                st["checked"][acct] = info
                st.get("deferred_busy", {}).pop(acct, None)
            save()
    st["rounds"].append({"ts": time.strftime("%Y-%m-%d %H:%M"), "processed": len(accts),
                         "elapsed_s": round(time.time() - t0, 1)})
    save()
    print("\n用时 %.0fs；已体检 %d；延后（有在跑）%d；尚未体检 %d"
          % (time.time() - t0, len(st["checked"]),
             len(st.get("deferred_busy", {})),
             len([x for x in accts if x not in st["checked"]])))
    print("state: %s" % st_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
