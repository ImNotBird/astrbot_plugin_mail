import email as email_lib
import imaplib
from datetime import datetime, timezone
from email.utils import parseaddr, parsedate_to_datetime

from .mail_utils import decode_mime_header, extract_text_body, truncate_text
from .network_utils import open_tcp_socket


class _IMAP4PreferredIPv4(imaplib.IMAP4):
    def _create_socket(self, timeout):
        return open_tcp_socket(self.host, self.port, timeout=timeout)


class _IMAP4SSLPreferredIPv4(imaplib.IMAP4_SSL):
    def _create_socket(self, timeout):
        sock = open_tcp_socket(self.host, self.port, timeout=timeout)
        return self.ssl_context.wrap_socket(sock, server_hostname=self.host)


def is_recent_email(mail_info: dict, init_dt: datetime) -> bool:
    date_str = mail_info.get("date_raw", "")
    if not date_str:
        return True
    try:
        mail_dt = datetime.fromisoformat(date_str)
        if mail_dt.tzinfo is None:
            mail_dt = mail_dt.replace(tzinfo=timezone.utc)
        if init_dt.tzinfo is None:
            init_dt = init_dt.replace(tzinfo=timezone.utc)
        return mail_dt >= init_dt
    except Exception:
        return True


def _connect(account: dict) -> imaplib.IMAP4 | imaplib.IMAP4_SSL:
    server = account["imap_server"]
    port = account.get("imap_port", 993)
    use_ssl = account.get("use_ssl", True)

    if use_ssl:
        conn = _IMAP4SSLPreferredIPv4(server, port, timeout=30)
    else:
        conn = _IMAP4PreferredIPv4(server, port, timeout=30)

    # ✅ IMAP 登录名优先 username，回退 email
    login_user = account.get("username") or account["email"]
    conn.login(login_user, account["password"])

    email_addr = account.get("email", "")
    if email_addr.endswith("@163.com") or email_addr.endswith("@126.com"):
        id_payload = '("name" "AstrBot-MailNotify" "version" "1.3.2" "vendor" "AstrBot")'
        try:
            conn.xatom("ID", id_payload)
        except Exception:
            pass

    # ✅ 读取 schema 的 folder 字段
    folder = account.get("folder") or "INBOX"
    status, data = conn.select(folder, readonly=True)
    if status != "OK":
        status, data = conn.select(folder, readonly=False)

    if status != "OK":
        raise ValueError(f"无法选择 {folder}: status={status!r}, data={data!r}")
    return conn


def _parse_email(
    msg, uid: bytes, preview_body_len: int, filter_body_len: int | None = None
) -> dict:
    subject = decode_mime_header(msg.get("Subject", ""))
    from_raw = msg.get("From", "")
    from_name, from_addr = parseaddr(from_raw)
    from_name = decode_mime_header(from_name) if from_name else from_addr

    date_str = msg.get("Date", "")
    try:
        dt = parsedate_to_datetime(date_str)
        date_formatted = dt.strftime("%Y-%m-%d %H:%M")
        date_raw = dt.isoformat()
    except Exception:
        date_formatted = date_str[:25] if date_str else "未知"
        date_raw = ""

    body = extract_text_body(msg)
    preview_body = truncate_text(body, preview_body_len)
    filter_text = truncate_text(body, filter_body_len or preview_body_len)

    return {
        "uid": int(uid),
        "subject": subject or "(无主题)",
        "from_name": from_name,
        "from_addr": from_addr,
        "date": date_formatted,
        "date_raw": date_raw,
        "body": preview_body,
        "filter_body": filter_text,
    }


def imap_fetch_new(
    account: dict,
    last_uid: int,
    preview_body_len: int,
    filter_body_len: int | None = None,
    baseline_only: bool = False,
) -> tuple[list[dict], int]:
    """基于 UID 增量拉取新邮件。

    baseline_only=True 时仅记录当前最大 UID 作为基线，不返回邮件（首次运行用）。
    空邮箱场景（last_uid=0 且 baseline_only=False）会搜索 UID 1:*，确保第一封邮件不丢。
    每次最多处理 UID 最小的 10 封，保证积压时不丢邮件（未处理的会在下一轮拉取）。
    """
    conn = _connect(account)
    new_emails: list[dict] = []
    new_max_uid = last_uid

    try:
        if last_uid == 0 and baseline_only:
            status, data = conn.uid("search", "ALL")
            if status == "OK" and data[0]:
                uid_list = data[0].split()
                if uid_list:
                    new_max_uid = max(int(u) for u in uid_list)
            return [], new_max_uid

        status, data = conn.uid("search", f"UID {last_uid + 1}:*")
        if status != "OK" or not data[0]:
            return [], last_uid

        uid_list = data[0].split()
        uid_list = [u for u in uid_list if int(u) > last_uid]
        if not uid_list:
            return [], last_uid

        # ✅ 按 UID 升序，处理最旧的 10 封，new_max_uid 只推进到已处理范围
        uid_list.sort(key=lambda x: int(x))
        to_process = uid_list[:10]
        new_max_uid = max(int(u) for u in to_process)

        for uid in to_process:
            status, msg_data = conn.uid("fetch", uid, "(RFC822)")
            if status != "OK" or not msg_data or not msg_data[0]:
                continue
            raw = msg_data[0][1]
            if not isinstance(raw, bytes):
                continue
            msg = email_lib.message_from_bytes(raw)
            new_emails.append(_parse_email(msg, uid, preview_body_len, filter_body_len))
    finally:
        try:
            conn.logout()
        except Exception:
            pass

    return new_emails, new_max_uid


def imap_query_since(
    account: dict, since_dt: datetime, max_body_len: int
) -> list[dict]:
    conn = _connect(account)
    results: list[dict] = []

    try:
        since_str = since_dt.strftime("%d-%b-%Y")
        status, data = conn.uid("search", f"SINCE {since_str}")
        if status != "OK" or not data[0]:
            return []

        uid_list = data[0].split()
        for uid in uid_list[-20:]:
            status, msg_data = conn.uid("fetch", uid, "(RFC822)")
            if status != "OK" or not msg_data or not msg_data[0]:
                continue
            raw = msg_data[0][1]
            if not isinstance(raw, bytes):
                continue
            msg = email_lib.message_from_bytes(raw)
            results.append(_parse_email(msg, uid, max_body_len))
    finally:
        try:
            conn.logout()
        except Exception:
            pass

    return results
def imap_fetch_by_uid(account: dict, uid: int) -> dict:
    """按 UID 精确拉取单封邮件，正文不截断（由调用方决定展示长度）。

    供 /mail_read 命令使用。与 imap_query_since 的区别：
    - 按 UID 精确定位单封邮件，不依赖日期搜索
    - preview_body_len 传大值，正文完整返回，不在这里截断
    """
    conn = _connect(account)
    try:
        status, msg_data = conn.uid("fetch", str(uid), "(RFC822)")
        if status != "OK" or not msg_data or not msg_data[0]:
            raise ValueError(
                f"无法获取 UID {uid} 的邮件，可能已被删除或 UID 不存在"
            )
        raw = msg_data[0][1]
        if not isinstance(raw, bytes):
            raise ValueError(f"UID {uid} 返回数据格式异常")
        msg = email_lib.message_from_bytes(raw)
        # 传一个足够大的值，确保 _parse_email 不截断正文
        return _parse_email(msg, str(uid).encode(), preview_body_len=1_000_000)
    finally:
        try:
            conn.logout()
        except Exception:
            pass