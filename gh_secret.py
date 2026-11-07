#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把刷新后的虎牙 Cookie 写回 GitHub Actions Secrets。

GitHub 的 Secret 是「只写」的：读取接口永远只返回名字，写入时必须先用仓库公钥
做 libsodium sealed box 加密，再把密文 PUT 回去（网页端行为一致）。本模块就是
把这套流程封装成 sync()。

令牌来源优先级：GH_PAT → HUYA_GH_PAT → GH_TOKEN → GITHUB_PAT → GITHUB_TOKEN
（Actions 自带的 GITHUB_TOKEN 没有 secrets 写权限，必须用个人令牌）

用法：
    GH_PAT=xxx GH_REPO=owner/repo python -c \
      "import gh_secret; print(gh_secret.sync({'HUYA_COOKIE': 'a=1; b=2'}))"
"""

import base64
import os
import time

import requests

try:  # PyNaCl 只在真正写入时需要，导入失败时延迟报错
    from nacl import public
except ImportError:  # pragma: no cover
    public = None

API = os.getenv("GITHUB_API_URL", "https://api.github.com").rstrip("/")
TOKEN_ENVS = ("GH_PAT", "HUYA_GH_PAT", "GH_TOKEN", "GITHUB_PAT", "GITHUB_TOKEN")
MAX_SECRET_BYTES = 48 * 1024  # GitHub 单个 Secret 上限 48KB


class SecretError(Exception):
    pass


def get_token():
    """返回 (令牌, 环境变量名)。"""
    for name in TOKEN_ENVS:
        value = os.getenv(name, "").strip()
        if value:
            return value, name
    return "", ""


def get_repo():
    repo = (os.getenv("GH_REPO") or os.getenv("GITHUB_REPOSITORY") or "").strip()
    if "/" not in repo:
        raise SecretError("未设置 GH_REPO / GITHUB_REPOSITORY（格式 owner/repo）")
    return repo


def _session(token):
    session = requests.Session()
    session.headers.update({
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "huya-cookie-sync/1.1",
    })
    return session


def _request(session, method, url, log=print, **kwargs):
    """带重试的请求；4xx（除 429）直接返回，交给调用方判断。"""
    last = ""
    for attempt in range(3):
        try:
            resp = session.request(method, url, timeout=20, **kwargs)
        except Exception as exc:  # 网络类异常
            last = f"{type(exc).__name__}: {exc}"
        else:
            if resp.status_code < 500 and resp.status_code != 429:
                return resp
            last = f"HTTP {resp.status_code}: {resp.text[:200]}"
        if attempt < 2:
            wait = 2 * (attempt + 1)
            log(f"[SECRET] {method} {url} 失败（{last}），{wait}s 后重试")
            time.sleep(wait)
    raise SecretError(f"{method} {url} 连续失败: {last}")


def get_public_key(repo, session, log=print):
    resp = _request(session, "GET",
                    f"{API}/repos/{repo}/actions/secrets/public-key", log=log)
    if resp.status_code != 200:
        raise SecretError(f"获取仓库公钥失败 HTTP {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    return data["key_id"], data["key"]


def encrypt(public_key_b64, value):
    if public is None:
        raise SecretError("缺少 PyNaCl，请先 pip install PyNaCl")
    pk = public.PublicKey(base64.b64decode(public_key_b64))
    sealed = public.SealedBox(pk).encrypt(value.encode("utf-8"))
    return base64.b64encode(sealed).decode("ascii")


def put_secret(repo, name, value, session, key_id=None, key=None, log=print):
    if key_id is None or key is None:
        key_id, key = get_public_key(repo, session, log=log)
    payload = {"encrypted_value": encrypt(key, value), "key_id": key_id}
    resp = _request(session, "PUT", f"{API}/repos/{repo}/actions/secrets/{name}",
                    json=payload, log=log)
    if resp.status_code not in (201, 204):
        raise SecretError(f"写入 {name} 失败 HTTP {resp.status_code}: {resp.text[:300]}")
    return True


def list_secret_names(repo, session, log=print):
    resp = _request(session, "GET",
                    f"{API}/repos/{repo}/actions/secrets?per_page=100", log=log)
    if resp.status_code != 200:
        log(f"[SECRET] 读取 Secret 列表失败 HTTP {resp.status_code}")
        return []
    return [item["name"] for item in resp.json().get("secrets", [])]


def sync(updates, log=print, dry_run=False):
    """updates: {Secret 名: 明文值}；返回结构化报告，不抛异常。"""
    report = {"ok": False, "updated": [], "failed": {}, "skipped": [], "dry_run": dry_run}

    token, token_env = get_token()
    if not token:
        report["error"] = f"没有可用令牌（{'/'.join(TOKEN_ENVS)}）"
        log(f"[SECRET] {report['error']}")
        return report
    try:
        repo = get_repo()
    except SecretError as exc:
        report["error"] = str(exc)
        log(f"[SECRET] {exc}")
        return report

    payload = {}
    for name, value in (updates or {}).items():
        value = (value or "").strip()
        if not value:
            report["skipped"].append(name)
            continue
        if len(value.encode("utf-8")) > MAX_SECRET_BYTES:
            report["failed"][name] = "超过 48KB 上限"
            log(f"[SECRET] {name} 超过 48KB 上限，跳过")
            continue
        payload[name] = value

    if not payload:
        report["error"] = "没有需要写入的内容"
        return report

    if dry_run:
        for name, value in payload.items():
            log(f"[SECRET] DRY-RUN 不写入 {name}（{len(value)} 字节，令牌来源 {token_env}）")
            report["updated"].append(name)
        report["ok"] = True
        return report

    session = _session(token)
    try:
        key_id, key = get_public_key(repo, session, log=log)
    except SecretError as exc:
        report["error"] = str(exc)
        log(f"[SECRET] {exc}")
        return report

    for name, value in payload.items():
        try:
            put_secret(repo, name, value, session, key_id=key_id, key=key, log=log)
        except SecretError as exc:
            report["failed"][name] = str(exc)
            log(f"[SECRET] 写入 {name} 失败: {exc}")
            continue
        log(f"[SECRET] 已写入 {repo} → {name}（{len(value)} 字节，令牌来源 {token_env}）")
        report["updated"].append(name)

    report["ok"] = bool(report["updated"]) and not report["failed"]
    return report


if __name__ == "__main__":  # 自检：只检查令牌/仓库/权限，不写任何值
    import json

    token, env_name = get_token()
    info = {
        "token_env": env_name or None,
        "token_len": len(token),
        "repo": os.getenv("GH_REPO") or os.getenv("GITHUB_REPOSITORY") or None,
        "pynacl": public is not None,
    }
    if token and info["repo"]:
        session = _session(token)
        try:
            names = list_secret_names(get_repo(), session)
            info["secrets"] = names
            info["can_read"] = bool(names) or True
        except SecretError as exc:
            info["error"] = str(exc)
    print(json.dumps(info, ensure_ascii=False, indent=2))
