import asyncio
import contextlib
import time

from common.models import InboundMessage
from common.text import split_text
from gateway.ilink import ILinkError
from gateway.outbound import OutboundLoop


class FakeILink:
    """对齐真实 ILinkClient 签名（token/base_url 均为可选关键字参数）。"""

    def __init__(self):
        self.sent = []    # (to_user, context_token, text)
        self.typed = []   # (user, ticket, status)
        self.fail_first = 0
        self.calls = 0

    async def sendmessage(self, to_user, context_token, text, token=None, base_url=None):
        self.calls += 1
        if self.calls <= self.fail_first:
            return False
        self.sent.append((to_user, context_token, text))
        return True

    async def getconfig(self, ilink_user_id, context_token, token=None, base_url=None):
        return "TICKET" if context_token else ""

    async def sendtyping(self, ilink_user_id, ticket, status, token=None, base_url=None):
        self.typed.append((ilink_user_id, ticket, status))


class TypingBrokenILink(FakeILink):
    """typing 端点独立故障：getconfig/sendtyping 全抛 ILinkError（对齐真实非 200 行为）。"""

    async def getconfig(self, ilink_user_id, context_token, token=None, base_url=None):
        raise ILinkError("getconfig down")

    async def sendtyping(self, ilink_user_id, ticket, status, token=None, base_url=None):
        raise ILinkError("sendtyping down")


class FakeCfg:
    def __init__(self):
        self.throttle = {"min_send_interval_s": 0.0, "page_char_limit": 2000,
                         "daily_send_limit": 500, "progress_window_s": 0.0}


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


def test_split_text():
    assert split_text("短文本", 2000) == ["短文本"]
    pages = split_text("x" * 5000, 2000)
    assert len(pages) == 3
    assert pages[0].startswith("(第 1/3 页)")
    assert pages[0] == "(第 1/3 页)\n" + "x" * 2000
    assert pages[2] == "(第 3/3 页)\n" + "x" * 1000   # 末页只含剩余字符，无缺损


