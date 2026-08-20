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


_SAMPLE_OK = {"cpu": 23.0, "mem": 61.0, "disks": {"/": 42.0}, "boot_days": 12.3}


def _dcfg(tmp_path):
    """最小 Config 替身（scheduler 只读 cron/default_cwd/whitelist/repo_root）。"""
    from common.config import _DEFAULT_CRON
    from types import SimpleNamespace
    return SimpleNamespace(cron=dict(_DEFAULT_CRON), default_cwd="/repo",
                           whitelist={"u@im.wechat"}, repo_root=tmp_path)


def test_render_daily_report():
    from gateway.scheduler import render_daily_report
    data = {"date": "2026-08-21", "tasks": {"done": 4, "canceled": 1, "dead": 0,
                                            "total": 5},
            "cost_usd": 0.83, "cpu": 23.0, "mem": 61.0, "disks": {"/": 42.0},
            "boot_days": 12.3, "sent": 32, "backlog": 0, "dead_outbox": 0,
            "online": True, "media_mb": 128.4}
    text = render_daily_report(data)
    assert "🌅 刀鱼日报 2026-08-21" in text
    assert "成功 4 / 取消 1 / 死信 0" in text
    assert "$0.83" in text
    assert "CPU 23%" in text and "磁盘" in text
    assert "出站 32 条" in text and "连接正常" in text


def test_daily_anomalies():
    from gateway.scheduler import daily_anomalies
    from common.config import _DEFAULT_CRON
    ok = {"tasks": {"dead": 0}, "disks": {"/": 42.0}, "cpu": 23.0, "mem": 61.0,
          "backlog": 0, "online": True}
    assert daily_anomalies(ok, _DEFAULT_CRON) == []
    bad = {"tasks": {"dead": 2}, "disks": {"/": 91.0}, "cpu": 95.0, "mem": 50.0,
           "backlog": 0, "online": True}
    got = daily_anomalies(bad, _DEFAULT_CRON)
    # brief 原文断言 len==2，但其 bad 夹具 cpu=95 已超阈（90）→ 实测 3 项
    # （死信 / 磁盘 / CPU），与 brief 自身实现（逐分支超阈判定）矛盾，
    # 实测失败后按语义修正并补 CPU 存在性断言。
    assert len(got) == 3 and any("死信" in g for g in got) and any("/" in g for g in got)
    assert any("CPU" in g for g in got)


def test_run_daily_normal(db, tmp_path):
    from gateway.scheduler import run_daily
    cfg = _dcfg(tmp_path)
    # brief 原文缺此行：online 判定读 state bot_token（login/reconnect 写入），
    # 空测试库无 token → online=False 恒走异常分支。正常轮次前提是已登录。
    db.set_state("bot_token", "fake-token")
    result = run_daily(db, cfg, _ANCHOR, dict(_SAMPLE_OK))
    assert "正常" in result
    rows = db._conn.execute(
        "SELECT to_user, text FROM outbox WHERE text LIKE '%日报%'").fetchall()
    assert rows and rows[0]["to_user"] == "u@im.wechat"
    assert "⏳" not in rows[0]["text"]
    # 正常轮次零 Claude 调用：无新任务、无 cost 行
    assert db._conn.execute("SELECT COUNT(*) c FROM tasks").fetchone()["c"] == 0


def test_run_daily_anomaly_escalates(db, tmp_path):
    from gateway.scheduler import run_daily, OPS_UUID
    cfg = _dcfg(tmp_path)
    s = db.get_or_create_session("u@im.wechat", "/repo")
    tid = db.create_task(None, s.id, "will-dead")
    db._conn.execute("UPDATE tasks SET state='dead' WHERE id=?", (tid,))
    db._conn.commit()
    sample = dict(_SAMPLE_OK, disks={"/": 91.0})
    result = run_daily(db, cfg, _ANCHOR, sample)
    assert "异常" in result
    text = db._conn.execute(
        "SELECT text FROM outbox WHERE text LIKE '%日报%'").fetchone()["text"]
    assert "⏳" in text
    # 分析任务挂 ops 话题
    row = db._conn.execute(
        "SELECT t.id, t.session_id FROM tasks t JOIN sessions s ON t.session_id=s.id "
        "WHERE s.claude_uuid=?", (OPS_UUID,)).fetchone()
    assert row is not None


