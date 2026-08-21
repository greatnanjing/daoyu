"""通知通道核心（M5A）：纯单向推送——写 outbox 文本行，出站协程照常投递。

不建任务、不进会话（与 M2 告警 / M4 日报同定位）。三类调用方进程：
gateway(HTTP 协程)、CLI 独立进程、approval_mcp 孙进程——取裸
sqlite3.Connection（WAL 多进程写安全；Database._conn 与 approval_mcp._conn
同为 sqlite3.Connection，零适配）。
"""
import sqlite3
import time
from typing import Iterable

PREFIX_NOTIFY = "🔔"   # 通用通知
PREFIX_DONE = "✅"      # 终端任务完成（hooks Stop）
PREFIX_ASK = "❓"       # Claude 等待确认（hooks Notification）


def format_notification(prefix: str, title: str, body: str = "") -> str:
    if body:
        return f"{prefix} {title}\n{body}"
    return f"{prefix} {title}"


def push_notification(conn: sqlite3.Connection, to_users: Iterable[str],
                      title: str, body: str = "", *, source: str,
                      prefix: str = PREFIX_NOTIFY) -> int:
    """逐用户写 outbox 文本行（task_id=None）+ audit 一行（同一 commit）。
    返回写入行数。"""
    text = format_notification(prefix, title, body)
    now = int(time.time())
    n = 0
    for user in to_users:
        conn.execute(
            "INSERT INTO outbox(task_id, to_user, text, created_at) VALUES(?,?,?,?)",
            (None, user, text, now))
        n += 1
    conn.execute(
        "INSERT INTO audit_log(ts, kind, detail) VALUES(?,?,?)",
        (now, "notify", f"{source}: {title[:40]}"))
    conn.commit()
    return n
