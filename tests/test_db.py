import json
import time

import pytest

from common.models import InboundMessage


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


def _msg(n, token="tokA"):
    return InboundMessage(msg_id=str(n), from_user="u@im.wechat", text=f"hi{n}",
                          context_token=token, received_at=1000)


def test_insert_message_dedup(db):
    assert db.insert_message(_msg(1)) is not None      # 新消息 → id
    assert db.insert_message(_msg(1)) is None          # 同 msg_id → None（去重）
    assert db.insert_message(_msg(2)) is not None


def test_latest_context_token(db):
    db.insert_message(_msg(1, token="t1"))
    db.insert_message(_msg(2, token="t2"))
    assert db.latest_context_token("u@im.wechat") == "t2"


def test_get_or_create_session(db):
    s1 = db.get_or_create_session("u@im.wechat", "/repo")
    s2 = db.get_or_create_session("u@im.wechat", "/repo")   # 幂等
    assert s1.id == s2.id and s1.claude_uuid == s2.claude_uuid
    s3 = db.get_or_create_session("u@im.wechat", "/other")  # 不同 cwd → 新会话
    assert s3.id != s1.id and s3.claude_uuid != s1.claude_uuid
    assert s1.policy == "auto"

    db.set_policy(s1.id, "strict")
    assert db.get_session(s1.id).policy == "strict"
    assert len(db.list_sessions("u@im.wechat")) == 2


def test_active_cwd_pointer(db):
    db.set_active_cwd("u@im.wechat", "/repo")
    assert db.get_active_cwd("u@im.wechat", "/dft") == "/repo"
    assert db.get_active_cwd("other@im.wechat", "/dft") == "/dft"
