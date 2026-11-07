# 虎牙虎粮自动发放、直播间打卡
轻量、开箱即用的 **虎牙虎粮自动批量赠送、直播间打卡工具**, 支持 GitHub Actions, 并通过 [Server 酱](https://sct.ftqq.com/)推送赠送虎粮、打卡结果至微信.

---

## 功能

- Cookie、房间号通过 GitHub Secrets 管理
- 支持定时运行和手动触发
- 支持一个或多个账号
- 自动领取粉丝牌每日礼包和固定 10 粉丝盒
- 自动读取背包虎粮, 按房间批量分配
- 通过 GitHub Actions 计划运行
- 可选 Server 酱推送赠送和打卡结果

---

## 项目结构

```
.github/workflows/auto.yml      # GitHub Actions 配置(送礼/打卡)
.github/workflows/keepalive.yml # 每 12 小时只续期 + 回写 Secret
requirements.txt                # 依赖清单
main.py                         # 主程序
huya_cookie.py                  # Cookie 续期与体检, 可单独运行自检
gh_secret.py                    # 把新 Cookie 写回 Actions Secrets
room_visit.py                   # 房间页访问(粉丝礼包)
tools/cookie_doctor.py          # Cookie 体检(只打印字段名和长度)
tools/capture_cookie.py         # 本地抓取完整 Cookie(含 HttpOnly)
```

---

## 部署

### 1. 获取虎牙 Cookie

1. 登录 [虎牙网页版](https://i.huya.com/index.php?m=Msg&do=getMsgC)
2. 按 F12 打开开发者工具, 切到网络, 刷新页面
3. 找到 `index.php?m=Msg&do=getMsgC` 这个请求
4. 在请求头里复制完整的 Cookie 字符串, 务必确认不含换行, 且包含 `udb_biztoken` 和 `udb_cred` 字段

### 2. 获取 Server 酱 SendKey(可选)

1. 打开 [Server 酱](https://sct.ftqq.com/)
2. 注册登录, 进入「发送通道」
3. 创建一个通道, 拿到 `SendKey`

### 3. 在 GitHub 上配置

1. Fork 本仓库
2. 进入仓库的 **Settings → Secrets and variables → Actions**
3. 添加密钥:
   - `HUYA_COOKIE`: 虎牙登录 Cookie, 单账号使用
   - `HUYA_COOKIES`: 多账号使用, 每行一个 Cookie, 设了它就忽略 `HUYA_COOKIE`
   - `HUYA_ROOMS`: 房间号列表, 英文逗号分隔, 例如 `518512,1964,294636272`, 必须
   - `SEND_KEY`: Server 酱 SendKey, 可选
   - `GH_PAT`: **必填**, 用于把续期后的新 Cookie 写回 Secrets, 见下一节
4. 进入 **Actions**, 启用工作流
5. 默认每天北京时间 07:17 自动运行, 另外每 12 小时有一次纯续期

### 4. 配置 GH_PAT(让 Cookie 自己续命)

虎牙的 `udb_cred` 是**滚动失效**的: 每续期一次, 上一份立刻作废。所以脚本必须把
新 Cookie 写回 Secret, 否则下一轮读到的还是旧快照, 直接 `40002 登录失败` ——
这就是"新 Cookie 只活很短"的根源。

1. 打开 https://github.com/settings/tokens
2. **Fine-grained token**: 选择本仓库 → Repository permissions → **Secrets: Read and write**
   (或者用 Classic token, 勾 `repo` + `workflow`)
3. 复制令牌, 加到仓库 Secret `GH_PAT`

脚本只用它做一件事: `PUT /repos/{owner}/{repo}/actions/secrets/{name}`,
写入前用仓库公钥做 libsodium sealed box 加密, 和网页端行为一致。

---

## 手动运行

### 在 GitHub 上运行

1. 进入仓库的 Actions
2. 选择「虎牙虎粮自动发放」
3. 点击 `Run workflow`
4. 可以输入房间号覆盖 Secrets 里的配置

### 本地运行

```bash
pip install -r requirements.txt

export HUYA_COOKIE="你的Cookie"
export HUYA_ROOMS="518512,1964"
export SEND_KEY="你的sendkey"

python main.py
```

---

## 配置项

| 名称 | 说明 |
|------|------|
| HUYA_COOKIE | 登录凭证, 单账号使用 |
| HUYA_COOKIES | 多账号 Cookie, 每行一个, 优先于 HUYA_COOKIE |
| HUYA_ROOMS | 目标房间号列表, 必须 |
| SEND_KEY | Server 酱 SendKey, 可选 |
| GH_PAT | 写回 Secret 用的令牌, 需要 Secrets 写权限 |
| HUYA_COOKIE_SYNC | 设为 0 时关闭 Secret 回写, 默认开启 |
| GH_SECRET_DRY_RUN | 设为 1 时只演练回写, 不真正写 |
| HUYA_MODE | 设为 refresh 时只续期, 不送礼/打卡 |
| HUYA_SEND_DRY_RUN | 设为 1 时只演练流程, 不真正赠送 |
| HUYA_CLAIM_FANS_BOX | 设为 0 时关闭固定 10 粉丝盒领取, 默认开启 |

---

## 多账号

把多个 Cookie 用换行分隔写进 `HUYA_COOKIES`(GitHub Secrets 支持多行内容), 脚本会依次处理每个账号:

- 每个账号独立续期, 缓存分别存到 `.huya_cookie/cookie_1.txt`、`cookie_2.txt`…, 下次直接复用
- 账号之间的顺序可以随意调整, 缓存会和 Cookie 自动对应
- 房间号是所有账号共用的; 想给不同账号发不同房间, 就分成几次运行
- 推送会把所有账号的结果合并成一条, 每行前面标上账号

---

## 关于 Cookie 续期(重点)

虎牙登录态由两个 Cookie 组成:

| Cookie | 作用 | 寿命 |
|--------|------|------|
| `udb_biztoken` | 接口鉴权真正看的东西 | 约 15 天(部分场景 7 天) |
| `udb_cred` | 长期凭证, 用来换新 biztoken, **HttpOnly** | 约 180 天 |

网页端并不是"永不过期", 而是每次访问时前端 SDK 发现 `udb_cred` 在、`udb_biztoken` 缺失,
就静默 `POST https://udblgn.huya.com/web/credLogin` 换一套新的, 顺便把 `udb_cred` 也滚成新的。

**关键: 每次续期都会让上一份 `udb_cred` 作废。** 所以:

1. 续期后的 Cookie 必须立刻持久化 —— 本工具会写回 `HUYA_COOKIE` / `HUYA_COOKIES` 这个 Secret
   (需要 `GH_PAT`), 同时留一份 Actions 缓存兜底;
2. 只有 Secret 更新了, 下一次运行才拿得到还在有效期内的那一份;
3. 以前的版本只写缓存、不写 Secret, 手动运行还会删缓存 —— 等于每次都退回那张已经被轮换
   作废的旧快照, 表现就是"新 Cookie 只能活很短"。

另外, 缓存续期失败时会自动换另一份 Cookie 再试, 所以 `.huya_cookie` 缓存和 Secret
任何一边是好的都能救回来。

本地可以单独测试续期是否正常:

```bash
HUYA_COOKIE="你的Cookie" python huya_cookie.py
```

体检当前 Cookie(只打印字段名和长度, 不泄露值):

```bash
HUYA_COOKIE="你的Cookie" python tools/cookie_doctor.py
```

`udb_cred` 缺失时的补救办法:

- DevTools → **Application → Cookies → https://www.huya.com**, 把整张表里的
  `udb_uid` / `udb_biztoken` / `udb_cred` 都拼进 Cookie 字符串(不要用 `document.cookie`,
  它读不到 HttpOnly);
- 或者直接跑 `python tools/capture_cookie.py`: 打开真实浏览器登录一次, 脚本从
  cookie jar 里读出完整 Cookie(含 HttpOnly), 并直接写回 Secret。

---

## 定时任务

默认每天 UTC 23:17 运行, 对应北京时间 07:17(UTC+8),实际运行时间会被 Github 推迟. 要修改时间, 编辑 `.github/workflows/auto.yml`:

```yaml
on:
  schedule:
    - cron: '17 23 * * *'
```

---

## 运行流程

1. 检查 Cookie 和房间号, 缺失就退出
2. 用 `udb_cred` 续期, 拿到新 Cookie 后**立刻**写回 Secret 并落缓存
3. 读取背包虎粮数量
4. 把虎粮分配到所有房间
5. 依次进房间, 送出虎粮
6. 房间打卡
7. 输出结果
8. 如果配了 SEND_KEY, 推送结果到微信

---

## 推送结果

推送内容类似:

```
🚀 房间 XXX 送出虎粮 X 个
打卡结果: ✅ 打卡成功 / ℹ️ 已打卡
```

---

## 注意

- 不要分享 Cookie
- Cookie 会自动续期. 如果日志出现「续期被拒绝」, 说明 `udb_cred` 也失效了, 重新获取 Cookie 即可
- 打卡需要账号已加入该主播的粉丝团
- 本项目仅供学习交流, 请遵守平台规则

---

## 常见问题

- **登录失败**: 如果日志显示「续期成功」之后仍失败, 多半是网络问题; 如果显示「续期被拒绝」, 说明 Cookie 失效
- **新 Cookie 只活一两天**: 看体检输出。没有 `udb_cred` 就无法续期, 只能撑到 `udb_biztoken` 到期;
  有 `udb_cred` 但很快失效, 通常是**浏览器和脚本在抢同一份登录态** —— 浏览器每刷一次页面就会
  滚掉一次 `udb_cred`, 脚本手里那份立刻作废。导出/抓取之后退出浏览器登录, 或者用一个
  专用浏览器配置文件, 只给脚本用
- **Secret 没更新**: 日志里出现 `[SECRET] 写入 xxx 失败 HTTP 403` 说明 `GH_PAT` 没有
  Secrets 写权限(fine-grained 要勾 Repository permissions → Secrets: Read and write);
  `401` 说明令牌过期
- **赠送失败**: 大概率网络问题, 稍等一会儿再跑一次
- **打卡失败**: 确认这个房间有没有加入粉丝团
- **Action 报错**: 检查 Secrets 是否配置正确; `POST:回写报告` 那一步会打印同步结果

---

## 许可证

MIT License
