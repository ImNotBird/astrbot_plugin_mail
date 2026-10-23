# 📬 邮件插件

基于 gangcaiyoule 的 `astrbot_plugin_mail_notify` 修改，新增 `send_smtp_mail` / `reply_smtp_mail` 函数工具、AI 自动处理邮件、`/mail_read` 全文查看等能力。

✨ 监控多个 IMAP 邮箱的新邮件，通过 AstrBot 自动推送通知，支持 AI 摘要、AI 自动处理、手动回复、全文查看。✨

---

## ✨ 核心功能

- **多邮箱监控** —— 同时监控多个 IMAP 邮箱（Gmail、QQ、163、Outlook、校园邮箱等）。
- **自动推送** —— 后台定时轮询（默认 5 分钟），仅推送插件启用后收到的新邮件。
- **智能过滤** —— 支持黑白名单规则（发件人、主题、正文关键词），精准筛选通知内容。
- **AI 摘要** —— 可选调用 LLM 对邮件正文生成简洁中文摘要。
- **AI 自动处理** —— 开启账户级 `ai_mode` 后，新邮件交给 LLM Agent，AI 可自主调用工具回复。
- **手动交互** —— `/mail_query` 查询历史、`/mail_read` 查看全文、`/mail_reply` 手动回复。
- **LLM 工具** —— 用自然语言指示 AI 发送邮件，支持附件。
- **WebUI 配置** —— 在 AstrBot 管理面板可视化配置，无需改代码。

---

## 📖 快速开始

### 1. 配置邮箱账户

在 AstrBot WebUI → 插件管理 → 邮件插件 → 配置，找到 **`mail_accounts`** 模板列表，逐个添加账户。

账户级字段：

- `name`：账户备注名（指令里用它匹配），如 `qq邮箱`
- `email`：邮箱地址，如 `123456@qq.com`
- `sender_name`：发件时显示的发送者名，如 `AstrBot Mail`
- `imap_server`：IMAP 服务器地址，如 `imap.qq.com`
- `imap_port`：IMAP 端口（SSL 通常 993，STARTTLS 通常 143）
- `use_ssl`：IMAP 是否使用 SSL，默认 `true`
- `username`：IMAP 登录名（留空则用 `email`；企业邮箱登录名可能不同）
- `password`：IMAP 密码 / 授权码
- `folder`：监控的邮箱文件夹，默认 `INBOX`
- `smtp_server`：SMTP 服务器地址，如 `smtp.qq.com`
- `smtp_port`：SMTP 端口（SSL 通常 465，STARTTLS 通常 587）
- `smtp_use_ssl`：SMTP 是否使用 SSL，默认 `true`
- `smtp_password`：SMTP 密码（留空则复用 `password`）
- `forward_to_user`：是否把新邮件推送到 `notify_umo`，默认 `true`
- `ai_mode`：是否启用 AI 自动处理，默认 `false`
- `custom_prompt`：传给 AI 的任务指令，默认 `你收到了一封新邮件，请阅读后决定是否回复。`
- `llm_persona`：AI 使用的人格（WebUI 里选）
- `notify_umo`：该账户的通知目标会话 ID（三段式，留空继承全局），如 `aiocqhttp:FriendMessage:123456789`

### 2. 配置全局参数

在插件配置的**根层级**设置（注意：这些不在账户内）：

- `admin_uids`：允许使用指令的管理员 ID 列表（必填）
- `notify_umo`：全局默认通知目标（三段式），账户级优先
- `default_smtp_account`：默认发件账户备注名 / 邮箱，留空用第一个账户
- `check_interval`：轮询间隔（分钟），默认 5
- `ai_summary`：是否在推送前生成 AI 摘要，默认 `false`
- `max_body_length`：通知预览正文最大长度，默认 500
- `filter_body_length`：黑白名单匹配用的正文最大长度，默认 3000
- `blacklist_settings`：黑名单配置
- `whitelist_settings`：白名单配置

**⚠️ `notify_umo` 必须是三段式**

格式为 `platform:message_type:session_id`。常见示例：

- OneBot / NapCat 私聊：`aiocqhttp:FriendMessage:123456789`
- OneBot / NapCat 群聊：`aiocqhttp:GroupMessage:123456789`
- Telegram 私聊：`telegram:FriendMessage:123456789`
- WebChat：`webchat:FriendMessage:default`

