#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import os
import random
import time

import requests

import gh_secret
from huya_api import HuyaAPI
from huya_cookie import (describe_cookie, load_cookie, load_cookie_cache, parse_cookie,
                         refresh_cookie, save_cookie_cache)

COOKIE_DIR = ".huya_cookie"
SYNC_REPORT = os.path.join(COOKIE_DIR, "sync.json")


def _pause(a, b):
    time.sleep(random.uniform(a, b))


def _env_flag(name, default="1"):
    return os.getenv(name, default).strip() not in ("0", "", "false", "False")


class SecretSync:
    """一次运行内同一个值只写一次，避免重复 PUT。"""

    def __init__(self, log=print):
        self.log = log
        self.done = {}
        self.report = {"updated": [], "failed": {}, "skipped": [], "errors": []}
        self.enabled = _env_flag("HUYA_COOKIE_SYNC", "1") and bool(gh_secret.get_token()[0])
        if not self.enabled:
            token, _ = gh_secret.get_token()
            reason = "HUYA_COOKIE_SYNC=0" if not _env_flag("HUYA_COOKIE_SYNC", "1") else "未配置 GH_PAT"
            self.log(f"[SECRET] 跳过回写（{reason}）")

    def push(self, updates):
        updates = {k: (v or "").strip() for k, v in (updates or {}).items()}
        updates = {k: v for k, v in updates.items() if v and self.done.get(k) != v}
        if not updates:
            return True
        if not self.enabled:
            self.report["skipped"].extend(updates)
            return False
        report = gh_secret.sync(updates, log=self.log,
                                dry_run=os.getenv("GH_SECRET_DRY_RUN", "").strip() == "1")
        for name in report.get("updated", []):
            self.done[name] = updates.get(name, "")
            if name not in self.report["updated"]:
                self.report["updated"].append(name)
        for name, err in (report.get("failed") or {}).items():
            self.report["failed"][name] = err
        if report.get("error"):
            self.report["errors"].append(report["error"])
        return report.get("ok", False)

    def dump(self):
        try:
            os.makedirs(COOKIE_DIR, exist_ok=True)
            with open(SYNC_REPORT, "w", encoding="utf-8") as f:
                json.dump(self.report, f, ensure_ascii=False, indent=2)
        except OSError as e:
            self.log(f"[SECRET][WARN] 写同步报告失败: {type(e).__name__}: {e}")


class AccountState:
    """所有账号的「当前最优 Cookie」，用于拼 HUYA_COOKIES / HUYA_COOKIE。"""

    def __init__(self, accounts, sync):
        self.sync = sync
        self.multi = bool(os.getenv("HUYA_COOKIES", "").strip())
        self.items = [{"label": a["label"], "cookie": a["seed"]} for a in accounts]

    def update(self, index, cookie):
        if cookie:
            self.items[index]["cookie"] = cookie

    def secrets(self):
        if self.multi:
            return {"HUYA_COOKIES": "\n".join(i["cookie"] for i in self.items if i["cookie"])}
        return {"HUYA_COOKIE": self.items[0]["cookie"] if self.items else ""}

    def push(self):
        return self.sync.push(self.secrets())


def build_accounts():
    """多账号用 HUYA_COOKIES（每行一个 Cookie），单账号沿用 HUYA_COOKIE 或缓存文件"""
    raw = os.getenv("HUYA_COOKIES", "").strip()
    if raw:
        rows = [line.strip() for line in raw.splitlines() if line.strip()]
        return [{"label": f"账号{i}", "seed": cookie,
                 "path": os.path.join(COOKIE_DIR, f"cookie_{i}.txt")}
                for i, cookie in enumerate(rows, 1)]

    seed, cache, path = load_cookie()
    return [{"label": "", "seed": seed, "path": path}]


def parse_rooms(text):
    return [int(s) for s in (text or "").replace(" ", "").split(",") if s.isdigit()]


