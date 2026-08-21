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


# ---- CLI（env 覆盖 DAOYU_DB/DAOYU_WHITELIST，完全不碰实例 config.json）----

from gateway import notify_cli


def test_cli_push(monkeypatch, db, capsys):
    monkeypatch.setenv("DAOYU_DB", db.path)
    monkeypatch.setenv("DAOYU_WHITELIST", "u@im.wechat")
    rc = notify_cli.main(["部署完成", "耗时", "3 分钟"])
    assert rc == 0
    row = db._conn.execute("SELECT text FROM outbox").fetchone()
    assert row["text"] == "🔔 部署完成\n耗时 3 分钟"   # 正文多段空格拼接
    assert "已推送 1 位用户" in capsys.readouterr().out


def test_cli_missing_title_exits_2(monkeypatch):
    monkeypatch.setenv("DAOYU_DB", "x")
    monkeypatch.setenv("DAOYU_WHITELIST", "u")
    with pytest.raises(SystemExit) as ei:
        notify_cli.main([])
    assert ei.value.code == 2


def test_cli_db_unreachable_exit_1(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DAOYU_DB", str(tmp_path / "no" / "such" / "db.sqlite"))
    monkeypatch.setenv("DAOYU_WHITELIST", "u@im.wechat")
    rc = notify_cli.main(["标题"])
    assert rc == 1                                    # stderr 一行，不静默
    assert "失败" in capsys.readouterr().err


def test_cli_hook_stop_formats_cwd(monkeypatch, db):
    monkeypatch.setenv("DAOYU_DB", db.path)
    monkeypatch.setenv("DAOYU_WHITELIST", "u@im.wechat")
    monkeypatch.setattr("sys.stdin", io.StringIO(
        json.dumps({"cwd": "/home/user/repo", "hook_event_name": "Stop"})))
    rc = notify_cli.main(["--hook", "stop"])
    assert rc == 0
    row = db._conn.execute("SELECT text FROM outbox").fetchone()
    assert row["text"] == "✅ 终端任务完成\n📁 /home/user/repo"


def test_cli_hook_notification_message(monkeypatch, db):
    monkeypatch.setenv("DAOYU_DB", db.path)
    monkeypatch.setenv("DAOYU_WHITELIST", "u@im.wechat")
    monkeypatch.setattr("sys.stdin", io.StringIO(
        json.dumps({"message": "需要允许 Bash 执行"})))
    rc = notify_cli.main(["--hook", "notification"])
    assert rc == 0
    row = db._conn.execute("SELECT text FROM outbox").fetchone()
    assert row["text"] == "❓ Claude 等待确认\n需要允许 Bash 执行"


def test_cli_hook_bad_json_degrades(monkeypatch, db):
    """hooks stdin 非 JSON：整段截 200 字作正文照推（不阻塞宿主会话流）。"""
    monkeypatch.setenv("DAOYU_DB", db.path)
    monkeypatch.setenv("DAOYU_WHITELIST", "u@im.wechat")
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    rc = notify_cli.main(["--hook", "stop"])
    assert rc == 0
    row = db._conn.execute("SELECT text FROM outbox").fetchone()
    assert row["text"] == "✅ 终端事件\nnot json"
