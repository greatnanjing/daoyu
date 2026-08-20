"""M4 主动服务：cron_jobs 表、调度判定、日报/巡检、/cron 命令。"""
import time

import pytest

from common.db import Database


def test_cron_jobs_preset(db):
    """ensure_schema 预置 daily(08:00) + patrol(10min) 两行，默认启用。"""
    jobs = {j.name: j for j in db.cron_jobs()}
    assert set(jobs) == {"daily", "patrol"}
    assert jobs["daily"].enabled == 1
    assert jobs["daily"].time_of_day == "08:00"
    assert jobs["patrol"].enabled == 1
    assert jobs["patrol"].interval_min == 10
    assert jobs["daily"].last_run_at is None


def test_update_cron_partial_and_touch(db):
    assert db.update_cron("daily", time_of_day="09:30") is True
    j = {x.name: x for x in db.cron_jobs()}["daily"]
    assert j.time_of_day == "09:30"
    assert j.enabled == 1          # 未传字段不动
    now = int(time.time())
    db.update_cron("patrol", enabled=0, touch_last_run=now)
    j = {x.name: x for x in db.cron_jobs()}["patrol"]
    assert j.enabled == 0
    assert j.last_run_at == now
    assert db.update_cron("nope", enabled=1) is False   # 未知名 False


def test_mark_cron_run(db):
    db.mark_cron_run("daily", "正常，推送 1 条")
    j = {x.name: x for x in db.cron_jobs()}["daily"]
    assert j.last_run_at is not None
    assert j.last_result == "正常，推送 1 条"


def test_daily_task_stats_window(db):
    now = int(time.time())
    s = db.get_or_create_session("u@im.wechat", "/repo")
    db.create_task(None, s.id, "a")                       # now（窗口内）
    old = db._conn.execute(
        "INSERT INTO tasks(message_id, session_id, prompt, kind, state, attempts,"
        " max_attempts, created_at, updated_at) VALUES(NULL,?, 'old', 'chat',"
        " 'done', 0, 3, ?, ?)", (s.id, now - 90000, now - 90000))
    db._conn.commit()
    tid = db.create_task(None, s.id, "will-dead")
    db._conn.execute("UPDATE tasks SET state='dead' WHERE id=?", (tid,))
    db._conn.commit()
    stats = db.daily_task_stats(now - 3600, now + 60)
    assert stats["total"] == 2          # 窗口只含 a 与 will-dead
    assert stats["dead"] == 1
    assert "done" not in stats or stats.get("done", 0) == 0   # old 落窗外


def test_daily_cost_and_sent_count(db):
    now = int(time.time())
    db.audit("cost", '{"task_id": 1, "usd": 0.5}')
    db.audit("cost", '{"task_id": 2, "usd": 0.25}')
    db.audit("cost", "not-json")        # 坏行不计不炸
    db.enqueue(None, "u@im.wechat", "hello")
    row = db._conn.execute("SELECT id FROM outbox LIMIT 1").fetchone()
    db.mark_sent(row["id"])
    assert db.daily_cost(now - 60, now + 60) == pytest.approx(0.75)
    assert db.outbox_sent_count(now - 60, now + 60) == 1


def test_create_fixed_session_idempotent(db):
    from common.models import SessionBinding
    b = db.create_fixed_session("u@im.wechat", "/repo", "0da0f00d-0f00-4000-8000-00000000000d")
    assert isinstance(b, SessionBinding)
    assert b.claude_uuid == "0da0f00d-0f00-4000-8000-00000000000d"
    # 重复 uuid 不炸（INSERT OR IGNORE）且返回既有行
    b2 = db.create_fixed_session("u@im.wechat", "/repo",
                                 "0da0f00d-0f00-4000-8000-00000000000d")
    assert b2.id == b.id
    # 不动当前话题指针
    assert db.get_state("active_session:u@im.wechat") is None
