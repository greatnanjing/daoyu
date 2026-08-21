"""M5A 通知通道：核心 format/push（写 outbox+audit）+ CLI + hooks 模式。"""
import io
import json

import pytest

from common.notify import (PREFIX_ASK, PREFIX_DONE, PREFIX_NOTIFY,
                           format_notification, push_notification)


def test_format_notification():
    assert format_notification(PREFIX_NOTIFY, "标题") == "🔔 标题"
    assert format_notification(PREFIX_NOTIFY, "标题", "正文") == "🔔 标题\n正文"
    assert format_notification(PREFIX_DONE, "终端任务完成", "📁 /repo") == \
        "✅ 终端任务完成\n📁 /repo"
    assert PREFIX_ASK == "❓"


def test_push_notification_multi_user(db):
    n = push_notification(db._conn, ["a@im.wechat", "b@im.wechat"],
                          "部署完成", "耗时 3 分钟", source="cli")
    assert n == 2
    rows = db._conn.execute(
        "SELECT to_user, task_id, text FROM outbox").fetchall()
    assert {(r["to_user"], r["task_id"]) for r in rows} == \
        {("a@im.wechat", None), ("b@im.wechat", None)}
    assert all(r["text"] == "🔔 部署完成\n耗时 3 分钟" for r in rows)


def test_push_notification_audit(db):
    push_notification(db._conn, ["a@im.wechat"], "部署完成", source="cli")
    push_notification(db._conn, ["a@im.wechat"], "标" * 60, source="http")
    rows = db._conn.execute(
        "SELECT detail FROM audit_log WHERE kind='notify' ORDER BY ts").fetchall()
    assert rows[0]["detail"] == "cli: 部署完成"
    assert rows[1]["detail"] == "http: " + "标" * 40   # 标题截 40 字


def test_push_notification_custom_prefix(db):
    push_notification(db._conn, ["a@im.wechat"], "终端任务完成", "📁 /repo",
                      source="hook:stop", prefix=PREFIX_DONE)
    row = db._conn.execute("SELECT text FROM outbox").fetchone()
    assert row["text"] == "✅ 终端任务完成\n📁 /repo"
