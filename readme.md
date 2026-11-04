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
.github/workflows/auto.yml  # GitHub Actions 配置
requirements.txt            # 依赖清单
main.py                     # 主程序
huya_cookie.py              # Cookie 续期, 可单独运行自检
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
4. 进入 **Actions**, 启用工作流
5. 默认每天北京时间 07:17 自动运行

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

## 关于 Cookie 续期

虎牙登录态里有两个关键 Cookie: `udb_biztoken` 负责接口鉴权, 寿命较短；`udb_cred` 是长期凭证, 寿命 180 天. 网页端会在 `udb_biztoken` 失效时用 `udb_cred` 自动换新, 同时滚动 `udb_cred` 本身.

本工具复刻了这个续期请求, 每次运行前自动续期, 再用新的 Cookie 继续后续操作. 续期后的 Cookie 使用 **Actions 缓存**保存, 缓存 7 天没访问会被清理, 每天跑一次就不会过期.

本地可以单独测试续期是否正常:

```bash
HUYA_COOKIE="你的Cookie" python huya_cookie.py
```

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
2. 读取背包虎粮数量
3. 把虎粮分配到所有房间
4. 依次进房间, 送出虎粮
5. 房间打卡
6. 输出结果
7. 如果配了 SEND_KEY, 推送结果到微信

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
- **赠送失败**: 大概率网络问题, 稍等一会儿再跑一次
- **打卡失败**: 确认这个房间有没有加入粉丝团
- **Action 报错**: 检查 Secrets 是否配置正确

---

## 许可证

MIT License
