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


def test_last_task_summary(db):
    s = db.get_or_create_session("u@im.wechat", "/repo")
    assert db.last_task_summary(s.id) is None            # 无任务
    db.create_task(None, s.id, "第一个任务")
    db.create_task(None, s.id, "x" * 40)                 # 最新一条，40 字截 30
    assert db.last_task_summary(s.id) == "x" * 30
    db.create_task(None, s.id, "后台长任务", kind="bg")   # 再新一条 → bg 前缀
    assert db.last_task_summary(s.id) == "[bg] 后台长任务"
    other = db.get_or_create_session("u@im.wechat", "/other")
    assert db.last_task_summary(other.id) is None        # 会话间不串


# ---- M3 媒体列 ----

def test_media_columns_on_fresh_db(db):
    """新库（_SCHEMA 直接建）与旧库（ALTER 迁移）终态一致：四列齐、幂等。"""
    cols = {r[1] for r in db._conn.execute("PRAGMA table_info(outbox)")}
    assert {"kind", "media_path", "caption"} <= cols
    mcols = {r[1] for r in db._conn.execute("PRAGMA table_info(messages)")}
    assert "media_path" in mcols
    db.ensure_schema()   # 幂等：重复执行不炸不变
    assert {"kind", "media_path", "caption"} <= {
        r[1] for r in db._conn.execute("PRAGMA table_info(outbox)")}


def test_media_columns_migrated_from_m2_db(tmp_path):
    """旧库（M2 形态，无媒体列）跑 ensure_schema 加列且数据无损。"""
    import sqlite3
    from common.db import Database
    old = tmp_path / "old.db"
    c = sqlite3.connect(old)
    c.executescript("""
      CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT,
        msg_id TEXT UNIQUE NOT NULL, from_user TEXT NOT NULL,
        text TEXT NOT NULL DEFAULT '', context_token TEXT NOT NULL DEFAULT '',
        received_at INTEGER NOT NULL, state TEXT NOT NULL DEFAULT 'received');
      CREATE TABLE outbox (id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id INTEGER REFERENCES tasks(id), to_user TEXT NOT NULL,
        text TEXT NOT NULL, seq INTEGER NOT NULL DEFAULT 0,
        state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
        max_attempts INTEGER NOT NULL DEFAULT 5, last_error TEXT,
        created_at INTEGER NOT NULL);
      INSERT INTO outbox(to_user, text, created_at) VALUES('u', '旧文本行', 1);
    """)
    c.commit()
    c.close()
    d = Database(old)
    d.ensure_schema()
    row = d._conn.execute("SELECT * FROM outbox").fetchone()
    assert row["text"] == "旧文本行" and row["kind"] == "text"   # 数据无损 + 默认值


def test_insert_message_with_media_path(db):
    from common.models import InboundMessage
    mid = db.insert_message(InboundMessage(
        msg_id="m1", from_user="u@im.wechat", text="", context_token="c",
        received_at=1, media_path="/data/media/inbound/img-x.png"))
    assert mid is not None
    row = db._conn.execute("SELECT media_path FROM messages WHERE id=?", (mid,)).fetchone()
    assert row["media_path"] == "/data/media/inbound/img-x.png"
    # 不带 media_path 的老调用兼容（默认 None → NULL）
    mid2 = db.insert_message(InboundMessage(
        msg_id="m2", from_user="u@im.wechat", text="hi", context_token="c", received_at=1))
    assert db._conn.execute(
        "SELECT media_path FROM messages WHERE id=?", (mid2,)).fetchone()["media_path"] is None


def test_enqueue_media_shape(db):
    oid = db.enqueue_media(None, "u@im.wechat", "/data/media/outbound/a.png", "看这个")
    item = db.next_outbox_batch(limit=10)[0]
    assert item.id == oid
    assert item.kind == "image"
    assert item.media_path == "/data/media/outbound/a.png"
    assert item.caption == "看这个"
    assert item.text == ""          # 媒体行 text 恒空串（caption 独立列）


def test_enqueue_text_rows_default_kind_text(db):
    db.enqueue(None, "u@im.wechat", "普通文本")
    assert db.next_outbox_batch(limit=10)[0].kind == "text"


# ---- outbox.sent_at 与日计数折算（出站熔断按页计数，M1 移交项清偿）----

def test_sent_at_migrated_from_pre_column_db(tmp_path):
    """旧库（无 sent_at 列）跑 ensure_schema 补列，历史行 NULL 不计入日计数。"""
    import sqlite3
    from common.db import Database
    old = tmp_path / "old.db"
    c = sqlite3.connect(old)
    c.executescript("""
      CREATE TABLE outbox (id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id INTEGER REFERENCES tasks(id), to_user TEXT NOT NULL,
        text TEXT NOT NULL, seq INTEGER NOT NULL DEFAULT 0,
        state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
        max_attempts INTEGER NOT NULL DEFAULT 5, last_error TEXT,
        created_at INTEGER NOT NULL, kind TEXT NOT NULL DEFAULT 'text',
        media_path TEXT, caption TEXT);
      INSERT INTO outbox(to_user, text, created_at, state)
        VALUES('u', '迁移前的历史 sent 行', 1, 'sent');
    """)
    c.commit()
    c.close()
    d = Database(old)
    d.ensure_schema()
    cols = {r[1] for r in d._conn.execute("PRAGMA table_info(outbox)")}
    assert "sent_at" in cols
    row = d._conn.execute("SELECT sent_at FROM outbox").fetchone()
    assert row["sent_at"] is None                       # 历史行无时间
    assert d.sent_pages_today(2000) == 0                # NULL 不计（当日略低估，可接受）


