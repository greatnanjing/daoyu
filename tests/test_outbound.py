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


def test_split_text_byte_cap_chinese():
    # 微信单条 16384 字节按字节计（实测钉死）：中文 3B/字，字符数没超 limit
    # 但字节超 MAX_PAGE_BYTES 时也必须切——否则 errcode=0 静默丢消息。
    from common.text import MAX_PAGE_BYTES
    text = "测" * 6000                      # 6000 字 = 18000 字节 > 15000
    pages = split_text(text, limit=100000)  # 字符上限故意放开，只看字节闸
    assert len(pages) >= 2
    for p in pages:
        assert len(p.encode("utf-8")) <= MAX_PAGE_BYTES + 32   # 前缀余量内
    assert "".join(p.split("\n", 1)[1] for p in pages) == text  # 内容无损


def test_split_text_byte_cap_ascii_high_limit():
    # 高 limit + 大 ASCII：字节闸兜底（16000B 超限要切，12000B 不切）
    from common.text import MAX_PAGE_BYTES
    assert len(split_text("x" * 16000, limit=99999)) >= 2
    assert split_text("x" * 12000, limit=99999) == ["x" * 12000]
    for p in split_text("x" * 40000, limit=99999):
        assert len(p.encode("utf-8")) <= MAX_PAGE_BYTES + 32


def test_split_text_mixed_content_no_char_split():
    # 中英混排不切碎多字节字符：逐字符累积的不变量
    text = ("中文abc" * 2000)               # 混排 14000 字
    rejoined = "".join(p.split("\n", 1)[1] for p in split_text(text, limit=3000))
    assert rejoined == text


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


# ---- M3 媒体出站 ----

import secrets as _secrets

from gateway.media import aes_ecb_decrypt, aes_ecb_encrypt


class FakeMediaILink:
    """对齐真实 ILinkClient 媒体签名：文本/图片发送 + CDN 上传三件。"""

    def __init__(self):
        self.sent_texts: list[str] = []       # (text,)
        self.sent_images: list[dict] = []
        self.upload_calls = 0
        self.uploaded_ct: bytes | None = None

    async def sendmessage(self, to_user, context_token, text,
                          token=None, base_url=None):
        self.sent_texts.append(text)
        return True

    async def send_image_message(self, to_user, context_token, *,
                                 download_param, aes_key_hex, size_cipher,
                                 token=None, base_url=None):
        self.sent_images.append({"download_param": download_param,
                                 "aes_key_hex": aes_key_hex,
                                 "size_cipher": size_cipher})
        return True

    async def send_media_message(self, to_user, context_token, *, item,
                                 token=None, base_url=None):
        self.sent_media_items = getattr(self, "sent_media_items", [])
        self.sent_media_items.append((to_user, item))
        return True

    async def getuploadurl(self, **kw):
        self.upload_calls += 1
        self.upload_kwargs = kw
        return {"upload_full_url": "https://cdn/up"}

    async def cdn_upload(self, url, ciphertext):
        self.uploaded_ct = ciphertext
        return "DL-PARAM"

    async def getconfig(self, ilink_user_id, context_token,
                        token=None, base_url=None):
        return "TICKET" if context_token else ""

    async def sendtyping(self, ilink_user_id, ticket, status,
                         token=None, base_url=None):
        return None


def _png_bytes():
    return b"\x89PNG\r\n\x1a\n" + _secrets.token_bytes(32)