class HuYaAuto:
    def __init__(self, account, rooms, dry_run=False, claim_fans_box=True,
                 visit_rooms=True, state=None, index=0, refresh_only=False):
        self.label = account["label"]
        self.tag = f"[{self.label}] " if self.label else ""
        self.cookie_seed = account["seed"]
        self.cookie_path = account["path"]
        self.rooms = rooms
        self.dry_run = dry_run
        self.enable_fans_box = claim_fans_box
        self.enable_room_visit = visit_rooms
        self.refresh_only = refresh_only
        self.state = state
        self.index = index
        self.msg_logs = []
        self.passport = ""
        self.api = None
        self.login_ok = False
        self.cookie_source = "env"
        self.cookie_cache = ""
        self.cookie = ""
        self.health = None

        if self.cookie_path:
            self.cookie_cache = load_cookie_cache(self.cookie_path, log=print)
            if self.cookie_cache:
                # 账号顺序变了的话缓存可能对不上，按 udb_uid 校验一下
                cache_uid = parse_cookie(self.cookie_cache).get("udb_uid")
                seed_uid = parse_cookie(self.cookie_seed or "").get("udb_uid")
                if cache_uid and seed_uid and cache_uid != seed_uid:
                    print(f"{self.tag}[WARN] 缓存的账号({cache_uid})与当前 Cookie({seed_uid})"
                          f"不一致，改用当前 Cookie")
                    self.cookie_cache = ""
                else:
                    self.cookie = self.cookie_cache
                    self.cookie_source = "file"

        if not self.cookie:
            self.cookie = self.cookie_seed

        # 缓存可能是旧版本写下的「没有 udb_cred」的快照，而 Secret 里有 —— 别用坏的
        if self.cookie and self.cookie_seed and self.cookie_source == "file":
            if (parse_cookie(self.cookie_seed).get("udb_cred")
                    and not parse_cookie(self.cookie).get("udb_cred")):
                print(f"{self.tag}[COOKIE] 缓存缺少 udb_cred，改用 Secret 里的 Cookie")
                self.cookie = self.cookie_seed
                self.cookie_source = "env"

    # ---------------- Cookie ----------------

    def persist_cookie(self):
        """续期是滑动的：拿到新 Cookie 立刻落盘 + 写回 Secret，晚一步就可能作废。"""
        if self.cookie_path:
            try:
                # 旧值留一份：万一新链在写回 Secret 前就断了，还有东西可试
                old = load_cookie_cache(self.cookie_path, log=print)
                if old and old != self.cookie:
                    save_cookie_cache(f"{self.cookie_path}.prev", old)
                save_cookie_cache(self.cookie_path, self.cookie)
                print(f"{self.tag}[COOKIE] 登录态已写入缓存: {self.cookie_path}")
            except Exception as e:
                print(f"{self.tag}[WARN] 写缓存失败: {type(e).__name__}: {e}")

        if self.state is not None:
            self.state.update(self.index, self.cookie)
            if self.state.push():
                print(f"{self.tag}[COOKIE] 已写回 GitHub Secret")

    def _candidates(self):
        """按可信度排序的候选 Cookie：当前 → 另一份(env/缓存) → 上一版。"""
        cands = [(self.cookie, self.cookie_source)]
        if self.cookie_source == "file" and self.cookie_seed:
            cands.append((self.cookie_seed, "env"))
        elif self.cookie_source == "env" and self.cookie_cache:
            cands.append((self.cookie_cache, "file"))

        if self.cookie_path:
            prev = load_cookie_cache(f"{self.cookie_path}.prev", log=print)
            if prev:
                cands.append((prev, "上一版"))

        out, seen = [], set()
        for cookie, source in cands:
            if cookie and cookie not in seen:
                seen.add(cookie)
                out.append((cookie, source))
        return out

    def _refresh_cookie(self):
        for idx, (cookie, source) in enumerate(self._candidates()):
            if idx:
                print(f"{self.tag}[COOKIE] 改用 {source} Cookie 重试续期")
            new_cookie, info = refresh_cookie(cookie, log=print)
            if info:
                self.passport = info.get("passport") or self.passport
                if new_cookie != self.cookie:
                    self.cookie = new_cookie
                    self.cookie_source = source
                return self.cookie
        return self.cookie

    # ---------------- 流程 ----------------

    def login(self):
        print(f"{self.tag}[LOGIN] 正在登录...")
        self.health = describe_cookie(self.cookie, tag=self.tag)
        old_cookie = self.cookie
        self.cookie = self._refresh_cookie()

        # 只要续期产生了新值就立刻持久化（cred 是滚动失效的）
        if self.cookie != old_cookie:
            self.persist_cookie()
            describe_cookie(self.cookie, tag=f"{self.tag}[续期后]")

        self.api = HuyaAPI(self.cookie, passport=self.passport)
        if self.api.uid <= 0:
            print(f"{self.tag}[ERROR] Cookie 里没有 udb_uid，无法登录")
            return False

        for i in range(2):
            if self.api.verify_login():
                print(f"{self.tag}[SUCCESS] 登录成功 (uid={self.api.uid})")
                if not self.api.passport:
                    print(f"{self.tag}[WARN] 未取到 passport，送礼可能失败")
                # 即使 Cookie 没变也写一次：保证 Secret 与本地缓存/实际使用的一致
                self.persist_cookie()
                self.login_ok = True
                _pause(0.5, 1.5)
                return True
            if i == 0:
                print(f"{self.tag}[WARN] 登录校验失败，重试一次")
                time.sleep(2)

        print(f"{self.tag}[ERROR] 登录失败，Cookie 可能已失效")
        return False

    def visit_rooms(self):
        """打开各房间页，触发每日粉丝礼包。"""
        if not self.enable_room_visit:
            return
        from room_visit import visit_rooms
        visit_rooms(self.cookie, self.rooms, tag=self.tag, log=print)

    def claim_fans_boxes(self):
        """领取每个主播的固定 10 粉丝盒。"""
        state_text = {1: "未达到", 2: "计时中", 3: "可领取", 4: "今日已领取"}
        seen = set()
        logs = []
        for rid in self.rooms:
            try:
                lp, _ = self.api.room_lp_gid(rid)
            except Exception as e:
                print(f"{self.tag}[WARN] 房间 {rid} 获取主播 uid 失败: "
                      f"{type(e).__name__}: {e}")
                continue
            if not lp or lp in seen:
                continue
            seen.add(lp)

            try:
                st = self.api.fans_box_state(int(lp))
                if st.get("bcode", 0) != 0:
                    msg = f"主播 {lp} 状态查询失败 bcode={st.get('bcode')}"
                    print(f"{self.tag}[WARN] {msg}")
                    logs.append(msg)
                    continue

                state = st.get("state")
                if state == 4:
                    msg = f"主播 {lp} 今日已领"
                    print(f"{self.tag}[FANSBOX] {msg}")
                    logs.append(msg)
                    continue
                if state != 3:
                    msg = f"主播 {lp} 暂不可领({state_text.get(state, state)})"
                    print(f"{self.tag}[FANSBOX] {msg}")
                    logs.append(msg)
                    continue

                res = self.api.claim_fans_box(int(lp))
                if res.get("ok"):
                    msg = f"主播 {lp} 领取 +{res.get('count', 0)}"
                    print(f"{self.tag}[FANSBOX] {msg}")
                elif res.get("iCode") == 5:
                    msg = f"主播 {lp} 领取被拒绝 iCode=5"
                    print(f"{self.tag}[FANSBOX] {msg}")
                else:
                    msg = (f"主播 {lp} 领取失败 iCode={res.get('iCode')} "
                           f"{res.get('sMsg') or ''}").strip()
                    print(f"{self.tag}[WARN] {msg}")
                logs.append(msg)
            except Exception as e:
                msg = f"主播 {lp} 粉丝盒异常: {type(e).__name__}: {e}"
                print(f"{self.tag}[WARN] {msg}")
                logs.append(msg)
            _pause(0.5, 1.2)

        if logs:
            self.msg_logs.append("粉丝盒: " + "；".join(logs))

    def get_hl_count(self):
        """查询失败返回 None，不要当成 0（否则会误跳过送礼）"""
        print(f"{self.tag}[SEARCH] 正在查询虎粮数量...")
        for attempt in range(3):
            try:
                count, bcode, items = self.api.hu_liang_count()
            except Exception as e:
                print(f"{self.tag}[WARN] 查询异常: {type(e).__name__}: {e}")
                bcode, count, items = -1, 0, []
            if bcode == 0:
                print(f"{self.tag}[COUNT] 背包 {len(items)} 项，虎粮 {count}")
                return count
            print(f"{self.tag}[WARN] 背包接口失败 bcode={bcode}，第 {attempt + 1} 次重试")
            _pause(2, 4)
        return None

    def send_to_room_in_situ(self, rid, count):
        if count <= 0:
            return "无粮跳过"
        try:
            lp, _ = self.api.room_lp_gid(rid)
            if not lp:
                return "❌ 获取参数失败"

            res = self.api.send_hu_liang(int(lp), count, dry_run=self.dry_run)
            if res.get("dryRun"):
                print(f"{self.tag}  [DRY] 流水号={res['sPayId']} sign={res['sSign']}")
                return f"🧪 房间 {rid} dry-run，{count} 个未发送"
            if res.get("ok"):
                try:
                    left = self.api.hu_liang_count()[0]
                except Exception:
                    left = "?"
                return f"🚀 房间 {rid} 送出虎粮 {count} 个，剩余 {left}"
            msg = res.get("sMsg") or f"iPayRespCode={res.get('iPayRespCode')}"
            return f"❌ 送礼失败: {msg}"
        except Exception as e:
            print(f"{self.tag}[ERROR] 房间 {rid} 送礼异常: {type(e).__name__}: {e}")
            return "❌ 过程异常"

    def daily_check_in(self, rid):
        try:
            lp, _ = self.api.room_lp_gid(rid)
            if not lp:
                return "❌ 打卡失败: 获取主播 uid 失败"

            res = {}
            for attempt in range(3):
                res = self.api.fans_sign(int(lp))
                if res.get("bcode", 0) == 0:
                    break
                print(f"{self.tag}  [CHECKIN] 接口失败 bcode={res.get('bcode')}，"
                      f"第 {attempt + 1} 次重试")
                _pause(2, 4)

            if res.get("bcode", 0) != 0:
                return f"❌ 打卡失败: 接口错误({res.get('bcode')})"
            if res.get("ok"):
                print(f"{self.tag}  [CHECKIN] 房间 {rid} 打卡成功")
                return "✅ 打卡成功"

            msg = (res.get("sMsg") or "").strip()
            if "重复打卡" in msg or "已打卡" in msg:
                print(f"{self.tag}  [CHECKIN] 房间 {rid} 今日已打卡")
                return "ℹ️ 已打卡"
            print(f"{self.tag}  [CHECKIN] 房间 {rid} 打卡失败: {msg}")
            return f"❌ 打卡失败: {msg or res.get('iCode')}"
        except Exception as e:
            print(f"{self.tag}[ERROR] 房间 {rid} 打卡异常: {type(e).__name__}: {e}")
            return "❌ 打卡异常"

    def run(self):
        if not self.cookie:
            print(f"{self.tag}[ERROR] 没有可用的 Cookie，跳过")
            self.msg_logs.append("没有可用的 Cookie")
            return

        if not self.login():
            self.msg_logs.append("登录失败")
            return

        if self.refresh_only:
            self.msg_logs.append("仅续期，未执行送礼/打卡")
            return

        self.visit_rooms()
        if self.enable_fans_box:
            self.claim_fans_boxes()
        total = self.get_hl_count()
        if total is None:
            self.msg_logs.append("虎粮查询失败，本次只打卡")
            total = 0
        else:
            self.msg_logs.append(f"今日虎粮总数: {total}")
            if total <= 0:
                print(f"{self.tag}[INFO] 暂无虎粮，跳过送礼，只做打卡")

        for i, rid in enumerate(self.rooms):
            _pause(1, 3)
            if total > 0:
                num = total // len(self.rooms) + (1 if i < total % len(self.rooms) else 0)
                print(f"{self.tag}>>> 房间: {rid} (目标数量: {num})")
                g_res = self.send_to_room_in_situ(rid, num)
                _pause(2, 5)
            else:
                print(f"{self.tag}>>> 房间: {rid} (无虎粮，仅打卡)")
                g_res = "无粮跳过"

            c_res = self.daily_check_in(rid)
            msg = f"{g_res}； {c_res}"
            print(f"{self.tag}结果: {msg}")
            self.msg_logs.append(msg)
            _pause(3, 8)


