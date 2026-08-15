import json
import time

import pytest


def test_ensure_schema_idempotent(db, tmp_path):
    db.ensure_schema()  # 二次调用不报错
    names = {r[0] for r in db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"messages", "tasks", "outbox", "sessions", "audit_log", "state"} <= names


def test_wal_mode(db, tmp_path):
    mode = db._conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode == "wal"


def test_state_kv_roundtrip(db):
    assert db.get_state("missing", "dft") == "dft"
    db.set_state("bot_token", "T1")
    assert db.get_state("bot_token") == "T1"
    db.set_state("bot_token", "T2")  # 覆盖
    assert db.get_state("bot_token") == "T2"


def test_audit_and_cost(db):
    db.audit("cost", json.dumps({"task_id": 1, "usd": 0.42}))
    assert db.today_cost_usd() == pytest.approx(0.42)
    db.audit("cost", json.dumps({"task_id": 2, "usd": 0.08}))
    assert db.today_cost_usd() == pytest.approx(0.50)


def test_queue_depth(db):
    assert db.queue_depth() == 0
    # session/create_task 接口在后续任务实现，这里直接走 SQL 造数
    now = int(time.time())
    cur = db._conn.execute(
        "INSERT INTO sessions(wechat_user, cwd, claude_uuid, created_at, last_active_at) "
        "VALUES(?,?,?,?,?)", ("u@im.wechat", "/repo", "uuid-x", now, now))
    db._conn.execute(
        "INSERT INTO tasks(session_id, prompt, created_at, updated_at) VALUES(?,?,?,?)",
        (cur.lastrowid, "x", now, now))
    db._conn.commit()
    assert db.queue_depth() == 1
