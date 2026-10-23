import asyncio
import re
import os
import time
from datetime import datetime, timezone
from typing import Dict, Any, List

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult, MessageChain, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.platform import AstrBotMessage, PlatformMetadata, MessageMember
from astrbot.core.message.components import Plain
from astrbot.core.platform.message_type import MessageType
from astrbot.core.agent.tool import ToolSet
from pydantic import Field
from pydantic.dataclasses import dataclass
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool
from astrbot.core.astr_agent_context import AstrAgentContext

from .imap_client import (
    imap_fetch_new,
    imap_query_since,
    imap_fetch_by_uid,
    is_recent_email,
)
from .smtp_client import smtp_send_mail


# ── [SEC-2026-10-24] 日志脱敏工具（模块级，供插件类与 SendMailTool 共用） ──
_SENSITIVE_KEYS = frozenset({"password", "smtp_password", "smtp_username", "username"})


def _redact_account(acc: Any) -> Any:
    """返回脱敏后的账户副本，剔除密码/授权码/登录名等敏感字段。"""
    if not isinstance(acc, dict):
        return acc
    return {k: v for k, v in acc.items() if k not in _SENSITIVE_KEYS}


def _redact_accounts(accounts: Any) -> Any:
    """批量脱敏；非 list/tuple 原样返回。"""
    if isinstance(accounts, (list, tuple)):
        return [_redact_account(a) for a in accounts]
    return accounts


