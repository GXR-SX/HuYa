#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地一次性抓取「完整」虎牙登录态（含 HttpOnly 的 udb_cred），并可直接写回 Secret。

为什么需要它：从 DevTools 的请求头 / 扩展导出的 Cookie 经常丢掉 HttpOnly 的
udb_cred，而 udb_cred 正是静默续期的唯一凭证 —— 没有它，Cookie 到期即死。

用法（在有桌面环境的机器上）：
    pip install playwright pyNaCl
    python -m playwright install chromium
    GH_PAT=ghp_xxx GH_REPO=owner/repo python tools/capture_cookie.py

流程：脚本打开一个真实 Chromium → 你扫码/账号密码登录虎牙 → 回到终端按回车，
脚本读取浏览器 cookie jar 里全部 huya.com Cookie（含 HttpOnly），打印字段清单，
并写回 HUYA_COOKIE（或 HUYA_COOKIES 第一行）。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gh_secret  # noqa: E402
from huya_cookie import (DEFAULT_COOKIE_FILE, cookie_health, describe_cookie,  # noqa: E402
                         format_cookie, save_cookie_cache)

LOGIN_URL = "https://www.huya.com/"


def capture(timeout_ms=180_000):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, args=["--no-sandbox"])
        context = browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"),
            viewport={"width": 1280, "height": 860},
            locale="zh-CN",
        )
        page = context.new_page()
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=timeout_ms)
        print("\n>>> 请在弹出的浏览器窗口里登录虎牙（扫码或账号密码）。")
        print(">>> 登录成功、右上角出现头像后，回到这里按回车继续。\n")
        try:
            input()
        except EOFError:
            pass
        page.wait_for_timeout(2000)

        cookies = [c for c in context.cookies() if "huya.com" in (c.get("domain") or "")]
        browser.close()

    merged = {c["name"]: c["value"] for c in cookies if c.get("name") and c.get("value")}
    return format_cookie(merged)


def main():
    cookie = capture()
    if not cookie:
        print("没有抓到任何 huya.com Cookie，登录可能没完成")
        raise SystemExit(1)

    health = describe_cookie(cookie, tag="[抓取]")
    save_cookie_cache(DEFAULT_COOKIE_FILE, cookie)
    print(f"[抓取] 已保存本地缓存: {DEFAULT_COOKIE_FILE}")

    if not health["has_cred"]:
        print("[抓取][WARN] 仍然没有 udb_cred —— 说明该账号当前不是「长期登录态」，"
              "请在浏览器里勾选「记住登录」，或先退出再重新登录一次")
        return

    if not gh_secret.get_token()[0]:
        print("[抓取] 未配置 GH_PAT，只写本地缓存；需要写回 Secret 时设置 GH_PAT/GH_REPO 再跑一次")
        print(cookie)
        return

    name = "HUYA_COOKIES" if os.getenv("HUYA_COOKIES", "").strip() else "HUYA_COOKIE"
    report = gh_secret.sync({name: cookie}, log=print)
    print(f"[抓取] 写回结果: {report}")


if __name__ == "__main__":
    main()
