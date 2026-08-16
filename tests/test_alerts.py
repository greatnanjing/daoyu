"""M2 监控告警：4 个触发点各自把 ⚠️ 消息复用出站通道推给全部白名单用户。

触发点：
1. outbox 条目转 dead（outbound 死信路径）
2. 日限熔断（每个熔断周期一次，随 daily_limit audit 一起）
3. 任务因 error_max_turns / error_max_budget_usd 死信（runner）
4. poll_loop 连续 401/403 清 token（连接失效，自动重连提示）
"""
import asyncio
import contextlib
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

from common.models import Budget, InboundMessage
from gateway.outbound import OutboundLoop
from worker.runner import TaskRunner

_orig_sleep = asyncio.sleep


class FakeILink:
    """成功发送的最小 fake（告警须能走同一条出站通道真正送达）。"""

    def __init__(self):
        self.sent = []   # (to_user, text)

    async def sendmessage(self, to_user, context_token, text, token=None, base_url=None):
        self.sent.append((to_user, text))
        return True

    async def getconfig(self, ilink_user_id, context_token, token=None, base_url=None):
        return "TICKET" if context_token else ""

    async def sendtyping(self, *a, **k):
        pass


class FakeCfg:
    def __init__(self):
        self.throttle = {"min_send_interval_s": 0.0, "page_char_limit": 2000,
                         "daily_send_limit": 500, "progress_window_s": 0.0}
        self.whitelist = {"u@im.wechat"}


class RunnerCfg:
    """TaskRunner 的 config 契约（形如真实 load_config）+ whitelist。"""

    def __init__(self, tmp_path):
        self.claude_bin = [sys.executable, "-c", "import sys; sys.stdin.read()"]
        self.secrets = {}
        self.repo_root = tmp_path
        self.throttle = {"progress_window_s": 0.0, "page_char_limit": 2000}
        self.budget = Budget(max_turns=10, max_usd=1.0)
        self.whitelist = {"u@im.wechat"}


def common_msg(user, token, msg_id="1"):
    return InboundMessage(msg_id=msg_id, from_user=user, text="hi",
                          context_token=token, received_at=1)