from collections import deque


def test_check_patrol_items(db, tmp_path):
    from gateway.scheduler import check_patrol
    cfg = _dcfg(tmp_path)
    now = _ANCHOR
    # brief 原文缺此行（Task 4 test_run_daily_normal 同款笔误）：ilink_token
    # 判定读 state bot_token，空测试库无 token → 首断言 == [] 与磁盘断言
    # ["disk:/"] 恒被 ilink_token 项击穿。「正常无告警」前提是已登录；
    # 中段再摘 token 验证 brief 注释「只剩 token 缺失一项」的原语义。
    db.set_state("bot_token", "T")
    # 正常采样 + 窗口低位 → 无告警
    ok = check_patrol(db, cfg, now, dict(_SAMPLE_OK), deque([20.0] * 5), deque([60.0] * 5))
    assert ok == []
    # 磁盘超阈
    bad = dict(_SAMPLE_OK, disks={"/": 91.0})
    got = check_patrol(db, cfg, now, bad, deque([20.0] * 5), deque([60.0] * 5))
    assert [a["key"] for a in got] == ["disk:/"]
    # CPU 连续 5 采样超阈才告警；4 个不够
    got = check_patrol(db, cfg, now, dict(_SAMPLE_OK),
                       deque([95.0] * 5), deque([60.0] * 5))
    assert "cpu" in [a["key"] for a in got]
    got = check_patrol(db, cfg, now, dict(_SAMPLE_OK),
                       deque([95.0] * 4), deque([60.0] * 5))
    assert "cpu" not in [a["key"] for a in got]
    # 队列积压：造 pending 任务（先摘 token，下断言才「只剩 token 缺失一项」）
    db.delete_state("bot_token")
    s = db.get_or_create_session("u@im.wechat", "/repo")
    db.create_task(None, s.id, "queued")
    got = check_patrol(db, cfg, now, dict(_SAMPLE_OK),
                       deque([20.0] * 5), deque([60.0] * 5))
    # backlog=1 未超 queue_backlog_warn=20 → 只剩 token 缺失一项
    assert [a["key"] for a in got] == ["ilink_token"]
    # token 在线后无告警
    db.set_state("bot_token", "T")
    assert check_patrol(db, cfg, now, dict(_SAMPLE_OK),
                        deque([20.0] * 5), deque([60.0] * 5)) == []


def test_check_certs(tmp_path):
    from gateway.scheduler import check_certs
    from common.config import _DEFAULT_CRON
    import datetime
    from types import SimpleNamespace
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    # 自签一张 7 天后到期的证书（< 预警 14 天）
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now_u = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "t")]))
            .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "t")]))
            .public_key(key.public_key())
            .not_valid_before(now_u - datetime.timedelta(days=1))
            .not_valid_after(now_u + datetime.timedelta(days=7))
            .serial_number(x509.random_serial_number()).sign(key, hashes.SHA256()))
    live = tmp_path / "letsencrypt" / "live" / "a"
    live.mkdir(parents=True)
    (live / "fullchain.pem").write_bytes(
        cert.public_bytes(serialization.Encoding.PEM))
    cfg = SimpleNamespace(cron=dict(_DEFAULT_CRON, cert_paths=[str(tmp_path / "letsencrypt" / "live")]))
    got = check_certs(cfg, int(now_u.timestamp()))
    # 注入 now = now_u（造证书基准）消除墙钟漂移：not_valid_after = now_u + 7d
    # 整 → 差恰 7 天、.days 恒 7（旧 6/7 容忍是墙钟漂移产物——now_u 读出后
    # 有 RSA keygen+签名 ~0.8s 耗时把 7 天差折为 6；注入后无需容忍）。
    assert len(got) == 1 and got[0]["key"].startswith("cert:") and (
        "剩余 7 天" in got[0]["lines"][0])
    # 路径不存在 → 空（Windows 开发机不误报）
    cfg2 = SimpleNamespace(cron=dict(_DEFAULT_CRON, cert_paths=["/no/such/dir"]))
    assert check_certs(cfg2, int(time.time())) == []


