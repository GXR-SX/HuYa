#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cookie 体检：只打印字段名、长度、到期时间，绝不打印字段值。

用法：
    HUYA_COOKIE='a=1; b=2' python tools/cookie_doctor.py
    HUYA_COOKIES=$'cookie1\ncookie2' python tools/cookie_doctor.py
    python tools/cookie_doctor.py .huya_cookie/cookie.txt
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gh_secret  # noqa: E402
from huya_cookie import cookie_health, describe_cookie  # noqa: E402


def report(raw, tag):
    if not raw:
        print(f"{tag} 空 Cookie")
        return
    health = describe_cookie(raw, tag=tag)
    missing = [k for k in ("udb_uid", "udb_biztoken", "udb_cred") if not health["fields"].get(k)]
    print(f"{tag} 结论: {'可直接续期' if health['has_cred'] else '无法续期'}"
          f"{'，缺 ' + '/'.join(missing) if missing else ''}")


def main():
    if len(sys.argv) > 1 and os.path.isfile(sys.argv[1]):
        with open(sys.argv[1], encoding="utf-8") as f:
            report(f.read().strip(), f"[{os.path.basename(sys.argv[1])}]")
    else:
        multi = os.getenv("HUYA_COOKIES", "").strip()
        if multi:
            for i, line in enumerate([l.strip() for l in multi.splitlines() if l.strip()], 1):
                report(line, f"[账号{i}]")
        else:
            report(os.getenv("HUYA_COOKIE", "").strip(), "[HUYA_COOKIE]")

    token, env_name = gh_secret.get_token()
    repo = os.getenv("GH_REPO") or os.getenv("GITHUB_REPOSITORY") or ""
    print(f"[环境] 令牌: {env_name or '未配置'}（长度 {len(token)}），"
          f"仓库: {repo or '未配置'}，PyNaCl: {'有' if gh_secret.public else '无'}")


if __name__ == "__main__":
    main()
