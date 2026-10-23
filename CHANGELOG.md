# Changelog

基于 gangcaiyoule/astrbot_plugin_mail_notify 修改。

---

## 1.0.2

安全修复：

- `SendMailTool.call` 不再把含 `password` / `smtp_password` 的完整账户配置写入 `logger.debug`。新增模块级 `_redact_account()` / `_redact_accounts()`，剔除 `password`、`smtp_password`、`smtp_username`、`username` 后打印。
- `send_smtp_mail` 与 `SendMailTool.call` 的 `except` 分支不再带 `exc_info`，只记录异常类型与消息，避免异常栈中的 `smtp_config` 泄露授权码。

---

## 1.0.1

功能修复与新增：

- 修复 `mail_status` 读取 schema 不存在的 `accounts` 键，导致永远报「未配置任何邮箱账户」。
- 修复 `notify_umo` 两段式导致的后台检查崩溃（`not enough values to unpack (expected 3, got 2)`），新增 `_normalize_umo()` 自动补齐三段式。
- 修复 `mail_query` 输出段缩进错误导致的 `IndentationError`、插件无法加载。
- 新增 `/mail_read` 命令与 `imap_client.imap_fetch_by_uid()`，按 UID 查看单封邮件全文。
- 接线黑白名单过滤与 AI 摘要（此前 `_should_notify_mail` / `_send_notification` / `_try_ai_summary` 定义但从未调用）。
- 修复 STARTTLS 发信必崩（`timeout=0` 触发 `Non-blocking socket`）。
- AI 发信工具改为 `asyncio.to_thread`，避免 SMTP 建连期间阻塞事件循环。
- 修复突发超过 10 封时被跳过的邮件永久不推送。
- 修复空邮箱场景首封邮件丢失（新增 `baseline_only` 参数）。
- `check_interval` 强制 `int`，修复字符串导致的循环崩溃。
- `SendMailTool` 默认值 `{}` 改为 `[]`，与字段类型一致。
- 调试 `print` 改为 `logger.debug`。
- `mail_check` 支持账户级 `notify_umo`，未配置的账户跳过而非整体拒绝。
- 读取 `default_smtp_account` / `folder` / `username` 配置。
- `mail_status` 展示通知目标，`mail_query` 输出每封邮件的 UID，`/mail_reply` 用法提示修正。
- README 同步修正示例。

---

## 1.0.0

基于 astrbot_plugin_mail_notify 的修改版，新增 `send_smtp_mail` LLM 工具、`ai_mode` 自动处理、多邮箱 IMAP 监控、黑白名单过滤、AI 摘要、WebUI 配置。

已知问题（已在 1.0.1 / 1.0.2 修复）：

- `mail_status` 读错配置键。
- `notify_umo` 两段式崩溃。
- 黑白名单 / AI 摘要未接线。
- STARTTLS 分支 `timeout=0` 必崩。
- AI 发信同步阻塞事件循环。
- 突发超过 10 封、空邮箱首封邮件丢失。
- `mail_query` 缩进错误 + 缺 `mail_read`。
- `DEBUG` 级别下明文打印账户密码/授权码。