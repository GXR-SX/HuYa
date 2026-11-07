#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""虎牙 UDB 登录态静默续期（复刻网页端 udb.sdk.web.js 的 credLogin）。

为什么导出的 Cookie 会「过期」，而浏览器永远不会掉登录？
--------------------------------------------------------
虎牙登录态由两个 udb Cookie 组成：

  udb_biztoken  业务令牌，网页/接口真正用来判断登录的凭证，寿命约 15 天（部分场景 7 天）
  udb_cred      长期凭证（refresh credential），寿命约 180 天，HttpOnly

浏览器并不是「Cookie 永不过期」，而是每次访问时前端 SDK
(https://a.msstatic.com/huya/hd/web/udb_login/lib/udb.sdk.web.js) 会检测到
udb_cred 还在、而 udb_biztoken / udb_uid 缺失，于是静默 POST
  https://udblgn.huya.com/web/credLogin
用 udb_cred 换一份全新的 udb_biztoken（服务端通过 Set-Cookie 下发），同时把
udb_cred 本身也滚动成一张新的凭证。也就是说这是一个「滑动续期」：
只要你 180 天内访问过一次网页，登录态就永远续下去。

关键点：**每一次续期都会让上一份 udb_cred 作废**。所以续期后的 Cookie 必须
立刻持久化（本仓库的做法：写回 GitHub Actions Secret），否则下一次运行拿到的
还是旧快照，直接 40002 登录失败 —— 这就是「新 Cookie 只能活很短」的真正原因。

SDK 的原始触发条件（本文件完全复刻）：
    if (udb_cred && (!udb_uid || !udb_biztoken)) -> credLogin(uri=70017)
"""

import base64
import hashlib
import json
import os
import sys
import time
from urllib.parse import quote

import requests

DEFAULT_COOKIE_ENV = "HUYA_COOKIE"
DEFAULT_COOKIE_FILE = ".huya_cookie/cookie.txt"

# 缓存里绝不能放明文 Cookie：公开仓库的 Actions 缓存，任何人开一个 PR 就能用
# actions/cache/restore 把 base 分支的缓存读出来（GitHub 官方文档明确警告）。
# 所以缓存落盘前先用这个密钥做 SecretBox 加密，密钥只存在 Secrets 里。
CACHE_KEY_ENV = "HUYA_COOKIE_BACKUP_KEY"

# 复刻自 udb.sdk.web.js 中 credLogin() 的默认参数（已对照线上 SDK 校验）
CRED_LOGIN_URL = "https://udblgn.huya.com/web/credLogin"
UDB_APPID = "5002"
UDB_APPKEY = "bfd434f70d40433c6b57a781dbcee91a"
UDB_SDK_VERSION = "2.6"

DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# 登录态里真正决定「还能活多久」的字段
KEY_FIELDS = ("udb_uid", "udb_biztoken", "udb_cred", "udb_login", "udb_guiddata")


def parse_cookie(cookie_str):
    """Cookie 字符串 -> dict。值里可能含 '='，所以只按第一个 '=' 切分。"""
    out = {}
    for part in (cookie_str or "").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def _cache_key():
    raw = os.getenv(CACHE_KEY_ENV, "").strip()
    if not raw:
        return None
    return hashlib.sha256(raw.encode("utf-8")).digest()


def encrypt_cookie(cookie):
    """有密钥就返回密文，没有就返回 None（此时不落盘，避免明文进缓存）。"""
    key = _cache_key()
    if not key:
        return None
    from nacl.secret import SecretBox
    return base64.b64encode(SecretBox(key).encrypt(cookie.encode("utf-8"))).decode("ascii")


def decrypt_cookie(blob):
    key = _cache_key()
    if not key:
        raise RuntimeError(f"缺少 {CACHE_KEY_ENV}，无法解密缓存")
    from nacl.secret import SecretBox
    return SecretBox(key).decrypt(base64.b64decode((blob or "").strip())).decode("utf-8")


def load_cookie_cache(path, log=print):
    """读取缓存：优先 .enc（加密），兼容旧的明文文件。"""
    if not path:
        return ""
    enc = f"{path}.enc"
    if os.path.exists(enc):
        try:
            with open(enc, encoding="utf-8") as f:
                text = decrypt_cookie(f.read()).strip()
            log(f"[COOKIE] 已解密缓存 {os.path.basename(enc)}（{len(text)} 字节）")
            return text
        except Exception as e:
            log(f"[COOKIE] 解密缓存失败: {type(e).__name__}: {e}")
    if os.path.exists(path):
        try:
            return open(path, encoding="utf-8").read().strip()
        except OSError as e:
            log(f"[COOKIE] 读取缓存文件失败: {type(e).__name__}: {e}")
    return ""


def load_cookie(log=print):
    """载入 Cookie：返回 (环境变量值, 缓存值, 缓存文件路径)。"""
    cookie_env = os.getenv(DEFAULT_COOKIE_ENV, "").strip()
    cookie_cache = load_cookie_cache(DEFAULT_COOKIE_FILE, log=log)
    return (cookie_env, cookie_cache, DEFAULT_COOKIE_FILE)


def drop_cookie_cache(path, log=print):
    """丢弃缓存文件（明文与密文一起删）。"""
    if not path:
        return False
    removed = False
    for target in (path, f"{path}.enc"):
        try:
            os.remove(target)
            removed = True
        except FileNotFoundError:
            continue
        except OSError as e:
            log(f"[WARN] 删除缓存文件失败: {type(e).__name__}: {e}")
    return removed


def save_cookie_cache(path, cookie):
    """保存缓存：有密钥就只写密文 .enc，并删掉可能残留的明文。"""
    if not path:
        return None
    path = os.path.abspath(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)

    blob = encrypt_cookie(cookie)
    if blob is None and os.getenv("GITHUB_ACTIONS") == "true":
        # CI 里没配密钥就不落盘：宁可不缓存，也不能把明文塞进 Actions 缓存
        return None
    target = f"{path}.enc" if blob else path
    payload = blob if blob else cookie

    tmp = f"{target}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(payload)
    os.replace(tmp, target)
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass

    if blob:  # 加密模式：清掉旧明文
        try:
            os.remove(path)
        except OSError:
            pass
    return target



def format_cookie(cookies):
    return ";".join(f"{k}={v}" for k, v in cookies.items() if v != "")


def _jwt_payload(value):
    """udb_biztoken 若是 JWT 就解出 payload，用于打印真实到期时间。"""
    parts = (value or "").split(".")
    if len(parts) != 3:
        return None
    try:
        seg = parts[1] + "=" * (-len(parts[1]) % 4)
        data = json.loads(base64.urlsafe_b64decode(seg.encode()))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def cookie_health(cookie_str, now=None):
    """体检 Cookie：只输出字段名与长度，绝不输出值。"""
    cookies = parse_cookie(cookie_str)
    now = time.time() if now is None else now
    health = {
        "count": len(cookies),
        "bytes": len((cookie_str or "").encode("utf-8")),
        "fields": {k: len(v) for k, v in cookies.items()},
        "has_uid": bool(cookies.get("udb_uid")),
        "has_cred": bool(cookies.get("udb_cred")),
        "has_biztoken": bool(cookies.get("udb_biztoken")),
        "exp": {},
        "notes": [],
    }

    for name in ("udb_biztoken", "udb_cred"):
        payload = _jwt_payload(cookies.get(name, ""))
        if not payload:
            continue
        exp = payload.get("exp")
        if isinstance(exp, (int, float)) and exp > 0:
            health["exp"][name] = {
                "exp": int(exp),
                "left_days": round((exp - now) / 86400, 2),
            }

    if not health["has_cred"]:
        health["notes"].append(
            "缺少 udb_cred（HttpOnly）：脚本无法静默续期，Cookie 到期即失效。"
            "请在 DevTools → Application → Cookies → https://www.huya.com 里导出完整 Cookie，"
            "或运行 tools/capture_cookie.py 抓取")
    if not health["has_biztoken"]:
        health["notes"].append("缺少 udb_biztoken：需要一次 credLogin 才能拿到新令牌")
    if not health["has_uid"]:
        health["notes"].append("缺少 udb_uid：无法确认账号身份")
    if health["bytes"] > 3800:
        health["notes"].append(
            f"Cookie 长度 {health['bytes']} 字节偏大，多数服务器/代理的请求头上限是 4KB，可能被截断")

    return health


def describe_cookie(cookie_str, tag="", log=print, now=None):
    """把体检结果打印成人话。"""
    health = cookie_health(cookie_str, now=now)
    fields = ", ".join(f"{k}({v})" for k, v in health["fields"].items()) or "空"
    log(f"{tag}[COOKIE] 共 {health['count']} 个字段 / {health['bytes']} 字节: {fields}")
    for name, exp in health["exp"].items():
        log(f"{tag}[COOKIE] {name} 剩余 {exp['left_days']} 天到期")
    if health["has_cred"]:
        log(f"{tag}[COOKIE] udb_cred 存在，可静默续期")
    for note in health["notes"]:
        log(f"{tag}[COOKIE][WARN] {note}")
    return health


def _app_sign(appid=UDB_APPID, version=UDB_SDK_VERSION, appkey=UDB_APPKEY):
    # SDK 里的 r() 就是一份标准 MD5 实现
    return hashlib.md5(f"{appid}_{version}_{appkey}".encode()).hexdigest()


def cred_login(cookies, timeout=20, page="https://www.huya.com/", drop_token=False):
    """用 udb_cred 换新令牌，返回 (响应 JSON, 合并后的 cookie dict)。

    drop_token=True 时按网页端逻辑先去掉 udb_biztoken / udb_uid，
    让服务端一定重新下发一套（对应 SDK 的触发条件）。
    """
    send = {k: v for k, v in (cookies or {}).items() if v}
    if drop_token:
        send.pop("udb_biztoken", None)
        send.pop("udb_uid", None)

    session = requests.Session()
    session.headers.update({
        "User-Agent": DEFAULT_UA,
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://www.huya.com",
        "Referer": "https://www.huya.com/",
    })
    for k, v in send.items():
        session.cookies.set(k, v, domain=".huya.com", path="/")

    body = {
        "uri": "70017",
        "version": UDB_SDK_VERSION,
        "context": "1",
        "appId": UDB_APPID,
        "appSign": _app_sign(),
        "lcid": "2052",
        "data": {"page": quote(page, safe="")},
    }
    resp = session.post(CRED_LOGIN_URL, data=json.dumps(body), timeout=timeout)
    resp.raise_for_status()
    payload = resp.json()

    merged = dict(cookies)
    # 服务端 Set-Cookie 才是权威结果（HttpOnly 的 udb_cred 也只在这里出现）
    for c in session.cookies:
        if c.domain and "huya.com" in c.domain:
            merged[c.name] = c.value
    for c in resp.cookies:
        if c.name:
            merged[c.name] = c.value
    return payload, merged


def refresh_cookie(cookie_str, log=print, attempts=2):
    """续期 Cookie。

    返回 (cookie_str, info)。续期失败时原样返回，并在日志里给出原因，
    这样即使虎牙改了协议，脚本也只是退化成「旧行为」，不会直接崩。
    """
    cookies = parse_cookie(cookie_str)
    if not cookies.get("udb_cred"):
        log("[COOKIE] 未发现 udb_cred，跳过续期（HttpOnly 凭证没导出，见 readme 的导出步骤）")
        return cookie_str, None

    payload = merged = None
    for attempt in range(1, attempts + 1):
        drop = attempt > 1
        try:
            payload, merged = cred_login(cookies, drop_token=drop)
        except Exception as e:
            log(f"[COOKIE] 续期请求失败（第 {attempt} 次）: {type(e).__name__}: {e}")
            payload = None
            continue

        if payload.get("returnCode") == 0:
            break
        log(f"[COOKIE] 第 {attempt} 次续期被拒绝 returnCode={payload.get('returnCode')} "
            f"message={payload.get('message')}")
        if not drop:
            log("[COOKIE] 按网页端逻辑（先摘掉 udb_biztoken/udb_uid）再试一次")

    if not payload or payload.get("returnCode") != 0:
        log("[COOKIE] 续期失败，Cookie 可能已失效，请重新导出")
        return cookie_str, None

    login_data = (payload.get("data") or {}).get("loginData") or {}
    uid = login_data.get("uid") or merged.get("udb_uid")
    if not uid:
        log("[COOKIE] 续期响应缺少 uid，按失败处理")
        return cookie_str, None

    changed = format_cookie(merged) != format_cookie(cookies)
    info = {
        "uid": uid,
        "passport": login_data.get("passport"),
        "biztoken_changed": merged.get("udb_biztoken") != cookies.get("udb_biztoken"),
        "cred_changed": merged.get("udb_cred") != cookies.get("udb_cred"),
        "changed": changed,
        "has_cred": bool(merged.get("udb_cred")),
    }
    log(f"[COOKIE] 续期成功 "
        f"(biztoken {'已更新' if info['biztoken_changed'] else '未变'}, "
        f"cred {'已更新' if info['cred_changed'] else '未变'}, "
        f"uid={uid})")
    if not info["has_cred"]:
        log("[COOKIE][WARN] 续期后依然没有 udb_cred，下次到期就只能重新导出")
    return format_cookie(merged), info


if __name__ == "__main__":
    # 直接运行本文件 = 用 cookie 做一次续期自检
    # 用法: python huya_cookie.py [cookie_file]
    #   若 cookie_file 存在则从其读取，否则回退到环境变量 HUYA_COOKIE
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_COOKIE_FILE

    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read().strip()
    else:
        raw = os.getenv(DEFAULT_COOKIE_ENV, "").strip()

    if not raw:
        print(f"用法: python {sys.argv[0]} [cookie_file]")
        print(f"  或:  {DEFAULT_COOKIE_ENV}='...' python {sys.argv[0]}")
        raise SystemExit(1)

    describe_cookie(raw, tag="[自检]")
    new_cookie, info = refresh_cookie(raw)
    if info:
        parsed = parse_cookie(new_cookie)
        print("\n续期后的 Cookie：")
        print(new_cookie)
        print(f"\n(uid={info['uid']}, 共 {len(parsed)} 个字段)")

        # 保存为 {path_name}_refresh.txt
        base, _ = os.path.splitext(path)
        refresh_path = f"{base}_refresh.txt"
        save_cookie_cache(refresh_path, new_cookie)
        print(f"\n已保存到: {refresh_path}")
    else:
        print("续期失败")
        raise SystemExit(2)
