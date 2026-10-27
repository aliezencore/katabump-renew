## 🚀 katabump 自动续期（GitHub Actions）

这是一个基于 GitHub Actions 的自动化脚本，用于定时登录自动续期[katabump](https://dashboard.katabump.com) 应用。

⚠️ 有cf盾,太垃圾的机房节点可能过不了，建议用稍微干净点的节点,[B2proxy住宅代理](https://www.b2proxy.com/signup?code=0F5133)

━━━━━━━━━━━━━━━━━━━━━━

🔐 Secrets 配置说明

| Secret 名称         | 是否必填 | 说明                                              |
|---------------------|----------|---------------------------------------------------|
| KATABUMP_ACCOUNTS  | 二选一   | **多账号**，JSON 数组（格式见下）                      |
| KATABUMP_EMAIL     | 二选一   | katabump 登录邮箱（单账号，兼容旧配置）                  |
| KATABUMP_PASSWORD  | 二选一   | katabump 登录密码（单账号，兼容旧配置）                  |
| NODE_LINK          | ❌ 可选  | 代理链接，如 vless:// vmess:// tuic:// hysteria2:// anttls:// socks5://|
| TG_BOT_TOKEN       | ❌ 可选  | Telegram Bot Token（用于发送通知）                     |
| TG_CHAT_ID         | ❌ 可选  | Telegram Chat ID（接收通知的用户或群组 ID）              |

> 同时配置 `KATABUMP_ACCOUNTS` 与旧的 `KATABUMP_EMAIL` / `KATABUMP_PASSWORD` 时，以 `KATABUMP_ACCOUNTS` 为准。

### 多账号格式（KATABUMP_ACCOUNTS）

JSON 数组，每个元素含 `email` 和 `password`：

```json
[
  {"email": "a@example.com", "password": "pass1"},
  {"email": "b@example.com", "password": "pass2"}
]
```

注意：
- 账号按数组顺序**依次**执行，每个账号使用独立的浏览器会话，互不影响
- 单个账号登录失败或运行异常不会中断后续账号
- 密码里若含 `"` 或 `\`，需写成 `\"` / `\\` 转义
- `KATABUMP_ACCOUNTS` 一旦配置但 JSON 格式非法，脚本会直接报错退出（不会悄悄回落到旧单账号变量）

### 多账号执行流程

每个账号严格串行，上一个账号的浏览器完全退出并清空缓存后，才开始下一个：

```
[1/N] 邮箱A
  ├─ 新建独立 profile 目录（临时目录 katabump_profile_<时间戳>_<序号>）
  ├─ 启动浏览器 → 登录 → 进入详情页 → Renew → 提交
  ├─ 主动关闭浏览器会话（driver.quit）
  ├─ 清理残留 Chrome/chromedriver 进程
  └─ 删除该 profile 目录（cookie / 缓存随之丢弃）
[2/N] 邮箱B
  └─ ... 同上，使用全新 profile
```

无论账号成功、登录失败还是运行异常，清理都会执行。


━━━━━━━━━━━━━━━━━━━━━━
### 代理格式（确认在v2rayN里使用正常的节点）

`NODE_LINK` 支持以下任意一种代理协议的完整分享链接（不配置则直连）：

- **VLESS**：`vless://uuid@server:port?security=reality&sni=...&type=ws&...`
- **VMess**：`vmess://base64encoded...`
- **Trojan**：`trojan://password@server:port?sni=...&type=ws&...`
- **tuic**：`tuic://uuid:password@server:port...`
- **anytls**：`anytls://uuid@server:port...`
- **hysteria2**：`hysteria2://base64@server:port...`
- **SOCKS5**：`socks5://user:pass@server:port` 或 `socks://user:pass@server:port`

### 注意事项
- 尽量添加一个干净的节点，以免过不了cf盾
- cron时间根据自己的服务到期时间的前一天来修改