@register(
    "astrbot_plugin_mail",
    "Neo",
    "imap邮箱接受与smtp回复",
    "1.0.1",
)
class MailNotifyPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._check_task: asyncio.Task | None = None
        self._last_check_time: dict[str, str] = {}
        self._account_status: dict[str, str] = {}
        # 默认值 []，与 SendMailTool.accounts 的 List 类型一致
        self.mail_tool = SendMailTool(accounts=self.config.get("mail_accounts", []) or [])
        self.context.add_llm_tools(self.mail_tool)

    async def initialize(self):
        self._check_task = asyncio.create_task(self._check_loop())
        logger.info("邮件通知插件：后台检查循环已启动。")

    # ── 统一配置读取工具 ──────────────────────────────────
    def _get_all_accounts(self) -> list:
        """统一读取账户列表。schema 顶层是 mail_accounts；兼容旧键 accounts。"""
        accounts = self.config.get("mail_accounts", [])
        if accounts:
            return accounts
        accounts = self.config.get("accounts", [])
        if accounts:
            return accounts
        try:
            cfg = self.context.get_config()
            if cfg:
                accounts = cfg.get("mail_accounts", []) or cfg.get("accounts", [])
                if accounts:
                    return accounts
        except Exception:
            pass
        return []

    @staticmethod
    def _normalize_umo(umo: str) -> str:
        """AstrBot 框架要求 UMO 为三段式 platform:message_type:session_id。
        两段式（如 webchat:default）会触发 'not enough values to unpack'，这里补齐。"""
        if not umo:
            return umo
        parts = umo.split(":")
        if len(parts) == 2:
            return f"{parts[0]}:FriendMessage:{parts[1]}"
        return umo

    def _get_account_notify_umo(self, account: dict) -> str:
        """账户级 notify_umo 优先，全局兜底，再统一规范化。"""
        raw = account.get("notify_umo") or self.config.get("notify_umo", "")
        return self._normalize_umo(raw)

    def _get_account_by_name_or_email(self, account_name: str) -> dict | None:
        accounts = self._get_all_accounts()
        target = account_name.strip()
        for acc in accounts:
            if target in ((acc.get("name") or "").strip(), (acc.get("email") or "").strip()):
                return acc
        for acc in accounts:
            name = (acc.get("name") or "").strip()
            addr = (acc.get("email") or "").strip()
            if target.lower() in name.lower() or target.lower() == addr.lower():
                return acc
        return None

    def _get_admin_uids(self) -> set[str]:
        admin_uids = self.config.get("admin_uids", []) or []
        return {
            str(uid).strip()
            for uid in admin_uids
            if isinstance(uid, (str, int)) and str(uid).strip()
        }

    def _get_admin_denied_message(self) -> str:
        if not self._get_admin_uids():
            return "❌ 还未指定插件管理员。\n请在插件web设置的admin_uid中添加用户id。"
        return "❌ 无权限使用该命令。"

    def _is_plugin_admin(self, event: AstrMessageEvent) -> bool:
        admin_uids = self._get_admin_uids()
        sender_id = str(event.get_sender_id()).strip()
        return bool(sender_id and sender_id in admin_uids)

    # ── 后台循环 ──────────────────────────────────────────
    async def _check_loop(self):
        await asyncio.sleep(10)
        while True:
            try:
                interval = max(int(self.config.get("check_interval", 5) or 5), 1)
                accounts = self._get_all_accounts()
                for account in accounts:
                    if not account.get("email") or not account.get("imap_server"):
                        continue
                    notify_umo = self._get_account_notify_umo(account)
                    if notify_umo:
                        try:
                            await self._check_account(account)
                            self._account_status[account["email"]] = "✅ 正常"
                        except Exception as e:
                            self._account_status[account["email"]] = f"❌ {str(e)[:80]}"
                            logger.error(f"邮件通知插件：{account['email']} 检查失败: {e}")
                        self._last_check_time[account["email"]] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    else:
                        logger.debug(f"邮件通知插件：账户 {account['email']} 未配置 notify_umo，跳过检查。")
                await asyncio.sleep(interval * 60)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"邮件通知插件：循环异常: {e}")
                await asyncio.sleep(60)

    # ── IMAP 逻辑 ─────────────────────────────────────────
    async def _check_account(self, account: dict):
        account_email = account["email"]
        max_body_len = max(int(self.config.get("max_body_length", 500) or 500), 1)
        filter_body_len = max(
            int(self.config.get("filter_body_length", 3000) or 3000), max_body_len
        )

        notify_umo = self._get_account_notify_umo(account)
        if not notify_umo:
            logger.warning(f"账户 {account_email} 未配置 notify_umo，跳过处理。")
            return

        uid_key = f"last_uid_{account_email}"
        init_key = f"init_time_{account_email}"
        last_uid = await self.get_kv_data(uid_key, 0) or 0
        init_time = await self.get_kv_data(init_key, "")

        is_first_run = not init_time
        if is_first_run:
            init_time = datetime.now(timezone.utc).isoformat()
            await self.put_kv_data(init_key, init_time)

        # baseline_only=True 时只记基线不返回邮件；空邮箱场景（last_uid=0 且非首次）
        # 会走正常搜索 UID 1:*，确保第一封邮件不丢
        new_emails, new_max_uid = await asyncio.to_thread(
            imap_fetch_new,
            account,
            last_uid,
            max_body_len,
            filter_body_len,
            baseline_only=is_first_run,
        )

        if new_max_uid > last_uid:
            await self.put_kv_data(uid_key, new_max_uid)

        if is_first_run:
            if new_max_uid > 0:
                logger.info(f"邮件通知插件：{account_email} 初始化完成，最大UID = {new_max_uid}")
            return

        try:
            init_dt = datetime.fromisoformat(init_time)
        except Exception:
            init_dt = datetime.now(timezone.utc)

        for mail_info in new_emails:
            if not is_recent_email(mail_info, init_dt):
                continue

            # 接线黑白名单过滤
            should_notify, reason = self._should_notify_mail(mail_info)
            if not should_notify:
                logger.info(f"邮件被过滤: {reason} - {mail_info['subject']}")
                continue

            should_forward = account.get("forward_to_user", True)
            ai_enabled = account.get("ai_mode", False)

            if ai_enabled:
                try:
                    session_id = f"mail_agent_{account_email}_{notify_umo}"
                    prov_id = await self.context.get_current_chat_provider_id(umo=notify_umo)
                    if not prov_id:
                        prov_id = await self.context.get_current_chat_provider_id(umo="")

                    persona_text = await self._get_personality_prompt(account)
                    if not persona_text:
                        persona_text = "你是一个邮件助手。"

                    fake_astrbot_message = AstrBotMessage()
                    fake_astrbot_message.type = MessageType.FRIEND_MESSAGE
                    fake_astrbot_message.self_id = "astrbot"
                    fake_astrbot_message.session_id = notify_umo
                    fake_astrbot_message.message_id = "mail_plugin_" + str(int(time.time()))
                    fake_astrbot_message.sender = MessageMember(
                        user_id="internal_bot", nickname="Mail Plugin"
                    )
                    fake_astrbot_message.message = [Plain(text="System: New Email Notification")]
                    fake_astrbot_message.message_str = "System: New Email Notification"
                    fake_astrbot_message.raw_message = {}
                    fake_astrbot_message.timestamp = int(time.time())

                    fake_platform_meta = PlatformMetadata(
                        name="MailPlugin",
                        description="Mail Notification",
                        id="mail_plugin",
                    )

                    fake_event = AstrMessageEvent(
                        message_str=fake_astrbot_message.message_str,
                        message_obj=fake_astrbot_message,
                        platform_meta=fake_platform_meta,
                        session_id=notify_umo,
                    )

                    custom_prompt = account.get(
                        "custom_prompt",
                        "你收到了一封新邮件，请阅读后决定是否回复或执行其他操作。",
                    )
                    final_system_prompt = (
                        f"# 角色设定-{persona_text}- # 任务指令-{custom_prompt}-"
                    ).strip()
                    llm_resp = await self.context.tool_loop_agent(
                        event=fake_event,
                        chat_provider_id=prov_id,
                        system_prompt=final_system_prompt,
                        prompt=(
                            f"📧 {account.get('name', '邮件')}收到新邮件，请处理：\n"
                            f"主题：{mail_info['subject']}\n"
                            f"发件人：{mail_info['from_addr']}\n"
                            f"内容：{mail_info['body'][:1000]}"
                        ),
                        max_steps=30,
                        tools=ToolSet([SendMailTool(accounts=[account])]),
                    )
                    ai_reply = llm_resp.completion_text
                    logger.info(f"AI 处理完成: {ai_reply}")
                except Exception as e:
                    logger.error(f"AI Agent 处理失败: {e}", exc_info=True)

            # 接线结构化通知（含 ai_summary）
            if should_forward:
                await self._send_notification(account, mail_info, notify_umo)

    async def _get_personality_prompt(self, account: dict) -> str:
        persona_id = account.get("llm_persona", "")
        if persona_id:
            persona_mgr = self.context.persona_manager
            persona = await persona_mgr.get_persona(persona_id)
            if persona:
                return persona.system_prompt
        return "你是一个邮件助手，请阅读邮件并决定是否需要回复。"

    # ── 过滤与通知 ────────────────────────────────────────
    def _get_filter_settings(self, prefix: str) -> dict:
        settings = self.config.get(f"{prefix}_settings", {}) or {}
        if isinstance(settings, dict):
            return settings
        return {}

    def _get_filter_enabled(self, prefix: str) -> bool:
        settings = self._get_filter_settings(prefix)
        if "enable" in settings:
            return bool(settings.get("enable", False))
        return bool(self.config.get(f"enable_{prefix}", False))

    def _get_filter_rules(self, prefix: str, field: str) -> list[str]:
        settings = self._get_filter_settings(prefix)
        nested_values = settings.get(f"{field}_rules", [])
        if not nested_values:
            nested_values = self.config.get(f"{field}_{prefix}", []) or []
        values = nested_values or []
        return [
            str(value).strip()
            for value in values
            if isinstance(value, (str, int, float)) and str(value).strip()
        ]

    @staticmethod
    def _matches_sender_rule(mail_info: dict, rule: str) -> bool:
        normalized_rule = rule.strip().casefold()
        if not normalized_rule:
            return False
        from_addr = (mail_info.get("from_addr") or "").strip().casefold()
        from_name = (mail_info.get("from_name") or "").strip().casefold()
        if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", normalized_rule):
            return from_addr == normalized_rule
        if normalized_rule.startswith("@"):
            return from_addr.endswith(normalized_rule)
        return normalized_rule in from_addr or normalized_rule in from_name

    @staticmethod
    def _matches_contains_rule(text: str, rule: str) -> bool:
        normalized_rule = rule.strip().casefold()
        if not normalized_rule:
            return False
        return normalized_rule in (text or "").casefold()

    def _match_rule_group(self, mail_info: dict, prefix: str) -> tuple[bool, str | None]:
        sender_rules = self._get_filter_rules(prefix, "sender")
        for rule in sender_rules:
            if self._matches_sender_rule(mail_info, rule):
                return True, f"sender:{rule}"
        subject_rules = self._get_filter_rules(prefix, "subject")
        subject = mail_info.get("subject") or ""
        for rule in subject_rules:
            if self._matches_contains_rule(subject, rule):
                return True, f"subject:{rule}"
        body_rules = self._get_filter_rules(prefix, "body")
        filter_body = mail_info.get("filter_body") or mail_info.get("body") or ""
        for rule in body_rules:
            if self._matches_contains_rule(filter_body, rule):
                return True, f"body:{rule}"
        return False, None

    def _should_notify_mail(self, mail_info: dict) -> tuple[bool, str]:
        enable_blacklist = self._get_filter_enabled("blacklist")
        enable_whitelist = self._get_filter_enabled("whitelist")
        if enable_blacklist:
            is_blacklisted, rule = self._match_rule_group(mail_info, "blacklist")
            if is_blacklisted:
                return False, f"被黑名单屏蔽 ({rule})"
        if enable_whitelist:
            is_whitelisted, rule = self._match_rule_group(mail_info, "whitelist")
            if not is_whitelisted:
                return False, "被白名单屏蔽"
            return True, f"白名单允许 ({rule})"
        return True, "允许，因为不需要匹配白名单限制"

    async def _send_notification(self, account: dict, mail_info: dict, notify_umo: str):
        account_name = account.get("name") or account["email"]
        use_ai = self.config.get("ai_summary", False)
        body_text = mail_info["body"]
        if use_ai and body_text:
            body_text = await self._try_ai_summary(mail_info, notify_umo, body_text)

        lines = [
            f"📬 新邮件通知 [{account_name}]",
            "━━━━━━━━━━━━━━━━",
            f"📤 发件人: {mail_info['from_name']}",
        ]
        if mail_info["from_addr"] and mail_info["from_addr"] != mail_info["from_name"]:
            lines[-1] += f" <{mail_info['from_addr']}>"
        lines.append(f"📋 主题: {mail_info['subject']}")
        lines.append(f"🕐 时间: {mail_info['date']}")
        if body_text:
            label = "📝 AI摘要" if use_ai else "📝 预览"
            lines.append(f"{label}: {body_text}")

        chain = MessageChain().message("\n".join(lines))
        await self.context.send_message(notify_umo, chain)

    async def _try_ai_summary(self, mail_info: dict, notify_umo: str, fallback: str) -> str:
        try:
            provider_id = await self.context.get_current_chat_provider_id(umo=notify_umo)
            if not provider_id:
                return fallback
            prompt = (
                "请用简洁的中文（不超过100字）总结以下邮件内容，只输出摘要：\n"
                f"主题：{mail_info['subject']}\n"
                f"正文：{fallback}"
            )
            llm_resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
            )
            if llm_resp and llm_resp.completion_text:
                return llm_resp.completion_text
        except Exception as e:
            logger.warning(f"MailNotify: AI summary failed: {e}")
        return fallback

    # ── 命令解析 ──────────────────────────────────────────
    def _parse_mail_reply_args(self, message_str: str) -> tuple[str, str, str, str]:
        raw = re.sub(r"\s+", " ", (message_str or "").strip())
        if not raw:
            raise ValueError("参数为空。")
        parts = raw.split(" ", 1)
        if len(parts) < 2:
            raise ValueError("参数缺失。")
        args_text = parts[1].strip()
        args = args_text.split(" ", 2)
        if len(args) < 3:
            raise ValueError("参数不足。")
        account_name, to_addr, subject_body = args[0].strip(), args[1].strip(), args[2]
        if "|" not in subject_body:
            raise ValueError("缺少主题与正文分隔符。")
        subject, body = [s.strip() for s in subject_body.split("|", 1)]
        if not account_name:
            raise ValueError("账户名不能为空。")
        if not to_addr:
            raise ValueError("收件人不能为空。")
        if "@" not in to_addr:
            raise ValueError("收件人邮箱格式错误。")
        if not subject:
            raise ValueError("邮件主题不能为空。")
        if not body:
            raise ValueError("邮件正文不能为空。")
        if len(subject) > 200:
            raise ValueError("邮件主题过长（最多 200 字符）。")
        if len(body) > 5000:
            raise ValueError("邮件正文过长（最多 5000 字符）。")
        return account_name, to_addr, subject, body

    # ── LLM 工具 ──────────────────────────────────────────
    @filter.llm_tool(name="send_smtp_mail")
    async def send_smtp_mail(
        self,
        event: AstrMessageEvent,
        to_addr: str,
        subject: str,
        body: str,
        account_name: str = None,
        attachments: list = None,
    ) -> MessageEventResult:
        '''
        发送 SMTP 邮件。

        Args:
            to_addr(string): 收件人邮箱地址
            subject(string): 邮件主题
            body(string): 邮件正文内容
            account_name(string): 发件账户名称（可选）
            attachments(list): 附件文件路径列表（可选）
        '''
        if not to_addr or "@" not in to_addr:
            return "❌ 错误：收件人邮箱格式不正确。"
        if not subject or not subject.strip():
            return "❌ 错误：邮件主题不能为空。"
        if not body or not body.strip():
            return "❌ 错误：邮件内容不能为空。"
        if attachments:
            for path in attachments:
                if not os.path.exists(path):
                    return f"❌ 错误：附件文件不存在：{path}"

        smtp_config = self.choose_account(account_name, to_addr)
        if not smtp_config:
            if account_name:
                return f"❌ 错误：未找到匹配的SMTP账户 '{account_name}'。请检查账户备注名。"
            return "❌ 错误：未找到有效的SMTP账户配置。"

        try:
            # 改 asyncio.to_thread，避免阻塞事件循环
            await asyncio.to_thread(
                smtp_send_mail, smtp_config, to_addr, subject, body, attachments
            )
            base_msg = f"✅ 邮件发送成功！\n📬 已发送至：{to_addr}\n📌 主题：{subject}"
            if attachments:
                base_msg += f"\n📎 附有 {len(attachments)} 个文件"
            return base_msg
        except ValueError as e:
            return f"❌ 配置错误：{str(e)}"
        except RuntimeError as e:
            return f"❌ 发送失败：{str(e)}"
        except Exception as e:
            # [SEC-2026-10-24] 不把 smtp_config / 异常栈里的局部变量写入日志，
            # 只记录异常类型与消息，避免授权码经 exc_info 泄露
            logger.error(
                "send_smtp_mail 未预期异常: %s: %s",
                type(e).__name__,
                str(e),
            )
            return f"❌ 未知错误：{str(e)}"

    def choose_account(self, account_name: str, to_addr: str) -> dict | None:
        all_accounts = self._get_all_accounts()
        if not all_accounts:
            logger.warning("❌ choose_account: 未找到账户列表。")
            return None

        if account_name and account_name.strip():
            query = account_name.strip()
            logger.debug(f"🔍 正在尝试匹配账户名: '{query}'")
            for acc in all_accounts:
                config_name = str(acc.get("name", "")).strip()
                if query.lower() in config_name.lower():
                    logger.info(f"✅ 成功匹配账户 (模糊匹配): {config_name} ({acc.get('email')})")
                    return acc
            for acc in all_accounts:
                if query.lower() == str(acc.get("email", "")).strip().lower():
                    logger.info(f"✅ 成功匹配账户 (邮箱): {acc.get('email')}")
                    return acc
            available = [a.get("name", "未命名") for a in all_accounts]
            logger.warning(f"❌ 未找到匹配的账户。搜索词: '{query}', 可用账户名: {available}")
            return None

        # 读取 default_smtp_account
        default_name = str(self.config.get("default_smtp_account", "") or "").strip()
        if default_name:
            for acc in all_accounts:
                name = str(acc.get("name", "")).strip()
                addr = str(acc.get("email", "")).strip()
                if default_name.lower() in (name.lower(), addr.lower()):
                    logger.info(f"✅ 使用默认账户: {name or addr}")
                    return acc
            logger.warning(f"⚠️ default_smtp_account '{default_name}' 未匹配，回退到第一个账户")

        first = all_accounts[0]
        logger.info(f"✅ 使用第一个账户: {first.get('name', '未知')}")
        return first

    # ── 命令 ──────────────────────────────────────────────
    @filter.command("mail_status")
    async def mail_status(self, event: AstrMessageEvent):
        if not self._is_plugin_admin(event):
            yield event.plain_result(self._get_admin_denied_message())
            return
        accounts = self._get_all_accounts()
        global_umo = self._normalize_umo(self.config.get("notify_umo", ""))
        interval = int(self.config.get("check_interval", 5) or 5)

        if not accounts:
            yield event.plain_result("📭 未配置任何邮箱账户，请在 WebUI 插件配置中添加。")
            return

        lines = [
            f"📊 邮箱监控状态 (间隔: {interval}分钟)",
            f"🔔 全局通知目标: {global_umo or '❗未绑定'}",
            "━━━━━━━━━━━━━━━━",
        ]
        for acc in accounts:
            addr = acc.get("email", "?")
            name = acc.get("name") or addr
            status = self._account_status.get(addr, "⏳ 等待首次检查")
            last = self._last_check_time.get(addr, "尚未检查")
            acc_umo = self._get_account_notify_umo(acc)
            lines.append(f"📧 {name} ({addr})")
            lines.append(f"   通知目标: {acc_umo or '❗未配置'}")
            lines.append(f"   状态: {status}")
            lines.append(f"   最近检查: {last}")
        yield event.plain_result("\n".join(lines))

    @filter.command("mail_check")
    async def mail_check(self, event: AstrMessageEvent):
        if not self._is_plugin_admin(event):
            yield event.plain_result(self._get_admin_denied_message())
            return
        accounts = self._get_all_accounts()
        if not accounts:
            yield event.plain_result("📭 未配置任何邮箱账户，请在 WebUI 插件配置中添加。")
            return

        yield event.plain_result("🔍 正在检查所有邮箱...")
        errors = []
        skipped = []
        for account in accounts:
            if not account.get("email") or not account.get("imap_server"):
                continue
            email_addr = account["email"]
            name = account.get("name") or email_addr
            if not self._get_account_notify_umo(account):
                skipped.append(name)
                self._account_status[email_addr] = "⚠️ 未配置 notify_umo，已跳过"
                continue
            try:
                await self._check_account(account)
                self._account_status[email_addr] = "✅ 正常"
            except Exception as e:
                self._account_status[email_addr] = f"❌ {str(e)[:80]}"
                errors.append(f"{name}: {e}")
            self._last_check_time[email_addr] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if errors:
            yield event.plain_result("⚠️ 部分邮箱检查失败:\n" + "\n".join(errors))
        elif skipped:
            yield event.plain_result(
                f"✅ 检查完成。\n⚠️ 以下账户未配置 notify_umo，已跳过: {', '.join(skipped)}"
            )
        else:
            yield event.plain_result("✅ 所有邮箱检查完成。")

    @filter.command("mail_query")
    async def mail_query(
        self, event: AstrMessageEvent, account_name: str = "", since_date: str = ""
    ):
        if not self._is_plugin_admin(event):
            yield event.plain_result(self._get_admin_denied_message())
            return
        if not account_name or not since_date:
            yield event.plain_result(
                "❌ 用法: /mail_query <账户备注名> <YYYY-MM-DD>\n"
                "示例: /mail_query qq邮箱 2026-03-01"
            )
            return

        accounts = self._get_all_accounts()
        target = None
        for acc in accounts:
            if account_name in (acc.get("name", ""), acc.get("email", "")):
                target = acc
                break
        if not target:
            yield event.plain_result(
                f'❌ 未找到名为 "{account_name}" 的邮箱账户。\n'
                f"已配置的账户: {', '.join(a.get('name') or a.get('email', '?') for a in accounts)}"
            )
            return

        try:
            since_dt = datetime.strptime(since_date, "%Y-%m-%d")
        except ValueError:
            yield event.plain_result("❌ 日期格式错误，请使用 YYYY-MM-DD，如 2026-03-01")
            return

        yield event.plain_result(f"🔍 正在查询 {account_name} 自 {since_date} 以来的邮件...")
        try:
            max_body_len = self.config.get("max_body_length", 500)
            emails = await asyncio.to_thread(imap_query_since, target, since_dt, max_body_len)
        except Exception as e:
            yield event.plain_result(f"❌ 查询失败: {e}")
            return

        if not emails:
            yield event.plain_result(f"📭 {account_name} 自 {since_date} 以来没有邮件。")
            return

        lines = [
            f"📬 {account_name} 自 {since_date} 以来共 {len(emails)} 封邮件：",
            "━━━━━━━━━━━━━━━━",
        ]
        for i, m in enumerate(emails, 1):
            lines.append(f"{i}. 📋 {m['subject']}")
            lines.append(
                f"   📤 {m['from_name']}  🕐 {m['date']}  🆔 UID:{m['uid']}"
            )
        lines.append("")
        lines.append("💡 使用 /mail_read <账户名> <UID> 查看邮件全文")
        yield event.plain_result("\n".join(lines))

    @filter.command("mail_read")
    async def mail_read(
        self, event: AstrMessageEvent, account_name: str = "", uid: str = ""
    ):
        """按 UID 读取单封邮件全文。格式：/mail_read <账户备注名> <邮件UID>"""
        if not self._is_plugin_admin(event):
            yield event.plain_result(self._get_admin_denied_message())
            return

        if not account_name or not uid:
            yield event.plain_result(
                "❌ 用法: /mail_read <账户备注名> <邮件UID>\n"
                "示例: /mail_read abird_admin 42\n"
                "提示: 先用 /mail_query 查询邮件列表，列表里会显示每封邮件的 UID"
            )
            return

        try:
            uid_int = int(str(uid).strip())
        except ValueError:
            yield event.plain_result("❌ UID 必须是数字。")
            return
        if uid_int <= 0:
            yield event.plain_result("❌ UID 必须为正整数。")
            return

        account = self._get_account_by_name_or_email(account_name)
        if not account:
            accounts = self._get_all_accounts()
            names = ", ".join(
                (a.get("name") or a.get("email") or "?") for a in accounts
            )
            yield event.plain_result(
                f'❌ 未找到名为 "{account_name}" 的邮箱账户。\n'
                f"已配置账户: {names or '(空)'}"
            )
            return

        yield event.plain_result(
            f"🔍 正在读取 {account_name} 的 UID={uid_int} 邮件..."
        )
        try:
            mail = await asyncio.to_thread(imap_fetch_by_uid, account, uid_int)
        except Exception as e:
            yield event.plain_result(f"❌ 读取失败: {e}")
            return

        max_len = max(int(self.config.get("read_body_length", 3000) or 3000), 100)
        body = mail.get("body") or ""
        truncated_note = ""
        if len(body) > max_len:
            body = body[:max_len]
            truncated_note = (
                f"\n\n⚠️ 正文超过 {max_len} 字已截断，"
                f"完整内容请到邮箱客户端查看。"
            )

        lines = [
            f"📧 {mail['subject']}",
            f"📤 {mail['from_name']} <{mail['from_addr']}>",
            f"🕐 {mail['date']}",
            f"🆔 UID: {mail['uid']}",
            "━━━━━━━━━━━━━━━━",
            body if body else "(无正文)",
        ]
        yield event.plain_result("\n".join(lines) + truncated_note)

    @filter.command("mail_reply")
    async def mail_reply(self, event: AstrMessageEvent):
        if not self._is_plugin_admin(event):
            yield event.plain_result(self._get_admin_denied_message())
            return
        usage = (
            "❌ 用法错误\n"
            "格式: /mail_reply <账户备注名> <收件人邮箱> <主题>|<正文>\n"
            "示例: /mail_reply qq邮箱 test@example.com 回复主题|你好，已收到你的邮件。"
        )
        try:
            account_name, to_addr, subject, body = self._parse_mail_reply_args(event.message_str)
        except ValueError as e:
            yield event.plain_result(f"{usage}\n原因: {e}")
            return

        account = self._get_account_by_name_or_email(account_name)
        if not account:
            accounts = self._get_all_accounts()
            account_names = ", ".join((a.get("name") or a.get("email") or "?") for a in accounts)
            yield event.plain_result(
                f'❌ 未找到名为 "{account_name}" 的邮箱账户。\n已配置账户: {account_names or "(空)"}'
            )
            return

        if not account.get("smtp_server"):
            yield event.plain_result(
                "❌ 该账户未配置 SMTP 服务器。请在插件配置中填写 smtp_server、smtp_port、smtp_use_ssl。"
            )
            return

        yield event.plain_result("📤 正在发送邮件...")
        try:
            await asyncio.to_thread(smtp_send_mail, account, to_addr, subject, body)
        except Exception as e:
            yield event.plain_result(f"❌ 发送失败: {e}")
            return
        account_display = account.get("name") or account.get("email") or account_name
        yield event.plain_result(
            f"✅ 发送成功\n账户: {account_display}\n收件人: {to_addr}\n主题: {subject}"
        )

    # ── Lifecycle ─────────────────────────────────────────
    async def terminate(self):
        if self._check_task and not self._check_task.done():
            self._check_task.cancel()
            try:
                await self._check_task
            except asyncio.CancelledError:
                pass
        logger.info("MailNotify: plugin terminated.")