async def test_drain_image_row_caption_then_image(db, tmp_path):
    # 注：_png_bytes() 每次调用随机，须先固定一份 raw 再比较（简报原样断言
    # 二次调用 _png_bytes() 会因随机串不同恒 False）。
    raw = _png_bytes()
    img = tmp_path / "out.png"; img.write_bytes(raw)
    db.insert_message(common_msg("u@im.wechat", "CTX"))
    db.enqueue_media(None, "u@im.wechat", str(img), "看这个")
    fake = FakeMediaILink()
    loop = OutboundLoop(db, fake, FakeCfg(),
                        token_ref={"token": "T", "base_url": ""}, typing_state={})
    await loop._drain_once()
    assert fake.sent_texts == ["看这个"]            # caption 先发（文本条在前）
    assert len(fake.sent_images) == 1              # 图片条在后
    sent = fake.sent_images[0]
    assert sent["download_param"] == "DL-PARAM"
    assert sent["size_cipher"] == ((len(raw) + 16) // 16 * 16)
    # aes_key_hex 是 hex32 字符串（sendmessage 报 base64(hex32 ASCII)——官方
    # 形态，防 base64(raw16B) 回退导致微信端空白图）
    assert len(sent["aes_key_hex"]) == 32
    key = bytes.fromhex(sent["aes_key_hex"])
    assert len(key) == 16 and aes_ecb_decrypt(fake.uploaded_ct, key) == raw
    assert db.get_outbox(db._conn.execute(
        "SELECT id FROM outbox").fetchone()["id"]).state == "sent"


async def test_drain_image_upload_failure_leaves_pending(db, tmp_path):
    img = tmp_path / "out.png"; img.write_bytes(_png_bytes())
    db.insert_message(common_msg("u@im.wechat", "CTX"))
    db.enqueue_media(None, "u@im.wechat", str(img), "")
    fake = FakeMediaILink()
    async def boom(url, ciphertext):
        raise RuntimeError("cdn down")
    fake.cdn_upload = boom          # 上传异常 → _send_media 捕获 → 整行留 pending
    loop = OutboundLoop(db, fake, FakeCfg(),
                        token_ref={"token": "T", "base_url": ""}, typing_state={})
    await loop._drain_once()
    row = db._conn.execute("SELECT state, last_error FROM outbox").fetchone()
    assert row["state"] == "pending" and row["last_error"]


async def test_drain_image_missing_file_failure(db):
    db.insert_message(common_msg("u@im.wechat", "CTX"))
    db.enqueue_media(None, "u@im.wechat", "/nonexistent/x.png", "")
    fake = FakeMediaILink()
    loop = OutboundLoop(db, fake, FakeCfg(),
                        token_ref={"token": "T", "base_url": ""}, typing_state={})
    await loop._drain_once()
    assert fake.sent_images == []
    row = db._conn.execute("SELECT state FROM outbox").fetchone()
    assert row["state"] == "pending"


class CaptionFailFirstILink(FakeMediaILink):
    """caption 文本条首次 sendmessage 失败、后续成功；统一 events 记录顺序。"""

    def __init__(self):
        super().__init__()
        self.events: list[tuple] = []
        self._text_fails = 1

    async def sendmessage(self, to_user, context_token, text,
                          token=None, base_url=None):
        if self._text_fails > 0:
            self._text_fails -= 1
            return False
        self.events.append(("text", text))
        return True

    async def send_image_message(self, to_user, context_token, *,
                                 download_param, aes_key_hex, size_cipher,
                                 token=None, base_url=None):
        self.events.append(("image", download_param))
        return True

    async def send_media_message(self, to_user, context_token, *, item,
                                 token=None, base_url=None):
        self.sent_media_items = getattr(self, "sent_media_items", [])
        self.sent_media_items.append((to_user, item))
        self.events.append(("media", item))   # 与 text/image 同入统一顺序日志
        return True


async def test_image_row_caption_fail_retry_resends_caption(db, tmp_path):
    # F5/ledger6：caption 发送失败 → 整行留 pending；重试整行重做（caption 会
    # 重发——_send_media 文档化的核心取舍）。回归锁：重试后两条消息顺序
    # caption 在前、图在后，行终态 sent。
    img = tmp_path / "out.png"; img.write_bytes(_png_bytes())
    db.insert_message(common_msg("u@im.wechat", "CTX"))
    db.enqueue_media(None, "u@im.wechat", str(img), "看这个")
    fake = CaptionFailFirstILink()
    loop = OutboundLoop(db, fake, FakeCfg(),
                        token_ref={"token": "T", "base_url": ""}, typing_state={})
    await loop._drain_once()
    assert fake.events == []                        # caption 失败 → 图未发
    row = db._conn.execute("SELECT state, last_error FROM outbox").fetchone()
    assert row["state"] == "pending" and "caption 发送失败" in row["last_error"]
    await loop._drain_once()                        # 整行重试
    assert [e[0] for e in fake.events] == ["text", "image"]   # caption 重发在前
    assert fake.events[0][1] == "看这个"
    assert db._conn.execute("SELECT state FROM outbox").fetchone()["state"] == "sent"


async def test_image_row_dead_letter_alerts(db, tmp_path):
    # F5/ledger6：图片行上传恒失败 → 烧满 attempts 进死信 + ⚠️ 告警入 outbox
    # （监控链路回归锁；告警需 cfg 带 whitelist，否则 _alert_all 静默跳过）。
    img = tmp_path / "gone.png"; img.write_bytes(_png_bytes())
    db.insert_message(common_msg("u@im.wechat", "CTX"))
    db.enqueue_media(None, "u@im.wechat", str(img), "")
    fake = FakeMediaILink()

    async def boom(url, ciphertext):
        raise RuntimeError("cdn down forever")
    fake.cdn_upload = boom
    cfg = FakeCfg()
    cfg.whitelist = {"u@im.wechat"}
    loop = OutboundLoop(db, fake, cfg,
                        token_ref={"token": "T", "base_url": ""}, typing_state={})
    task = asyncio.create_task(loop.run_forever())
    try:
        assert await wait_until(lambda: db._conn.execute(
            "SELECT state FROM outbox WHERE kind='image'"
        ).fetchone()["state"] == "dead")
        row = db._conn.execute(
            "SELECT attempts, last_error FROM outbox WHERE kind='image'").fetchone()
        assert row["attempts"] >= 5 and "CDN 上传失败" in row["last_error"]
        alerts = [r["text"] for r in db._conn.execute(
            "SELECT text FROM outbox WHERE kind!='image'")]
        assert any(t.startswith("⚠️ 出站图片死信") for t in alerts)
        dead = db._conn.execute(
            "SELECT detail FROM audit_log WHERE kind='dead_letter'").fetchall()
        assert dead
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


# ---- 出站日计数按页折算（M1 移交项清偿：计数口径=微信侧真实发送条数）----

async def test_daily_count_counts_pages_not_rows(db):
    """3 页长文送达 → _sent_today 计 3（旧口径按 outbox 行只计 1）。"""
    db.insert_message(common_msg("u@im.wechat", "CTX"))
    il = FakeILink()
    loop = OutboundLoop(db, il, FakeCfg(),
                        token_ref={"token": "T", "base_url": ""}, typing_state={})
    db.enqueue(None, "u@im.wechat", "x" * 5000)   # 3 页（limit 2000）
    await loop._drain_once()
    assert len(il.sent) == 3
    assert loop._sent_today == 3


async def test_daily_count_failed_pages_not_counted(db):
    """首页发送失败即止：成功页 0，计数 0（失败页对端未收到不计）。"""
    db.insert_message(common_msg("u@im.wechat", "CTX"))
    il = FakeILink()
    il.fail_first = 1
    loop = OutboundLoop(db, il, FakeCfg(),
                        token_ref={"token": "T", "base_url": ""}, typing_state={})
    db.enqueue(None, "u@im.wechat", "x" * 5000)
    await loop._drain_once()
    assert loop._sent_today == 0


async def test_daily_count_survives_restart(db):
    """重启恢复：3 页送达后新建 OutboundLoop（同 db），计数从 sent 行折算回来。"""
    db.insert_message(common_msg("u@im.wechat", "CTX"))
    il = FakeILink()
    loop = OutboundLoop(db, il, FakeCfg(),
                        token_ref={"token": "T", "base_url": ""}, typing_state={})
    db.enqueue(None, "u@im.wechat", "x" * 5000)
    await loop._drain_once()
    assert loop._sent_today == 3
    loop2 = OutboundLoop(db, FakeILink(), FakeCfg(),
                         token_ref={"token": "T", "base_url": ""}, typing_state={})
    assert loop2._sent_today == 3


async def test_daily_count_image_row_counts_caption_and_image(db, tmp_path):
    """图片行带 caption 送达 → 计 2（caption 文本条 + 图片条各计 1）。"""
    img = tmp_path / "out.png"; img.write_bytes(_png_bytes())
    db.insert_message(common_msg("u@im.wechat", "CTX"))
    db.enqueue_media(None, "u@im.wechat", str(img), "看这个")
    fake = FakeMediaILink()
    loop = OutboundLoop(db, fake, FakeCfg(),
                        token_ref={"token": "T", "base_url": ""}, typing_state={})
    await loop._drain_once()
    assert len(fake.sent_texts) == 1 and len(fake.sent_images) == 1
    assert loop._sent_today == 2


# ---- M5B：kind='file' 出站（video/file 两分支） ----

def _mk_file_outbox_row(db, path, caption="配文", to_user="u@im.wechat"):
    db._conn.execute(
        "INSERT INTO outbox(task_id, to_user, text, kind, media_path, caption, "
        "created_at) VALUES(?,?,?,?,?,?,?)",
        (None, to_user, "", "file", str(path), caption, 1))
    db._conn.commit()
    return db._conn.execute("SELECT id FROM outbox ORDER BY id DESC LIMIT 1"
                            ).fetchone()["id"]


async def test_outbound_file_video_branch(db, tmp_path):
    """mp4 → media_type=2 + video 条（video_size 密文）；先 caption 文本条。"""
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"\x00" * 100)
    oid = _mk_file_outbox_row(db, f)
    db.insert_message(InboundMessage(msg_id="1", from_user="u@im.wechat",
                                     text="hi", context_token="CTX", received_at=1))
    fake = FakeMediaILink()   # 复用 M3 图片 fake（getuploadurl 记录 upload_kwargs）
    loop = OutboundLoop(db, fake, FakeCfg(),
                        token_ref={"token": "T", "base_url": ""}, typing_state={})
    await loop._drain_once()
    assert fake.upload_kwargs["media_type"] == 2
    to_user, item = fake.sent_media_items[-1]
    assert to_user == "u@im.wechat"
    assert item["type"] == 5 and "video_item" in item
    assert item["video_item"]["video_size"] == fake.upload_kwargs["filesize"]
    texts = [t for t in fake.sent_texts if t == "配文"]
    assert texts                                       # caption 先发
    assert db.get_outbox(oid).state == "sent"