async def wait_until(pred, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        await asyncio.sleep(0.05)
    return False


def _alert_rows(db):
    """outbox 中的告警行（⚠️ 开头），按 id 序。"""
    rows = db._conn.execute(
        "SELECT * FROM outbox WHERE text LIKE '⚠️%' ORDER BY id").fetchall()
    return [db.get_outbox(r["id"]) for r in rows]


async def test_dead_letter_enqueues_alert_to_whitelist(db):
    # 触发点 1：无入站历史用户（拿不到 context_token）→ 5 次尝试全败 → 死信
    # → 白名单每人一条 ⚠️，且告警本身经同一条出站通道真正送达。
    il = FakeILink()
    db.insert_message(common_msg("u@im.wechat", "CTX"))
    loop = OutboundLoop(db, il, FakeCfg(),
                        token_ref={"token": "T", "base_url": ""}, typing_state={})
    db.enqueue(None, "ghost@im.wechat", "要紧的内容")
    task = asyncio.create_task(loop.run_forever())
    try:
        assert await wait_until(lambda: db.get_outbox(1).state == "dead")
        assert await wait_until(lambda: _alert_rows(db))
        alert = _alert_rows(db)[0]
        assert alert.to_user == "u@im.wechat"
        assert alert.text.startswith("⚠️ 出站死信（id=1）")
        assert "要紧的内容" in alert.text        # 含死信内容预览
        assert await wait_until(lambda: _alert_rows(db)[0].state == "sent")
        assert ("u@im.wechat", alert.text) in il.sent
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_dead_letter_alert_does_not_realert_on_itself(db):
    # 防自激：系统性发送故障下告警自己也会死信——不得再对告警产生新告警
    # （⚠️ → 死信 → ⚠️ → 死信 → … 无限循环会刷爆 outbox/audit）。
    class AlwaysFailILink(FakeILink):
        async def sendmessage(self, to_user, context_token, text,
                              token=None, base_url=None):
            return False

    il = AlwaysFailILink()
    db.insert_message(common_msg("u@im.wechat", "CTX"))
    loop = OutboundLoop(db, il, FakeCfg(),
                        token_ref={"token": "T", "base_url": ""}, typing_state={})
    db.enqueue(None, "ghost@im.wechat", "m")
    task = asyncio.create_task(loop.run_forever())
    try:
        # 原始消息死信 → 告警入 outbox；告警再 5 次失败也死信
        assert await wait_until(lambda: db.get_outbox(1).state == "dead")
        assert await wait_until(lambda: _alert_rows(db)
                                and _alert_rows(db)[0].state == "dead")
        await asyncio.sleep(1.5)                 # 跨多轮 drain，给潜在的自激留时间
        assert len(_alert_rows(db)) == 1         # 只有第一条告警，无连锁
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_daily_limit_breaker_alerts_once(db):
    # 触发点 2：日限=1，第 1 条送达后第 2 条触发熔断 → audit 与 ⚠️ 各恰一次
    # （循环 0.5s 一轮，不逐轮刷屏）；熔断期间 outbox 保持 pending 不投。
    il = FakeILink()
    cfg = FakeCfg()
    cfg.throttle["daily_send_limit"] = 1
    db.insert_message(common_msg("u@im.wechat", "CTX"))
    loop = OutboundLoop(db, il, cfg,
                        token_ref={"token": "T", "base_url": ""}, typing_state={})
    db.enqueue(None, "u@im.wechat", "m1")
    task = asyncio.create_task(loop.run_forever())
    try:
        assert await wait_until(lambda: db.get_outbox(1).state == "sent")
        db.enqueue(None, "u@im.wechat", "m2")    # 第 1 条已出 → 本轮起熔断
        assert await wait_until(lambda: _alert_rows(db))
        await asyncio.sleep(1.2)                 # 跨 ~3 轮 drain
        rows = db._conn.execute(
            "SELECT detail FROM audit_log WHERE kind='daily_limit'").fetchall()
        assert len(rows) == 1
        alerts = _alert_rows(db)
        assert len(alerts) == 1
        assert alerts[0].to_user == "u@im.wechat"
        assert "上限" in alerts[0].text
        assert db.get_outbox(2).state == "pending"   # 熔断：不被 claim、不空耗
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_runner_budget_dead_alerts_whitelist(db, tmp_path):
    # 触发点 3：error_max_budget_usd → 任务死信不重试 → ⚠️ 含任务号与截断原因。
    cfg = RunnerCfg(tmp_path)
    result_line = json.dumps({"type": "result", "subtype": "error_max_budget_usd",
                              "result": "Budget limit of $1.00 exceeded",
                              "total_cost_usd": 1.0, "is_error": True})
    budget_claude = tmp_path / "budget_claude.py"
    budget_claude.write_text("import sys\nsys.stdin.read()\n"
                             f"print({result_line!r}, flush=True)\n"
                             "sys.exit(1)\n", encoding="utf-8")
    cfg.claude_bin = [sys.executable, str(budget_claude)]
    s = db.get_or_create_session("u@im.wechat", str(tmp_path))
    t = db.create_task(None, s.id, "big job")
    db.claim_next_pending({s.id})
    runner = TaskRunner(db, cfg, process_registry={})
    await runner.run(db.get_task(t), s)

    assert db.get_task(t).state == "dead"
    alerts = _alert_rows(db)
    assert len(alerts) == 1
    assert alerts[0].to_user == "u@im.wechat"
    assert f"#{t}" in alerts[0].text                 # 任务号
    assert "error_max_budget_usd" in alerts[0].text  # 截断原因（subtype）


async def test_poll_loop_401_clear_enqueues_reconnect_alert(db, monkeypatch):
    # 触发点 4：连续 5 次 HTTP 401 → 清 token（自动重连路径）→ ⚠️ 提示可能需
    # 重新扫码；幂等门保证清空后继续失败不重复告警。
    from gateway import app as app_mod
    from gateway.ilink import ILinkError

    class Unauthorized:
        def __init__(self):
            self.n = 0

        async def getupdates(self, buf, token, base_url=None):
            self.n += 1
            raise ILinkError("POST ilink/bot/getupdates HTTP 401: token invalid")

    async def _sleep(_t):   # 失败重试固定睡 5s → 即时让步，测试毫秒级跑完
        await _orig_sleep(0)

    monkeypatch.setattr("gateway.app.asyncio.sleep", _sleep)
    ilink = Unauthorized()
    cfg = SimpleNamespace(whitelist={"u@im.wechat"})
    db.set_state("bot_token", "VALID")
    token_ref = {"token": "VALID", "base_url": ""}
    task = asyncio.create_task(app_mod.poll_loop(db, cfg, ilink, None, None, token_ref))

    async def cleared():
        while token_ref["token"]:
            await _orig_sleep(0.01)
    await asyncio.wait_for(cleared(), 5)

    alerts = _alert_rows(db)
    assert len(alerts) == 1
    assert alerts[0].to_user == "u@im.wechat"
    assert "失效" in alerts[0].text and "重新扫码" in alerts[0].text

    async def more_fails():   # 清空后循环继续活着，但不重复清/重复告警
        while ilink.n < 12:
            await _orig_sleep(0.01)
    await asyncio.wait_for(more_fails(), 5)
    assert len(_alert_rows(db)) == 1
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