@dataclass
class SendMailTool(FunctionTool[AstrAgentContext]):
    accounts: List[Dict[str, Any]] = Field(default_factory=list)
    name: str = "reply_smtp_mail"
    description: str = (
        "通过SMTP协议发送邮件。需要配置至少一个SMTP账户。"
        "to_addr: 收件人邮箱地址。"
        "subject: 邮件主题。"
        "body: 邮件正文内容。"
        "account_name: 使用的账户的备注名，支持模糊匹配。"
        "attachments: (可选) 附件文件路径列表。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "to_addr": {"type": "string", "description": "收件人邮箱地址"},
                "subject": {"type": "string", "description": "邮件主题"},
                "body": {"type": "string", "description": "邮件正文内容"},
                "account_name": {"type": "string", "description": "发件账户名称，支持模糊匹配"},
                "attachments": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "附件文件路径列表（可选）",
                },
            },
            "required": ["to_addr", "subject", "body", "account_name"],
        }
    )

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        accounts = self.accounts
        if not accounts:
            return "❌ 错误：未找到可用的SMTP账户配置。请检查插件配置。"

        to_addr = kwargs.get("to_addr")
        subject = kwargs.get("subject")
        body = kwargs.get("body")
        account_name = kwargs.get("account_name")
        attachments = kwargs.get("attachments", [])

        if not to_addr or "@" not in to_addr:
            return "❌ 错误：收件人邮箱格式不正确。"
        if not subject or not subject.strip():
            return "❌ 错误：邮件主题不能为空。"
        if not body or not body.strip():
            return "❌ 错误：邮件内容不能为空。"
        if not account_name or not account_name.strip():
            return "❌ 错误：发件账户名称 (account_name) 不能为空。"
        if attachments:
            for path in attachments:
                if not os.path.exists(path):
                    return f"❌ 错误：附件文件不存在：{path}"

        # [SEC-2026-10-24] 日志脱敏：只打印账户名与邮箱，绝不打印密码/授权码
        logger.debug(f"SendMailTool called with account_name: {account_name}")
        logger.debug(f"Available accounts (redacted): {_redact_accounts(accounts)}")

        smtp_config = None
        query = account_name.strip()
        available_names = []
        for acc in accounts:
            config_name = str(acc.get("name", "")).strip()
            available_names.append(config_name)
            if query.lower() in config_name.lower():
                smtp_config = acc
                break

        if not smtp_config:
            available_names_str = ", ".join(available_names) if available_names else "无"
            return (
                f"❌ 错误：未找到匹配的SMTP账户。\n"
                f"正在查找: '{query}'\n可用账户: [{available_names_str}]"
            )

        try:
            # 改 asyncio.to_thread，避免阻塞事件循环
            await asyncio.to_thread(
                smtp_send_mail, smtp_config, to_addr, subject, body, attachments
            )
            base_msg = f"✅ 邮件发送成功！\n📬 已发送至：{to_addr}\n📌 主题：{subject}"
            if attachments:
                base_msg += f"\n📎 附有 {len(attachments)} 个文件"
            return base_msg
        except Exception as e:
            # [SEC-2026-10-24] 不回传/记录异常栈中的 smtp_config 局部变量
            logger.error(
                "SendMailTool.call 发送失败: %s: %s",
                type(e).__name__,
                str(e),
            )
            return f"❌ 发送失败：{str(e)}"
