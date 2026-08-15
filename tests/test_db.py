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


def _session(db):
    return db.get_or_create_session("u@im.wechat", "/repo")


def test_task_lifecycle(db):
    s = _session(db)
    t1 = db.create_task(None, s.id, "/review", kind="command")
    t2 = db.create_task(None, s.id, "hello", kind="chat")
    assert db.pending_sessions() == [s.id]

    got = db.claim_next_pending({s.id})
    assert got.id == t1 and got.state == "running" and got.attempts == 1
    assert [t.id for t in db.active_tasks()] == [t1, t2]  # running+pending 都算活跃

    db.finish_task(t1, "done")
    got2 = db.claim_next_pending({s.id})
    assert got2.id == t2
    db.finish_task(t2, "failed")          # attempts=1 < max 3 → 回 pending 重试
    assert db.get_task(t2).state == "pending"


def test_task_retry_exhausted_to_dead(db):
    s = _session(db)
    t = db.create_task(None, s.id, "x", max_attempts=2)
    for _ in range(2):
        got = db.claim_next_pending({s.id})
        db.finish_task(t, "failed")
    assert db.get_task(t).state == "dead"


def test_reset_running_tasks_recovery(db):
    s = _session(db)
    db.create_task(None, s.id, "a")
    got = db.claim_next_pending({s.id})
    assert got.state == "running"
    n = db.reset_running_tasks()          # 模拟崩溃后重启
    assert n == 1 and db.get_task(got.id).state == "pending"


def test_cancel_pending_task(db):
    s = _session(db)
    t = db.create_task(None, s.id, "a")
    assert db.cancel_task(t) is True
    assert db.get_task(t).state == "canceled"
    assert db.claim_next_pending({s.id}) is None
    t2 = db.create_task(None, s.id, "b")
    db.claim_next_pending({s.id})         # running
    assert db.cancel_task(t2) is False    # running 的取消走 pool.kill，DB 不直接改


def test_outbox_state_machine(db):
    oid = db.enqueue(None, "u@im.wechat", "回复1")
    assert db.enqueue(None, "u@im.wechat", "回复2") == oid + 1
    batch = db.next_outbox_batch()
    assert [o.text for o in batch] == ["回复1", "回复2"]
    assert batch[0].attempts == 1

    db.mark_sent(batch[0].id)
    db.mark_send_failed(batch[1].id, "HTTP 500")
    assert db.get_outbox(batch[0].id).state == "sent"
    assert db.get_outbox(batch[1].id).state == "pending"   # 1 < 5 次，回 pending

    o = db.get_outbox(batch[1].id)
    for _ in range(4):                                     # 凑满 5 次
        b = db.next_outbox_batch()
        db.mark_send_failed(b[0].id, "again")
    assert db.get_outbox(batch[1].id).state == "dead"
    assert db.dead_letter_count() == 1


def test_retry_failed_outbox_on_startup(db):
    oid = db.enqueue(None, "u", "m")
    b = db.next_outbox_batch()
    db.mark_send_failed(b[0].id, "x")
    # failed→pending 转换在 mark_send_failed 内完成（未死信则回 pending），
    # 启动恢复只需重投 pending
    assert db.get_outbox(oid).state == "pending"


def test_retry_failed_outbox_recovers_failed_state(db):
    # 兜底：mark_send_failed 从不留 failed 态，但若 DB 中存在 failed
    # （如手工置入/未来路径），启动恢复 retry_failed_outbox 将其重置为 pending
    oid = db.enqueue(None, "u", "m")
    db._conn.execute("UPDATE outbox SET state='failed' WHERE id=?", (oid,))
    db._conn.commit()
    assert db.retry_failed_outbox() == 1
    assert db.get_outbox(oid).state == "pending"
    assert db.retry_failed_outbox() == 0  # 无 failed 可恢复