async def test_outbox_drain_marks_sent(db):
    il = FakeILink()
    db.insert_message(common_msg("u@im.wechat", "CTX-NEW"))
    loop = OutboundLoop(db, il, FakeCfg(),
                        token_ref={"token": "T", "base_url": ""},
                        typing_state={})
    db.enqueue(None, "u@im.wechat", "hello")
    task = asyncio.create_task(loop.run_forever())
    try:
        assert await wait_until(lambda: db.get_outbox(1).state == "sent")
        assert il.sent == [("u@im.wechat", "CTX-NEW", "hello")]   # 用最新 token
        # typing 开/关包住每次发送
        assert il.typed == [("u@im.wechat", "TICKET", 1),
                            ("u@im.wechat", "TICKET", 2)]
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_retry_then_success(db):
    il = FakeILink()
    il.fail_first = 2
    db.insert_message(common_msg("u@im.wechat", "CTX"))
    loop = OutboundLoop(db, il, FakeCfg(),
                        token_ref={"token": "T", "base_url": ""}, typing_state={})
    db.enqueue(None, "u@im.wechat", "m")
    task = asyncio.create_task(loop.run_forever())
    try:
        assert await wait_until(lambda: db.get_outbox(1).state == "sent")
        assert il.calls == 3   # 失败 2 次后第 3 次成功
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_no_inbound_history_skips_send_and_deads(db):
    # TRD "token 陷阱"对策：该用户无入站历史 → 拿不到 context_token，绝不拿空
    # token 发（会 HTTP 200 但静默不投递）→ return False 走重试耗尽 → 死信 + 告警
    il = FakeILink()
    loop = OutboundLoop(db, il, FakeCfg(),
                        token_ref={"token": "T", "base_url": ""}, typing_state={})
    db.enqueue(None, "ghost@im.wechat", "m")
    task = asyncio.create_task(loop.run_forever())
    try:
        assert await wait_until(lambda: db.get_outbox(1).state == "dead")
        assert il.calls == 0      # 一次都没真正发过
        assert il.sent == []
        assert il.typed == []     # 拿不到 ticket，typing 也未发
        # last_error 按原因区分：空 token ≠ sendmessage 未确认（排障不被误导）
        assert "context_token" in db.get_outbox(1).last_error
        dead = db._conn.execute(
            "SELECT detail FROM audit_log WHERE kind='dead_letter'").fetchall()
        assert dead and "id=1" in dead[-1]["detail"]
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_paging_uses_latest_token(db):
    il = FakeILink()
    db.insert_message(common_msg("u@im.wechat", "CTX-OLD", msg_id="1"))
    db.insert_message(common_msg("u@im.wechat", "CTX-NEW", msg_id="2"))
    loop = OutboundLoop(db, il, FakeCfg(),
                        token_ref={"token": "T", "base_url": ""}, typing_state={})
    db.enqueue(None, "u@im.wechat", "x" * 5000)
    task = asyncio.create_task(loop.run_forever())
    try:
        assert await wait_until(lambda: db.get_outbox(1).state == "sent")
        assert len(il.sent) == 3                              # 5000 字符 → 3 页
        assert all(t == "CTX-NEW" for _, t, _ in il.sent)     # 每页都用最新 token
        assert il.sent[0][2].startswith("(第 1/3 页)")
        assert il.calls == 3
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_typing_failure_does_not_block_send(db):
    # typing 是 cosmetic 功能：getconfig/sendtyping 端点独立故障（抛 ILinkError）
    # 只能降级，绝不阻断 sendmessage 主路径——否则 typing 故障期间全部出站会
    # 逐条烧完 5 次尝试进死信、回复全丢。
    il = TypingBrokenILink()
    db.insert_message(common_msg("u@im.wechat", "CTX"))
    loop = OutboundLoop(db, il, FakeCfg(),
                        token_ref={"token": "T", "base_url": ""}, typing_state={})
    db.enqueue(None, "u@im.wechat", "m")
    task = asyncio.create_task(loop.run_forever())
    try:
        assert await wait_until(lambda: db.get_outbox(1).state == "sent")
        assert il.sent == [("u@im.wechat", "CTX", "m")]   # sendmessage 照常送达
        assert il.calls == 1                              # 且只发一次（无徒劳重试）
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_daily_limit_circuit_breaker_audits_once(db):
    # 熔断告警：每日上限触发时记 audit，但每个熔断周期只记一次（循环 0.5s 一轮，
    # 不逐轮刷屏）；熔断期间不 claim outbox（attempts 不空耗）
    il = FakeILink()
    cfg = FakeCfg()
    cfg.throttle["daily_send_limit"] = 0                    # 上来即熔断
    db.insert_message(common_msg("u@im.wechat", "CTX"))
    loop = OutboundLoop(db, il, cfg,
                        token_ref={"token": "T", "base_url": ""}, typing_state={})
    db.enqueue(None, "u@im.wechat", "m")
    task = asyncio.create_task(loop.run_forever())
    try:
        await asyncio.sleep(1.2)                            # 跨 ~3 轮 drain
        rows = db._conn.execute(
            "SELECT detail FROM audit_log WHERE kind='daily_limit'").fetchall()
        assert len(rows) == 1                               # 恰一次，非每轮一条
        assert "limit=0" in rows[0]["detail"]
        assert db.get_outbox(1).state == "pending"          # 未被 claim、未空耗
        assert il.calls == 0
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_empty_bot_token_window_keeps_pending_then_delivers(db):
    # I-1 回归：token 失效（401/403 清空）→ 重连扫码窗（最长 600s）内不得 claim
    # outbox——空 token 发送必败，5 次尝试会在几十秒内烧光 → 全部死信（M1 无
    # re-drive）。守卫须保 outbox pending、attempts 不空耗；token 原子换回
    # （_swap_token 原位改写共享 dict）后自动续投送达。
    il = FakeILink()
    db.insert_message(common_msg("u@im.wechat", "CTX"))
    token_ref = {"token": "", "base_url": ""}     # 空窗期：token 已清空，待扫码
    loop = OutboundLoop(db, il, FakeCfg(), token_ref, {})
    db.enqueue(None, "u@im.wechat", "m")
    task = asyncio.create_task(loop.run_forever())
    try:
        await asyncio.sleep(1.0)                   # 跨 ~2 轮 drain（0.5s 兜底轮询）
        assert db.get_outbox(1).state == "pending"  # 未被 claim、attempts 未空耗
        assert db.get_outbox(1).attempts == 0
        assert il.calls == 0
        token_ref["token"] = "T"                   # 重连成功：token 原子替换回填
        loop.notify()                              # 重连/入站方唤醒出站循环
        assert await wait_until(lambda: db.get_outbox(1).state == "sent")
        assert il.sent == [("u@im.wechat", "CTX", "m")]
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
