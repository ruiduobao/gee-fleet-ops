#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""authorize — 带 state 校验 + 换 token 重试的 GEE 授权（替代 `earthengine authenticate`）。

为什么需要它（实测两个缺陷）:
  1. 官方 CLI 的授权 URL **不含 state**，其回调服务器照单全收 —— 浏览器里旧标签页/旧链接
     的授权码会被当成"本次结果"接收，配新 PKCE verifier 换 token 必然失败：
     `invalid_grant: Invalid code verifier`（实测 3 连败中 2 次由此而来）。
  2. 官方 `request_token` 用 urllib **单次调用、零重试** —— 代理抖动时直接
     `SSL: UNEXPECTED_EOF_WHILE_READING`，用户得重新点一次。

本工具：URL 带 state（旧回调被拒收并回"链接已过期"页）+ 换 token 重试 6 次。
凭证格式与官方完全一致：{redirect_uri, refresh_token, scopes}。

用法:
  python authorize.py --account <账号> [--port 8085] [--timeout 1800] [--no-browser]

退出码: 0 成功 / 1 换 token 失败 / 2 等待超时 / 3 端口被占
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gee_fleet as gf  # noqa: E402

SCOPES = ["https://www.googleapis.com/auth/earthengine",
          "https://www.googleapis.com/auth/cloud-platform",
          "https://www.googleapis.com/auth/drive",
          "https://www.googleapis.com/auth/devstorage.full_control"]
TOKEN_URI = "https://oauth2.googleapis.com/token"

OK_PAGE = ("<html><meta charset='utf-8'><body style='font:16px/1.6 sans-serif;padding:40px'>"
           "<h2>✅ 授权成功</h2><p>可以关闭本页。</p></body></html>")
STALE_PAGE = ("<html><meta charset='utf-8'><body style='font:16px/1.6 sans-serif;padding:40px'>"
              "<h2>⚠️ 这个授权链接已过期</h2><p>本页是<b>旧的</b>授权页，已忽略。"
              "<br>请重新获取<b>最新</b>链接后再点。</p></body></html>")


def b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def port_free(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def main():
    ap = argparse.ArgumentParser(description="GEE 授权（state 校验 + 重试）")
    ap.add_argument("--account", required=True)
    ap.add_argument("--port", type=int, default=8085)
    ap.add_argument("--timeout", type=int, default=1800)
    ap.add_argument("--config")
    ap.add_argument("--no-browser", action="store_true", help="只打印链接，不自动打开")
    a = ap.parse_args()

    cfg = gf.load_config(a.config)
    acct_dir = os.path.join(cfg["accounts_root"], a.account)
    cred = gf.cred_path(cfg, a.account)
    os.makedirs(os.path.dirname(cred), exist_ok=True)

    if not port_free(a.port):
        print("⛔ 端口 %d 已被占用：可能是上一个账号的授权进程仍活着。" % a.port)
        print("   Windows: netstat -ano | findstr :%d   然后 taskkill /F /PID <pid>" % a.port)
        print("   （不要用 taskkill /IM python.exe —— 会杀掉其它重要进程）")
        return 3

    try:
        from ee import oauth as ee_oauth
        client_id, client_secret = ee_oauth.CLIENT_ID, ee_oauth.CLIENT_SECRET
    except Exception as e:
        print("需要 earthengine-api: pip install earthengine-api  (%s)" % e)
        return 1

    redirect_uri = "http://localhost:%d" % a.port
    verifier = b64url(secrets.token_bytes(32))
    challenge = b64url(hashlib.sha256(verifier.encode()).digest())
    state = secrets.token_urlsafe(16)
    url = "https://accounts.google.com/o/oauth2/auth?" + urlencode({
        "client_id": client_id, "scope": " ".join(SCOPES), "redirect_uri": redirect_uri,
        "response_type": "code", "code_challenge": challenge,
        "code_challenge_method": "S256", "state": state})

    box = {"code": None, "stale": 0}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            q = parse_qs(urlparse(self.path).query)
            st = (q.get("state") or [""])[0]
            code = (q.get("code") or [""])[0]
            if st != state:
                box["stale"] += 1
                print("  ⚠️ 拒收一条**过期链接**的回调（state 不匹配，累计 %d）——请用最新链接"
                      % box["stale"])
                body = STALE_PAGE.encode()
            elif not code:
                body = b"<html><body>missing code</body></html>"
            else:
                box["code"] = code
                print("  ✅ 收到本链接的授权码（state 校验通过）")
                body = OK_PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", a.port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    print("\n授权链接（把整行复制到浏览器；旧链接的回调会被自动拒收）:\n%s\n" % url)
    if not a.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    print("等待授权 ……")
    t0 = time.time()
    while box["code"] is None and time.time() - t0 < a.timeout:
        time.sleep(2)
    if box["code"] is None:
        print("\nTIMEOUT %ds 未收到本链接的回调；服务仍在监听，链接仍有效。" % a.timeout)
        return 2

    import requests
    data = {"code": box["code"], "client_id": client_id, "client_secret": client_secret,
            "redirect_uri": redirect_uri, "grant_type": "authorization_code",
            "code_verifier": verifier}
    proxies = {"http": cfg["proxy"], "https": cfg["proxy"]}
    last = ""
    for i in range(6):
        try:
            r = requests.post(TOKEN_URI, data=data, proxies=proxies, timeout=60)
        except Exception as e:
            last = "%s: %s" % (type(e).__name__, str(e)[:80])
            print("  换 token 第 %d 次失败（网络类，将重试）: %s" % (i + 1, last))
            time.sleep(8)
            continue
        if r.status_code == 200:
            tok = r.json()
            with open(cred, "w", encoding="utf-8") as f:
                json.dump({"redirect_uri": redirect_uri,
                           "refresh_token": tok["refresh_token"],
                           "scopes": (tok.get("scope") or "").split() or SCOPES}, f)
            print("\nAUTH_OK  %s  [耗时 %.0fs]  凭证: %s" % (a.account, time.time() - t0, cred))
            return 0
        last = "HTTP %d %s" % (r.status_code, r.text.replace("\n", " ")[:120])
        if "invalid_grant" in r.text:
            print("  ❌ 换 token 被拒：%s" % last)
            print("     若为 Invalid code verifier → 多为点了旧授权页；请用最新链接再试。")
            return 1
        print("  换 token 第 %d 次失败: %s" % (i + 1, last))
        time.sleep(8)
    print("🔴 换 token 连续失败：%s" % last)
    return 1


if __name__ == "__main__":
    sys.exit(main())