def test_silence_window(db):
    from gateway.scheduler import mark_alert, silenced
    now = _ANCHOR
    assert silenced(db, "disk:/", 6 * 3600, now) is False   # 从未告警
    mark_alert(db, "disk:/", now)
    assert silenced(db, "disk:/", 6 * 3600, now + 3600) is True    # 静默期内
    assert silenced(db, "disk:/", 6 * 3600, now + 6 * 3600 + 1) is False  # 过期重报


def test_run_patrol_alert_and_silence(db, tmp_path):
    from gateway.scheduler import run_patrol, OPS_UUID
    cfg = _dcfg(tmp_path)
    db.set_state("bot_token", "T")
    bad = dict(_SAMPLE_OK, disks={"/": 91.0})
    r1 = run_patrol(db, cfg, _ANCHOR, bad, deque([20.0] * 5), deque([60.0] * 5))
    assert "告警" in r1 and "分析任务" in r1
    # 告警行 + 分析任务（ops 话题）
    assert db._conn.execute(
        "SELECT COUNT(*) c FROM outbox WHERE text LIKE '%巡检告警%'"
    ).fetchone()["c"] == 1
    row = db._conn.execute(
        "SELECT t.id FROM tasks t JOIN sessions s ON t.session_id=s.id "
        "WHERE s.claude_uuid=?", (OPS_UUID,)).fetchone()
    assert row is not None
    # 静默期内第二轮：不重报不重建
    r2 = run_patrol(db, cfg, _ANCHOR + 300, bad, deque([20.0] * 5), deque([60.0] * 5))
    assert "静默" in r2
    assert db._conn.execute(
        "SELECT COUNT(*) c FROM outbox WHERE text LIKE '%巡检告警%'"
    ).fetchone()["c"] == 1
    # 静默期过后仍异常 → 再报
    r3 = run_patrol(db, cfg, _ANCHOR + 6 * 3600 + 60, bad,
                    deque([20.0] * 5), deque([60.0] * 5))
    assert "告警" in r3


def test_scheduler_loop_dispatch(db, tmp_path, monkeypatch):
    """一轮分发：daily/patrol 到点各自触发一次并落 last_result；未到点不动。"""
    import gateway.scheduler as sch
    cfg = _dcfg(tmp_path)
    calls = []
    # brief 原文缺此行：daily 预设 08:00 而 _tick 以真实墙钟判定，凌晨跑测试
    # （now < 今日 08:00）daily 恒不 due → 首断言必挂。改 00:00 使今日时刻恒
    # 已过，断言语义（预置行从未跑 → 双双 due）不变。
    db.update_cron("daily", time_of_day="00:00")

    def fake_run_daily(d, c, now, sample):
        calls.append("daily"); return "日报OK"

    def fake_run_patrol(d, c, now, sample, cw, mw):
        calls.append("patrol"); return "巡检OK"

    monkeypatch.setattr(sch, "run_daily", fake_run_daily)
    monkeypatch.setattr(sch, "run_patrol", fake_run_patrol)
    monkeypatch.setattr(sch, "psutil_sample", lambda cfg: dict(_SAMPLE_OK))

    async def one_round():
        await sch._tick(db, cfg)   # 单轮内联：scheduler_loop 的每分钟体

    import asyncio
    asyncio.get_event_loop_policy()
    asyncio.run(one_round())
    assert calls == ["daily", "patrol"]        # 预置行从未跑 → 双双 due
    j = {x.name: x for x in db.cron_jobs()}
    assert j["daily"].last_result == "日报OK"
    assert j["patrol"].last_result == "巡检OK"
    # 第二轮：patrol 间隔未满、daily 今日已跑 → 都不动
    calls.clear()
    asyncio.run(one_round())
    assert calls == []
    # off 后即便到点也不跑
    db.update_cron("patrol", enabled=0)
    db.update_cron("patrol", touch_last_run=_ANCHOR - 99999)
    calls.clear()
    asyncio.run(one_round())
    assert calls == []
