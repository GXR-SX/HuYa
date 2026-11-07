#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""打开直播间页面，触发每日粉丝礼包。"""

import os

from huya_cookie import parse_cookie

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")


def visit_rooms(cookie, rooms, tag="", log=print):
    """用无头浏览器依次访问房间页；返回是否至少成功访问一个房间。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log(f"{tag}[WARN] 未安装 playwright，跳过房间访问")
        return False

    cookies = [
        {"name": k, "value": v, "domain": ".huya.com", "path": "/"}
        for k, v in parse_cookie(cookie).items() if k and v
    ]
    if not cookies:
        log(f"{tag}[WARN] Cookie 为空，跳过房间访问")
        return False

    wait_ms = int(os.getenv("HUYA_ROOM_WAIT_MS", "10000") or 10000)
    goto_timeout = int(os.getenv("HUYA_ROOM_TIMEOUT_MS", "20000") or 20000)
    chrome_path = os.getenv("HUYA_CHROME_PATH", "").strip()
    launch_args = {
        "headless": True,
        "args": ["--no-sandbox", "--disable-dev-shm-usage"],
    }
    if chrome_path:
        launch_args["executable_path"] = chrome_path

    ok = False
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(**launch_args)
            try:
                context = browser.new_context(
                    user_agent=USER_AGENT,
                    viewport={"width": 1440, "height": 900},
                )
                try:
                    context.add_cookies(cookies)
                except Exception as e:
                    log(f"{tag}[WARN] 写入 Cookie 失败: {type(e).__name__}: {e}")
                    return False

                for rid in rooms:
                    url = f"https://www.huya.com/{rid}"
                    # 直播间页面资源很重，等 domcontentloaded 经常直接超时；
                    # 用 commit（导航一提交就返回）再驻留，既快又不丢粉丝礼包
                    for attempt in (1, 2):
                        page = context.new_page()
                        try:
                            page.goto(url, wait_until="commit", timeout=goto_timeout)
                            page.wait_for_timeout(wait_ms)
                            log(f"{tag}[ROOM] 已访问房间 {rid}")
                            ok = True
                            break
                        except Exception as e:
                            log(f"{tag}[WARN] 房间 {rid} 访问失败(第 {attempt} 次): "
                                f"{type(e).__name__}: {e}")
                        finally:
                            page.close()
            finally:
                browser.close()
    except Exception as e:
        log(f"{tag}[WARN] 房间访问失败: {type(e).__name__}: {e}")
        return False

    return ok