> 插件会自动把两段式（如 `webchat:default`）补成三段式，但**建议直接写三段式**。`message_type` 常见值：`FriendMessage`（私聊）、`GroupMessage`（群聊）。

黑白名单配置示例（`blacklist_settings` / `whitelist_settings` 结构相同）：

```json
{
  "enable": true,
  "sender_rules": ["noreply@", "@qq.com", "广告邮件"],
  "subject_rules": ["促销", "优惠"],
  "body_rules": ["垃圾邮件", "退订"]
}
```

- `sender_rules`：完整邮箱精确匹配；`@域名` 后缀匹配；其余按包含匹配（发件人地址或显示名）。
- `subject_rules` / `body_rules`：不区分大小写的包含匹配。
- 黑名单先判：命中任一条 → 丢弃。
- 白名单后判：启用后必须至少命中一条才推送。
- 两者可同时启用，黑名单优先。

### 3. 验证运行

```
/mail_status
```

能看到账户列表、通知目标、最近检查时间，说明配置被正确读取。

---

## 📋 指令列表

> 所有指令仅限 `admin_uids` 列表中的管理员使用。

- `/mail_status`：查看所有邮箱的连接状态和最近检查时间
- `/mail_check`：立即手动检查所有邮箱的新邮件
- `/mail_query`：查询指定邮箱自某日期以来的邮件（最多 20 条，带 UID）
- `/mail_read`：按 UID 读取单封邮件全文
- `/mail_reply`：使用指定账户手动发送邮件

---

## 🔍 `/mail_query` 用法详解

```
/mail_query <账户备注名> <YYYY-MM-DD>
```

- `<账户备注名>`：配置中 `mail_accounts[].name` 字段，也支持直接填邮箱地址。
- `<YYYY-MM-DD>`：查询起始日期（含当天）。
- 效果：列出该邮箱自指定日期以来的邮件（最多 20 条），每条带 UID。

输出示例：

```
📬 qq邮箱 自 2026-10-01 以来共 3 封邮件：
━━━━━━━━━━━━━━━━
1. 📋 项目进度汇报
   📤 张三  🕐 2026-10-22 14:30  🆔 UID:42
2. 📋 周报
   📤 李四  🕐 2026-10-21 09:15  🆔 UID:41
3. 📋 会议纪要
   📤 王五  🕐 2026-10-20 18:00  🆔 UID:40

💡 使用 /mail_read <账户名> <UID> 查看邮件全文
```

---

## 📖 `/mail_read` 用法详解

```
/mail_read <账户备注名> <邮件UID>
```

- 先用 `/mail_query` 拿到 UID，再用本命令读全文。
- 正文默认最多显示 **3000 字**；超过会被截断并提示到邮箱客户端查看。
- 想调整上限：在 `config.json` 里给该插件加 `"read_body_length": 5000`（未在 schema 中，需手动编辑）。

输出示例：

```
📧 项目进度汇报
📤 张三 <zhangsan@example.com>
🕐 2026-10-22 14:30
🆔 UID: 42
━━━━━━━━━━━━━━━━
（完整正文……）
```

> 当前 `/mail_read` **只读正文，不下载附件**。附件请到邮箱客户端查看。

---

## 📨 `/mail_reply` 用法详解

```
/mail_reply <账户备注名> <收件人邮箱> <主题>|<正文>
```

- `<账户备注名>`：`mail_accounts[].name` 或邮箱地址
- `<收件人邮箱>`：必须含 `@`
- `<主题>|<正文>`：**英文半角竖线 `|` 是必填分隔符**，主题在前、正文在后
- 主题长度 ≤ 200 字，正文长度 ≤ 5000 字

正确示例：

```
/mail_reply qq邮箱 test@example.com 回复主题|你好，已收到你的邮件。
```

错误示例（会报「缺少主题与正文分隔符」）：

```
/mail_reply qq邮箱 test@example.com 回复主题 你好
```

> `/mail_reply` 目前**不支持附件**。需要发附件请用下面「LLM 工具」章节的方式。

---

## 🤖 LLM 工具

插件注册了两个 LLM 函数工具，可直接用自然语言指示 AI 发送邮件：

- `send_smtp_mail`（`@filter.llm_tool` 注册，支持附件）
- `reply_smtp_mail`（`SendMailTool` FunctionTool，支持附件）

用法示例（在聊天里直接说）：

```
帮我用 qq邮箱 给 test@example.com 发封邮件，主题"测试"，正文"你好，这是一封测试邮件"
```