def send_notification(send_key, logs):
    if not send_key or not logs:
        return
    print("[PUSH] 正在发送推送...")
    try:
        res = requests.post(
            f"https://sctapi.ftqq.com/{send_key}.send",
            data={"title": "虎牙自动任务报告", "desp": "\n\n".join(logs)},
            timeout=10,
        )
        print("[PUSH] 推送成功" if res.status_code == 200
              else f"[PUSH] 推送失败，状态码 {res.status_code}")
    except Exception as e:
        print(f"[ERROR] 推送异常: {type(e).__name__}: {e}")


def main():
    mode = os.getenv("HUYA_MODE", "").strip().lower()
    refresh_only = mode in ("refresh", "keepalive", "refresh-only")
    rooms = parse_rooms(os.getenv("HUYA_ROOMS", ""))
    if not rooms and not refresh_only:
        print("[ERROR] 未设置 HUYA_ROOMS")
        raise SystemExit(1)

    accounts = build_accounts()
    dry_run = os.getenv("HUYA_SEND_DRY_RUN", "").strip() == "1"
    claim_fans_box = os.getenv("HUYA_CLAIM_FANS_BOX", "1").strip() != "0"
    visit_rooms = os.getenv("HUYA_VISIT_ROOMS", "1").strip() != "0"

    sync = SecretSync()
    state = AccountState(accounts, sync)
    print(f"[INFO] 模式={'仅续期' if refresh_only else '送礼+打卡'}，"
          f"共 {len(accounts)} 个账号，房间 {rooms}，"
          f"粉丝盒{'开启' if claim_fans_box else '关闭'}，"
          f"房间访问{'开启' if visit_rooms else '关闭'}，"
          f"Secret 回写{'开启' if sync.enabled else '关闭'}")

    logs = []
    ok_count = 0
    for idx, account in enumerate(accounts):
        bot = HuYaAuto(account, rooms, dry_run, claim_fans_box, visit_rooms,
                       state=state, index=idx, refresh_only=refresh_only)
        bot.run()
        if bot.login_ok:
            ok_count += 1
        label = account["label"]
        logs.extend(f"{label}: {m}" if label else m for m in bot.msg_logs)
        if len(accounts) > 1:
            _pause(8, 15)

    # 收尾再写一次，确保最后一张 Cookie 也进了 Secret
    state.push()
    sync.dump()

    print(f"[INFO] 登录成功 {ok_count}/{len(accounts)}，"
          f"Secret 已更新: {', '.join(sync.report['updated']) or '无'}")
    if sync.report["failed"]:
        print(f"[WARN] Secret 写入失败: {json.dumps(sync.report['failed'], ensure_ascii=False)}")

    if not refresh_only:
        send_notification(os.getenv("SEND_KEY", "").strip(), logs)

    if ok_count == 0:
        print("[ERROR] 全部账号登录失败：Cookie 已失效，需要重新导出（见 readme）")
        raise SystemExit(2)


if __name__ == "__main__":
    main()