def test_mark_sent_stamps_and_sent_pages_today_folds(db):
    """mark_sent 写时间；折算口径=文本行分页数、图片行 caption+图。"""
    import time as _time
    # mark_sent 本身写 sent_at（非 NULL）
    db.enqueue(None, "u@im.wechat", "先用 mark_sent 的行")
    db.mark_sent(db._conn.execute("SELECT id FROM outbox").fetchone()["id"])
    assert db._conn.execute(
        "SELECT sent_at FROM outbox WHERE state='sent'").fetchone()["sent_at"]
    db._conn.execute("DELETE FROM outbox")
    db._conn.commit()
    # 3 页文本（5000 字 / limit 2000）+ 图片行带 caption（=2 条）+ 无 caption 图（=1 条）
    db.enqueue(None, "u@im.wechat", "x" * 5000)                 # id 1 → 3 页
    db.enqueue_media(None, "u@im.wechat", "/a.png", "配文")       # → 2 条
    db.enqueue_media(None, "u@im.wechat", "/b.png", "")          # → 1 条
    db.enqueue(None, "u@im.wechat", "未发送的 pending 行")        # 不计
    now = int(_time.time())
    ids = [r["id"] for r in db._conn.execute("SELECT id FROM outbox").fetchall()]
    assert len(ids) == 4
    for oid in ids[:3]:   # 前三行（文本 + 两图）置今日 sent；第四行保持 pending
        db._conn.execute(
            "UPDATE outbox SET state='sent', sent_at=? WHERE id=?", (now, oid))
    db._conn.commit()
    assert db.sent_pages_today(2000) == 3 + 2 + 1
    # 非今日（零点前）的 sent 行不计：文本行 sent_at 置 1（1970 年）
    db._conn.execute(
        "UPDATE outbox SET sent_at=1 WHERE id=?", (ids[0],))
    db._conn.commit()
    assert db.sent_pages_today(2000) == 2 + 1   # 只剩今日的两行图片


def test_sent_pages_today_folds_file_rows(db):
    """M5B 终审 #0：kind='file' 行并入 image 同支折算——媒体条 1 + 非空
    caption 1（file 行 text 恒空串，走文本分支只计 1 会低估带配文行实发 2）。"""
    import time as _time
    now = int(_time.time())
    for caption, path in (("配文", "/x/a.pdf"), ("", "/x/b.zip")):
        db._conn.execute(
            "INSERT INTO outbox(to_user, text, kind, media_path, caption, "
            "created_at, state, sent_at) VALUES(?,?,?,?,?,?,?,?)",
            ("u@im.wechat", "", "file", path, caption, now, "sent", now))
    db._conn.commit()
    assert db.sent_pages_today(2000) == 2 + 1      # 带 caption=2、无 caption=1


def test_pending_task_count(db):
    """pending/running 计数；终态不计。"""
    db.insert_message(InboundMessage(msg_id="m1", from_user="u@im.wechat",
                                     text="hi", context_token="c", received_at=1))
    s = db.get_or_create_session("u@im.wechat", "/repo")
    t1 = db.create_task(None, s.id, "a", kind="chat")
    assert db.pending_task_count(s.id) == 1
    t2 = db.create_task(None, s.id, "b", kind="chat")
    assert db.pending_task_count(s.id) == 2
    db.finish_task(t1, "done")
    assert db.pending_task_count(s.id) == 1          # done 不计
    # running 仍计
    db._conn.execute("UPDATE tasks SET state='running' WHERE id=?", (t2,))
    db._conn.commit()
    assert db.pending_task_count(s.id) == 1
    # 其他 session 不串
    s2 = db.get_or_create_session("u@im.wechat", "/other")
    assert db.pending_task_count(s2.id) == 0


def test_scan_merge_pending(db):
    """扫描 merge_pending:* KV，返回 (user, value) 列表。"""
    assert db.scan_merge_pending() == []
    db.set_state("merge_pending:a@im.wechat", '{"texts":["x"]}')
    db.set_state("merge_pending:b@im.wechat", '{"texts":["y","z"]}')
    db.set_state("other_key", "noise")              # 非 merge_pending 前缀不收
    found = dict(db.scan_merge_pending())
    assert found == {"a@im.wechat": '{"texts":["x"]}',
                     "b@im.wechat": '{"texts":["y","z"]}'}

