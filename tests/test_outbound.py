import asyncio
import contextlib
import time

from common.models import InboundMessage
from common.text import split_text
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