```
用 qq邮箱 给 boss@company.com 发邮件，主题"报表"，正文"附件是本月报表"，附件 /data/reports/2026-10.pdf
```

前提：

- 当前会话已配置 chat provider（AI 模型）。
- `account_name` 支持模糊匹配（`query.lower() in config_name.lower()`）。
- 附件路径必须是 **AstrBot 进程可访问**的路径，文件不存在会报错。
- 未指定账户时，按 `default_smtp_account` → 第一个账户 的顺序选择。

---

## 🔑 各邮箱授权码获取

> **重要**：大多数邮箱需开启 IMAP/SMTP 服务并使用「授权码」而非登录密码。

- **QQ 邮箱**：网页登录 → 设置 → 账户 → 开启 IMAP/SMTP 服务 → 短信验证 → 生成 16 位授权码。
- **163 / 网易邮箱**：网页登录 → 设置 → POP3/SMTP/IMAP → 开启服务 → 手机验证 → 设置授权码。
- **Gmail**：开启两步验证 → 应用专用密码页面生成 16 位密码。国内直连 `imap.gmail.com` 通常不可用，需代理。
- **Outlook**：账户密码通常可用；若开双重验证需生成应用密码。

---

## 📡 通知效果示例

原始模式（`ai_summary: false`）：

```
📬 新邮件通知 [qq邮箱]
━━━━━━━━━━━━━━━━
📤 发件人: 张三 <zhangsan@example.com>
📋 主题: 项目进度汇报
🕐 时间: 2026-10-22 14:30
📝 预览: 你好，本周项目已完成模块A的开发……
```

AI 摘要模式（全局 `ai_summary: true`）：

```
📬 新邮件通知 [qq邮箱]
━━━━━━━━━━━━━━━━
📤 发件人: 张三 <zhangsan@example.com>
📋 主题: 项目进度汇报
🕐 时间: 2026-10-22 14:30
📝 AI摘要: 张三汇报本周完成模块A开发，需你确认下一步计划。
```

AI 自动处理模式（账户级 `ai_mode: true`）：

插件会构造事件调用 `tool_loop_agent`，AI 根据 `custom_prompt` + `llm_persona` 自行决定是否回复或执行其他工具，处理结果写入后台日志。可与 `forward_to_user` 同时开启。

---

## ⚙️ 常见问题

**Q: 收不到通知？**
1. `/mail_status` 确认账户被正确读取、通知目标已显示。
2. 确认 `notify_umo` 是**三段式**且平台名正确（`aiocqhttp` / `telegram` / `webchat` 等，不是 `QQUser`）。
3. 确认邮箱已开启 IMAP 服务。
4. 查看后台日志有无 `send_message` 相关报错。

**Q: 提示「无权限使用该命令」？**
把发送指令的账号 ID 加到 `admin_uids`。

**Q: 提示「Notification target is not bound yet.」？**
说明该账户既没配账户级 `notify_umo`，全局 `notify_umo` 也为空。任选一处填三段式即可。

**Q: 报 `not enough values to unpack (expected 3, got 2)` 或「不合法的 session 字符串」？**
`notify_umo` 写成了两段式（如 `webchat:default`）。改成三段式 `webchat:FriendMessage:default`。新版插件会自动补齐两段式，但建议直接写三段式。

**Q: 连接超时？**
Gmail 用户检查网络或代理；国内服务器建议用 QQ / 163。

**Q: AI 摘要没生效？**
AI 摘要由**全局** `ai_summary` 控制，与账户级 `ai_mode` 是两回事：
- `ai_summary: true` → 推送前先生成 100 字摘要。
- `ai_mode: true` → 新邮件交给 LLM Agent 自动处理（可回复、可执行工具）。

两者可独立开启。

**Q: `/mail_reply` 报「缺少主题与正文分隔符」？**
主题和正文之间必须用英文半角 `|` 分隔，例如 `回复主题|你好`。

**Q: 想发附件？**
`/mail_reply` 不支持附件。用自然语言让 AI 发（`send_smtp_mail` / `reply_smtp_mail` 支持 `attachments` 参数）。

**Q: STARTTLS（`smtp_use_ssl: false`）发信失败？**
确认 `smtp_port` 用 587，且插件版本已修复 starttls 的 `timeout=0` 崩溃问题（旧版会报 `Non-blocking socket (timeout=0) is not supported`）。