async def test_outbound_file_plain_branch(db, tmp_path):
    """pdf → media_type=3 + file 条（len 明文字符串 + file_name 原名）。"""
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"%PDF-1.4")
    oid = _mk_file_outbox_row(db, f)
    db.insert_message(InboundMessage(msg_id="1", from_user="u@im.wechat",
                                     text="hi", context_token="CTX", received_at=1))
    fake = FakeMediaILink()
    loop = OutboundLoop(db, fake, FakeCfg(),
                        token_ref={"token": "T", "base_url": ""}, typing_state={})
    await loop._drain_once()
    assert fake.upload_kwargs["media_type"] == 3
    _, item = fake.sent_media_items[-1]
    assert item["type"] == 4 and item["file_item"]["file_name"] == "doc.pdf"
    assert item["file_item"]["len"] == str(f.stat().st_size)   # 明文大小字符串
    assert db.get_outbox(oid).state == "sent"


async def test_outbound_file_caption_fail_retry_order(db, tmp_path):
    """M5B 终审 #11/#12：file 行顺序回归锁（同构图片行 CaptionFailFirst 形态）——
    caption 首发失败 → 媒体条未发、整行留 pending；重试整行重做后 events 顺序
    caption 文本在前、媒体条在后（send_media_message 已入 events 统一日志）。"""
    f = tmp_path / "doc.pdf"; f.write_bytes(b"%PDF-1.4")
    db.insert_message(common_msg("u@im.wechat", "CTX"))
    _mk_file_outbox_row(db, f, caption="配文")
    fake = CaptionFailFirstILink()
    loop = OutboundLoop(db, fake, FakeCfg(),
                        token_ref={"token": "T", "base_url": ""}, typing_state={})
    await loop._drain_once()
    assert fake.events == []                        # caption 失败 → 媒体条未发
    row = db._conn.execute(
        "SELECT state, last_error FROM outbox WHERE kind='file'").fetchone()
    assert row["state"] == "pending" and "caption 发送失败" in row["last_error"]
    await loop._drain_once()                        # 整行重试
    assert [e[0] for e in fake.events] == ["text", "media"]   # caption 重发在前
    assert fake.events[0][1] == "配文"
    assert db._conn.execute(
        "SELECT state FROM outbox WHERE kind='file'").fetchone()["state"] == "sent"


async def test_daily_count_file_row_counts_caption_and_media(db, tmp_path):
    """M5B 终审 #0：file 行运行时实发 2 条（caption 文本条经 _send + 媒体条
    各计 1），重启恢复折算同为 2——outbox_sent_pages 已把 file 并入 image
    同支（此前 file 行走空文本分支只折算 1，恢复口径低估）。"""
    f = tmp_path / "doc.pdf"; f.write_bytes(b"%PDF-1.4")
    db.insert_message(common_msg("u@im.wechat", "CTX"))
    _mk_file_outbox_row(db, f, caption="配文")
    loop = OutboundLoop(db, FakeMediaILink(), FakeCfg(),
                        token_ref={"token": "T", "base_url": ""}, typing_state={})
    await loop._drain_once()
    assert loop._sent_today == 2
    loop2 = OutboundLoop(db, FakeMediaILink(), FakeCfg(),
                         token_ref={"token": "T", "base_url": ""}, typing_state={})
    assert loop2._sent_today == 2                  # 重启从 sent 行折算回来同为 2
