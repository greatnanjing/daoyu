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


def test_config_cron_defaults():
    """实例 config.json 无 cron 节时给全默认（config.example.json 同构）。"""
    from common.config import _DEFAULT_CRON
    assert _DEFAULT_CRON["disk_threshold_pct"] == 85
    assert _DEFAULT_CRON["load_sustain_min"] == 5
    assert _DEFAULT_CRON["cert_paths"] == ["/etc/letsencrypt/live"]
    assert _DEFAULT_CRON["alert_silence_h"] == 6
    assert _DEFAULT_CRON["queue_backlog_warn"] == 20


# ---- 调度判定（时间注入；固定锚点 2026-08-21 10:30 本地）----
_ANCHOR = int(time.mktime((2026, 8, 21, 10, 30, 0, 0, 0, -1)))


def _job(**kw):
    from common.models import CronJob
    base = dict(id=1, name="daily", enabled=1, time_of_day="08:00",
                interval_min=None, last_run_at=None, last_result=None)
    base.update(kw)
    return CronJob(**base)


def test_due_daily():
    from gateway.scheduler import due_daily
    # 今日 08:00 已过、从未跑 → due
    assert due_daily(_job(), _ANCHOR) is True
    # 上次跑在今日 08:00 之后 → 今日已跑，不 due
    assert due_daily(_job(last_run_at=_ANCHOR), _ANCHOR) is False
    # 还没到点（07:00 时刻）→ 不 due
    early = _ANCHOR - 3 * 3600
    assert due_daily(_job(), early) is False
    # 禁用恒不 due
    assert due_daily(_job(enabled=0), _ANCHOR) is False


def test_due_patrol():
    from gateway.scheduler import due_patrol
    p = dict(id=2, name="patrol", enabled=1, time_of_day=None, interval_min=10)
    # 从未跑（last_run_at=None）→ 立即 due（首轮建立基线）
    assert due_patrol(_job(**p), _ANCHOR) is True
    # 5 分钟前跑过、间隔 10 → 未到
    assert due_patrol(_job(**p, last_run_at=_ANCHOR - 300), _ANCHOR) is False
    # 11 分钟前跑过 → due
    assert due_patrol(_job(**p, last_run_at=_ANCHOR - 660), _ANCHOR) is True


def test_next_run_time():
    from gateway.scheduler import next_run_time
    early = _ANCHOR - 3 * 3600   # 07:30：daily 下次 = 今日 08:00
    assert next_run_time(_job(), early) == early + 1800
    # 明日 08:00 = 锚点 10:30 + 21.5h；brief 原文 16*3600+1800（16.5h=明日 03:00）
    # 与其注释「明日 08:00」及实现（ts+=86400）矛盾，实测失败后按语义修正。
    assert next_run_time(_job(), _ANCHOR) == _ANCHOR + 21 * 3600 + 1800
    assert next_run_time(_job(enabled=0), _ANCHOR) is None
    p = dict(id=2, name="patrol", enabled=1, time_of_day=None, interval_min=10)
    assert next_run_time(_job(**p, last_run_at=_ANCHOR - 300), _ANCHOR) == _ANCHOR + 300


class _FakeCfg:
    def __init__(self):
        self.reconnect = {"session_duration_s": 86400}
        self.default_cwd = "/repo"
        self.throttle = {"page_char_limit": 2000}


def _route(cmd, args=""):
    from gateway.router import Route
    return Route(kind="bridge", command=cmd, args=args, detail={})


async def test_cron_cmd(db):
    from gateway.bridge import execute_bridge
    # 列表（无参 = list）
    r = await execute_bridge(db, None, _route("cron"), "u@im.wechat", _FakeCfg())
    assert "daily" in r and "patrol" in r and "08:00" in r
    # off / on
    r = await execute_bridge(db, None, _route("cron", "off patrol"), "u@im.wechat", _FakeCfg())
    assert "已暂停" in r
    j = {x.name: x for x in db.cron_jobs()}["patrol"]
    assert j.enabled == 0
    r = await execute_bridge(db, None, _route("cron", "on patrol"), "u@im.wechat", _FakeCfg())
    assert "已开启" in r
    # time / interval
    r = await execute_bridge(db, None, _route("cron", "time daily 09:30"),
                             "u@im.wechat", _FakeCfg())
    assert "09:30" in r
    assert {x.name: x for x in db.cron_jobs()}["daily"].time_of_day == "09:30"
    r = await execute_bridge(db, None, _route("cron", "interval patrol 15"),
                             "u@im.wechat", _FakeCfg())
    assert "15" in r
    # 非法参数回用法
    r = await execute_bridge(db, None, _route("cron", "time daily 25:99"),
                             "u@im.wechat", _FakeCfg())
    assert "HH:MM" in r
    r = await execute_bridge(db, None, _route("cron", "bogus"), "u@im.wechat", _FakeCfg())
    assert "用法" in r


def test_router_cron_bridge():
    from gateway.router import route
    assert route("/cron", set()).kind == "bridge"
