# -*- coding: utf-8 -*-
"""gee_fleet — 多账号 GEE 账号池管理的共享层。

设计要点（都是实战踩坑换来的，改代码前先读 docs/错误手册）：
  · **每账号一个独立子进程**：`ee` 多账号在单进程内必然串号（症状：permission denied /
    asset not found），因为 ee 用 HOME/USERPROFILE 定位凭证。任何多账号并发都必须
    走 `run_in_account()` 拉子进程，不要在调用方进程里切账号。
  · **凭证文件格式**：`<账号目录>/.config/earthengine/credentials`，键为
    `{redirect_uri, refresh_token, scopes}`（与官方 CLI 产出完全一致，
    无 client_id/secret → 需配 `ee.oauth.CLIENT_ID/CLIENT_SECRET`）。
  · **单仓失败绝不放弃整个账号**：见 errors.py 的 E5。
  · **EXC（网络失败）≠ 不可用**：见 errors.py 的 E6。

环境要求：python 3.9+，pip install earthengine-api requests google-auth
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Iterable, List, Optional, Tuple

# ---------------------------------------------------------------- 配置

DEFAULT_CONFIG = {
    # 账号池根目录（含 <账号>/.config/earthengine/credentials）
    "accounts_root": "~/.gee_accounts",
    # 索引文件（账号 -> 仓库清单）。留空则用目录名 + 自动发现
    "index_file": "",
    # 出口代理；GEE 必须 socks5h://（http:// 前缀会失败）
    "proxy": "socks5h://127.0.0.1:7890",
    # 状态/日志目录（相对 accounts_root）
    "state_dir": "_logs",
    # 每账号同时提交的任务数上限（账号级并发，保守为 1）
    "max_parallel_per_account": 1,
    # 单仓软额度（EECU·h）——注意：这是"账号总量包络"不是单仓硬上限，见 docs
    "repo_quota_eecu_h": 150,
}

CONFIG_ENV = "GEE_FLEET_CONFIG"


def load_config(path: Optional[str] = None) -> dict:
    """读配置：优先 --config > 环境变量 GEE_FLEET_CONFIG > ~/.gee_fleet.json > 默认"""
    cfg = dict(DEFAULT_CONFIG)
    p = path or os.environ.get(CONFIG_ENV) or os.path.expanduser("~/.gee_fleet.json")
    if p and os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception as e:  # 配置坏了要早失败，不要静默用默认值
            raise SystemExit("[gee_fleet] 配置文件解析失败 %s: %s" % (p, e))
    cfg["accounts_root"] = os.path.abspath(os.path.expanduser(cfg["accounts_root"]))
    return cfg


def state_path(cfg: dict, name: str) -> str:
    d = os.path.join(cfg["accounts_root"], cfg.get("state_dir", "_logs"))
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, name)


# ---------------------------------------------------------------- 账号池

def list_accounts(cfg: dict) -> List[str]:
    """列出所有带凭证的账号目录名（无凭证的目录不算账号）。"""
    root = cfg["accounts_root"]
    out = []
    if not os.path.isdir(root):
        return out
    for name in sorted(os.listdir(root)):
        d = os.path.join(root, name)
        if name.startswith(("_", ".")) or not os.path.isdir(d):
            continue
        if os.path.exists(os.path.join(d, ".config", "earthengine", "credentials")):
            out.append(name)
    return out


def cred_path(cfg: dict, acct: str) -> str:
    return os.path.join(cfg["accounts_root"], acct, ".config", "earthengine", "credentials")


def acct_env(cfg: dict, acct: str) -> dict:
    """该账号的独立环境（HOME/USERPROFILE 双设是凭证隔离的关键）。"""
    d = os.path.join(cfg["accounts_root"], acct)
    env = dict(os.environ)
    env.update({
        "HOME": d,
        "USERPROFILE": d,
        "PYTHONUNBUFFERED": "1",
        "HTTP_PROXY": cfg["proxy"],
        "HTTPS_PROXY": cfg["proxy"],
    })
    return env


def load_index(cfg: dict) -> Dict[str, List[str]]:
    """读 <账号, [仓库]> 索引。

    支持两种来源（按顺序）：
      1. `index_file` 指向的 JSON：{"<账号>": ["pid", ...], ...}
      2. 缺省：从每个账号目录下的 `_repos.json` / `_repos.txt` 读；都没有则返回空
    """
    if cfg.get("index_file") and os.path.exists(cfg["index_file"]):
        with open(cfg["index_file"], encoding="utf-8") as f:
            return {k: list(v) for k, v in json.load(f).items()}
    out = {}
    for acct in list_accounts(cfg):
        d = os.path.join(cfg["accounts_root"], acct)
        for fn in ("_repos.json", "_repos.txt"):
            p = os.path.join(d, fn)
            if not os.path.exists(p):
                continue
            with open(p, encoding="utf-8") as f:
                if fn.endswith(".json"):
                    v = json.load(f)
                    out[acct] = list(v if isinstance(v, list) else v.get("repos", []))
                else:
                    out[acct] = [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
            break
    return out


def save_index(cfg: dict, index: Dict[str, List[str]]) -> None:
    if not cfg.get("index_file"):
        raise SystemExit("[gee_fleet] 未配置 index_file，无法统一写索引；"
                         "请设置后重试，或改用每账号 _repos.json")
    with open(cfg["index_file"], "w", encoding="utf-8") as f:
        json.dump({k: list(v) for k, v in sorted(index.items())}, f,
                  ensure_ascii=False, indent=1)


# ---------------------------------------------------------------- REST 会话
# 直接走 REST（而非 ee.Initialize）的场景：只要探测/列举/取消，无需上传表达式。
# 好处：不受 ee 客户端全局状态影响，且可在同一进程内并发多账号（凭证显式传入）。

def session_for(cfg: dict, acct: str, retries: int = 0):
    """返回 (AuthorizedSession, None) 或 (None, 错误码)。错误码见 errors.py。"""
    try:
        import google.oauth2.credentials
        from google.auth.transport.requests import AuthorizedSession
        from ee import oauth as ee_oauth
    except Exception:
        return None, "NO_SDK"
    cf = cred_path(cfg, acct)
    if not os.path.exists(cf):
        return None, "NO_CRED"
    try:
        with open(cf, encoding="utf-8") as f:
            c = json.load(f)
        sess = AuthorizedSession(google.oauth2.credentials.Credentials(
            token=None, refresh_token=c["refresh_token"],
            token_uri="https://oauth2.googleapis.com/token",
            client_id=ee_oauth.CLIENT_ID, client_secret=ee_oauth.CLIENT_SECRET,
            scopes=c.get("scopes")))
    except Exception:
        return None, "NO_CRED"
    sess.proxies = {"http": cfg["proxy"], "https": cfg["proxy"]}
    return sess, None


def authed_get(sess, url: str, pid: Optional[str] = None, timeout: int = 60,
               retries: int = 3, backoff: float = 6.0):
    """带重试的 GET。代理抖动是本类工作的常态，**所有**网络调用都要重试。"""
    import time
    headers = {"X-Goog-User-Project": pid} if pid else {}
    last = None
    for i in range(retries + 1):
        try:
            return sess.get(url, headers=headers, timeout=timeout)
        except Exception as e:
            last = e
            if "invalid_grant" in str(e):
                raise
            if i < retries:
                time.sleep(backoff)
    raise last


def authed_request(sess, method: str, url: str, pid: Optional[str] = None,
                   json_body: Optional[dict] = None, timeout: int = 60,
                   retries: int = 3, backoff: float = 6.0):
    import time
    headers = {"X-Goog-User-Project": pid} if pid else {}
    last = None
    for i in range(retries + 1):
        try:
            return sess.request(method, url, json=json_body, headers=headers, timeout=timeout)
        except Exception as e:
            last = e
            if "invalid_grant" in str(e):
                raise
            if i < retries:
                time.sleep(backoff)
    raise last


# ---------------------------------------------------------------- 结论分类

def classify_http(resp) -> Tuple[str, str]:
    """把一次 config/operations 响应归类。返回 (verdict, detail)。

    verdict 取值：
      OK             可用
      UNREGISTERED   项目未注册 EE
      EXPIRED        非商业资格过期
      API_OFF        EE API 未启用
      WRONG_OWNER    该账号无权（索引归属有误）
      AUTH_FAIL      凭证失效
      HTTP_NNN      其它 HTTP
      EXC            网络异常（**未测出**，不是不可用）
    """
    if resp is None:
        return "EXC", ""
    if resp.status_code == 200:
        return "OK", ""
    tx = (resp.text or "")
    if "expired" in tx:
        return "EXPIRED", ""
    if "not registered" in tx:
        return "UNREGISTERED", ""
    if "has not been used" in tx:
        return "API_OFF", ""
    if "required permission" in tx:
        return "WRONG_OWNER", ""
    return "HTTP_%d" % resp.status_code, tx.replace("\n", " ")[:120]


# 结论 → 人可读的处置建议（CLI 与 annotate 共用）
ERR_HINT = {
    "OK": "可用",
    "UNREGISTERED": "未注册 EE —— 到 console 的 configuration 页注册该项目",
    "EXPIRED": "非商业资格过期 —— 到 configuration/register 页重新验证资格",
    "API_OFF": "EE API 未启用 —— 跑一次 checkin 即可（serviceusage enable）",
    "WRONG_OWNER": "该账号无权访问 —— 通常是索引把项目挂错了账号",
    "AUTH_FAIL": "凭证失效（invalid_grant）—— 需重新授权该账号",
    "NO_CRED": "本地无凭证文件 —— 需先授权",
    "NO_SDK": "缺少 earthengine-api / google-auth —— pip install earthengine-api google-auth",
    "EXC": "本次未测出（网络/代理抖动）—— 需复测",
    "ALL_BAD": "名下仓库全部不可用",
}

# 只有这些是"确认不可用"；EXC/HTTP_* 属"未测出"，不得写进禁令
BAD_CONFIRMED = {"UNREGISTERED", "EXPIRED", "API_OFF", "WRONG_OWNER", "AUTH_FAIL"}
UNKNOWN = {"EXC", "HTTP_429", "HTTP_500", "HTTP_502", "HTTP_503", "HTTP_504"}


def is_unknown(verdict: str) -> bool:
    return verdict in UNKNOWN or verdict.startswith("HTTP_5") or verdict == "EXC"


def recompute_account(repos: Dict[str, str]) -> str:
    """由仓级判定重算账号级状态。

    🔴 必须在仓级判定更新后调用：首轮全 EXC → ALL_BAD，复测后仓变 OK 若不重算，
    好账号会被永久误封（真实事故 d2f47c8）。
    优先级：AUTH_FAIL > ALL_BAD > OK
    """
    if not repos:
        return "OK"
    if any(v == "AUTH_FAIL" for v in repos.values()):
        return "AUTH_FAIL"
    nbad = sum(1 for v in repos.values() if not is_unknown(v) and v != "OK")
    nunk = sum(1 for v in repos.values() if is_unknown(v))
    if nbad == len(repos):
        return "ALL_BAD"
    if nbad + nunk == len(repos) and nbad > 0:
        return "ALL_BAD"
    return "OK"


# ---------------------------------------------------------------- 任务列举/取消

def list_operations(cfg: dict, sess, pid: str, max_pages: int = 4,
                    page_size: int = 100) -> List[dict]:
    """列出某仓的 operations（含 state / batchEecuUsageSeconds / 时间字段）。

    🔴 单仓失败只返回已取到的部分（调用方按仓继续），绝不抛出中断整账号扫描。
    """
    API = "https://earthengine.googleapis.com/v1/projects/%s/operations" % pid
    out, token = [], None
    for _ in range(max_pages):
        url = API + "?pageSize=%d" % page_size + ("&pageToken=" + token if token else "")
        try:
            r = authed_get(sess, url, pid=pid, retries=2)
        except Exception:
            return out
        if r.status_code != 200:
            return out
        j = r.json()
        out.extend(j.get("operations", []) or [])
        token = j.get("nextPageToken")
        if not token:
            break
    return out


def op_age_hours(op: dict, now: Optional[float] = None):
    """任务的已运行时长（小时）。字段是 createTime/startTime（camelCase），
    没有 startTimestamp——猜字段名会永远拿到 None（真实事故）。"""
    import time
    md = op.get("metadata", {}) or {}
    ts = md.get("startTime") or md.get("createTime") or ""
    if not ts:
        return None
    t = ts.replace("Z", "").split(".")[0]
    try:
        st = time.strptime(t, "%Y-%m-%dT%H:%M:%S")
    except Exception:
        return None
    return ((now or time.time()) - time.mktime(st)) / 3600.0


def should_cancel(op: dict, hours: float = 3.0, all_running: bool = False):
    """按判据决定是否取消。返回 (是否取消, 原因)。

    判据（用户 2026-09-24 指令）：
      · PENDING/READY          → 取消（未开始跑，不损失已烧算力）
      · RUNNING 且已跑 >= hours → 取消（多半卡住）
      · RUNNING < hours        → 默认保留（可能只是慢）
    """
    md = op.get("metadata", {}) or {}
    state = (md.get("state") or "").upper()
    if state in ("PENDING", "READY"):
        return True, state
    if state == "RUNNING" or op.get("done") is False:
        age = op_age_hours(op)
        if all_running:
            return True, "RUNNING(--all-running)"
        if age is not None and age >= hours:
            return True, "RUNNING>%.0fh" % hours
        return False, "RUNNING<%.0fh" % hours
    return False, state


def cancel_operation(sess, op_name: str, pid: str) -> Tuple[bool, str]:
    """取消一个 operation。

    🔴 端点必须是 POST {operation}:cancel（LRO 取消语义）。
       用 DELETE 会得到 501 Unimplemented —— 任务一个都不会取消（真实事故）。
       404/410 表示已不在队列，算成功。
    """
    try:
        r = authed_request(sess, "POST", op_name + ":cancel", pid=pid, retries=3)
    except Exception as e:
        return False, str(e)[:80]
    if r.status_code in (200, 202, 204):
        return True, "cancelled"
    if r.status_code in (404, 410):
        return True, "gone"
    return False, "HTTP%d %s" % (r.status_code, (r.text or "").replace("\n", " ")[:70])


# ---------------------------------------------------------------- 子进程工人

def run_in_account(cfg: dict, acct: str, script: str, args: Iterable[str] = (),
                   timeout: int = 3600) -> Tuple[int, str, str]:
    """在"该账号专属子进程"里跑一个脚本（凭证隔离唯一可靠方案）。

    返回 (returncode, stdout, stderr)。子进程环境已设好 HOME/USERPROFILE/代理。
    """
    d = os.path.join(cfg["accounts_root"], acct)
    if not os.path.isdir(d):
        return 1, "", "账号目录不存在: " + d
    cmd = [sys.executable, script] + [str(a) for a in args]
    try:
        p = subprocess.run(cmd, cwd=os.path.dirname(os.path.abspath(script)) or None,
                           env=acct_env(cfg, acct), capture_output=True, text=True,
                           timeout=timeout, errors="replace")
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    return p.returncode, p.stdout or "", p.stderr or ""


def map_accounts(fn, accounts: Iterable[str], workers: int = 4):
    """按账号并行（瓶颈通常在代理往返而非本地 CPU；workers 别开太大）。"""
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        return list(ex.map(fn, accounts))


# ---------------------------------------------------------------- 文本卫生

CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def scan_control(text: str) -> dict:
    """扫描控制字符与损坏字符。

    🔴 常规 `[\\x00-\\x08...]` 扫描**刻意排除 TAB 与 CR**，而 `\\t`/`\\r` 型转义腐蚀
    恰恰藏在那里 → 必须单独计数。
    """
    return {
        "ctrl_other": len(CTRL.findall(text)),
        "tab": text.count("\t"),
        "cr": text.count("\r"),
        "replacement": text.count("\ufffd"),
    }


def safe_path(*parts: str) -> str:
    """拼 Windows 路径的安全方式。

    🔴 非 raw 字符串里 `\\a` `\\1` `\\7` `\\v` `\\0` 等会被当成转义吃掉首字符
    （真实事故：`gee_accounts\\a7c4e91` → BEL + a truncated name）。
    拼路径一律用 `os.path.join` 或本函数，**不要写含反斜杠的字面量**。
    """
    return os.path.join(*parts) if parts else ""
