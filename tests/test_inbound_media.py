"""入站图片消息管线：下载落盘 → 发图即对话建任务；失败回执；msg_id 去重覆盖图片重投。"""
import secrets
from pathlib import Path

from common.db import Database
from gateway.app import handle_inbound
from gateway.media import aes_ecb_encrypt, sniff_image

USER = "u@im.wechat"


class Cfg:
    """最小 cfg：handle_inbound 用 whitelist/default_cwd，落盘目录走 repo_root。"""

    def __init__(self, tmp_path):
        self.whitelist = {USER}
        self.default_cwd = str(tmp_path)
        self.repo_root = tmp_path


class FakeDownloadILink:
    def __init__(self, ciphertext: bytes):
        self._ct = ciphertext

    async def cdn_download(self, url):
        return self._ct


def _png() -> bytes:
    return b"\x89PNG\r\n\x1a\n" + secrets.token_bytes(32)


def _img_msg(msg_id, key_hex: str, raw: bytes, text: str | None = None):
    item = {"type": 2, "image_item": {
        "aeskey": key_hex, "media": {"encrypt_query_param": "EQ"}}}
    items = [item]
    if text is not None:
        items = [{"type": 1, "text_item": {"text": text}}, item]
    return {"message_id": msg_id, "seq": msg_id, "from_user_id": USER,
            "message_type": 1, "context_token": "CTX", "item_list": items}


def _tasks(db):
    return db._conn.execute("SELECT prompt FROM tasks ORDER BY id").fetchall()


async def test_image_only_message_creates_chat_task(tmp_path, monkeypatch):
    import base64
    db = Database(tmp_path / "t.db"); db.ensure_schema()
    cfg = Cfg(tmp_path)
    key = secrets.token_bytes(16)
    fake = FakeDownloadILink(aes_ecb_encrypt(_png(), key))
    await handle_inbound(db, cfg, None, None, _img_msg(1, key.hex(), _png()),
                         ilink=fake)
    rows = _tasks(db)
    assert len(rows) == 1
    prompt = rows[0]["prompt"]
    assert "用户发来图片" in prompt and "已保存到" in prompt
    # 图片确实落盘在 data/media/inbound 且是合法 PNG
    path = prompt.split("已保存到 ")[1].split("，")[0]
    assert Path(path).read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert Path(path).parent == tmp_path / "data" / "media" / "inbound"
    # messages 行带 media_path；有 ACK 回执
    assert db._conn.execute(
        "SELECT media_path FROM messages").fetchone()["media_path"] == path
    assert any("收到" in r["text"] for r in db._conn.execute(
        "SELECT text FROM outbox"))


async def test_image_with_text_appends_to_prompt(tmp_path):
    db = Database(tmp_path / "t.db"); db.ensure_schema()
    cfg = Cfg(tmp_path)
    key = secrets.token_bytes(16)
    raw = _png()
    fake = FakeDownloadILink(aes_ecb_encrypt(raw, key))
    await handle_inbound(db, cfg, None, None,
                         _img_msg(2, key.hex(), raw, text="看看这个报错"),
                         ilink=fake)
    prompt = _tasks(db)[0]["prompt"]
    assert prompt.startswith("看看这个报错")
    assert "用户发来图片" in prompt      # 图路径附在文字后


async def test_image_download_failure_receipt_no_task(tmp_path):
    db = Database(tmp_path / "t.db"); db.ensure_schema()
    cfg = Cfg(tmp_path)
    bad = FakeDownloadILink(b"garbage-not-encrypted")   # 解密必败
    await handle_inbound(db, cfg, None, None,
                         _img_msg(3, secrets.token_bytes(16).hex(), _png()),
                         ilink=bad)
    assert _tasks(db) == []            # 不建任务
    assert any("图片" in r["text"] for r in db._conn.execute(
        "SELECT text FROM outbox"))    # 失败回执


async def test_image_without_ilink_receipt(tmp_path):
    db = Database(tmp_path / "t.db"); db.ensure_schema()
    cfg = Cfg(tmp_path)
    await handle_inbound(db, cfg, None, None,
                         _img_msg(4, "00" * 16, _png()), ilink=None)
    assert _tasks(db) == []
    assert any("图片" in r["text"] for r in db._conn.execute(
        "SELECT text FROM outbox"))


async def test_image_msg_id_dedup(tmp_path):
    db = Database(tmp_path / "t.db"); db.ensure_schema()
    cfg = Cfg(tmp_path)
    key = secrets.token_bytes(16)
    fake = FakeDownloadILink(aes_ecb_encrypt(_png(), key))
    msg = _img_msg(5, key.hex(), _png())
    await handle_inbound(db, cfg, None, None, msg, ilink=fake)
    await handle_inbound(db, cfg, None, None, msg, ilink=fake)   # iLink 重投
    assert len(_tasks(db)) == 1


class CountingBadILink:
    """密文恒坏（解密必败）并计数——锁"失败图重投只下载一次"。"""

    def __init__(self):
        self.calls = 0

    async def cdn_download(self, url):
        self.calls += 1
        return b"garbage-not-encrypted"


async def test_failed_image_redelivery_single_receipt(tmp_path):
    """I-1/F1 回归：下载失败的图消息被 iLink 重投两次 → 只下载一次 CDN 密文、
    ⚠️ 回执恰好一条（去重保护回执副作用，不只保护任务创建）。"""
    db = Database(tmp_path / "t.db"); db.ensure_schema()
    cfg = Cfg(tmp_path)
    bad = CountingBadILink()
    msg = _img_msg(7, secrets.token_bytes(16).hex(), _png())
    await handle_inbound(db, cfg, None, None, msg, ilink=bad)
    await handle_inbound(db, cfg, None, None, msg, ilink=bad)   # 重投 1
    await handle_inbound(db, cfg, None, None, msg, ilink=bad)   # 重投 2
    assert bad.calls == 1                       # 不再重复下载
    receipts = [r["text"] for r in db._conn.execute(
        "SELECT text FROM outbox") if "图片接收失败" in r["text"]]
    assert len(receipts) == 1                   # 回执恰好一条
    assert _tasks(db) == []


async def test_partial_images_one_ok_one_failed(tmp_path):
    """F5/ledger5：多图部分成功（1 成 1 败）→ 任务 prompt 只带成功路径 + ⚠️ 回执。"""
    db = Database(tmp_path / "t.db"); db.ensure_schema()
    cfg = Cfg(tmp_path)
    key = secrets.token_bytes(16)
    good_ct = aes_ecb_encrypt(_png(), key)

    class PartialILink:
        async def cdn_download(self, url):
            if "BAD" in url:
                raise RuntimeError("cdn 410 gone")
            return good_ct

    msg = {"message_id": 8, "seq": 8, "from_user_id": USER,
           "message_type": 1, "context_token": "CTX", "item_list": [
               {"type": 2, "image_item": {"aeskey": "00" * 16,
                                          "media": {"encrypt_query_param": "BAD"}}},
               {"type": 2, "image_item": {"aeskey": key.hex(),
                                          "media": {"encrypt_query_param": "OK"}}}]}
    await handle_inbound(db, cfg, None, None, msg, ilink=PartialILink())
    rows = _tasks(db)
    assert len(rows) == 1
    assert rows[0]["prompt"].count("已保存到") == 1     # 只带成功的那张
    assert any("图片接收失败" in r["text"] for r in db._conn.execute(
        "SELECT text FROM outbox"))                     # ⚠️ 部分失败回执


# ---- M5B：语音/文件/视频入站 ----

def _media_msg(msg_id, item: dict, text: str | None = None):
    items = [item]
    if text is not None:
        items = [{"type": 1, "text_item": {"text": text}}, item]
    return {"message_id": msg_id, "seq": msg_id, "from_user_id": USER,
            "message_type": 1, "context_token": "CTX", "item_list": items}


def _file_item(key: bytes, name="报表.xlsx", raw=b"PK\x03\x04data"):
    import base64
    return {"type": 4, "file_item": {
        "file_name": name,
        "media": {"encrypt_query_param": "EQ",
                  "aes_key": base64.b64encode(key.hex().encode()).decode()}}}


async def test_voice_transcript_treated_as_text(tmp_path):
    """有转写的语音：text 直接当用户文字建任务（零解码成本，官方同构）。"""
    db = Database(tmp_path / "t.db"); db.ensure_schema()
    msg = _media_msg(11, {"type": 3, "voice_item": {"text": "帮我看下日志"}})
    await handle_inbound(db, Cfg(tmp_path), None, None, msg, ilink=None)
    rows = _tasks(db)
    assert len(rows) == 1 and rows[0]["prompt"] == "帮我看下日志"
    assert db._conn.execute(
        "SELECT COUNT(*) c FROM messages").fetchone()["c"] == 1   # 落盘正常


async def test_voice_no_transcript_archived_receipt_no_task(tmp_path):
    """无转写语音：下载 SILK 存档 + ⚠️ 回执 + 不建任务。"""
    import secrets as _s
    db = Database(tmp_path / "t.db"); db.ensure_schema()
    key = _s.token_bytes(16)
    raw = b"silk-bytes" + _s.token_bytes(16)
    fake = FakeDownloadILink(aes_ecb_encrypt(raw, key))
    item = {"type": 3, "voice_item": {
        "media": {"encrypt_query_param": "EQ",
                  "aes_key": __import__("base64").b64encode(key.hex().encode()).decode()}}}
    await handle_inbound(db, Cfg(tmp_path), None, None, _media_msg(12, item),
                         ilink=fake)
    assert _tasks(db) == []                          # 不建任务
    texts = [r["text"] for r in db._conn.execute("SELECT text FROM outbox")]
    assert any("语音未能转写" in t for t in texts)   # ⚠️ 回执
    saved = list((tmp_path / "data" / "media" / "inbound").glob("voice-*.silk"))
    assert len(saved) == 1 and saved[0].read_bytes() == raw


async def test_file_message_creates_task_with_name_and_size(tmp_path):
    import secrets as _s
    db = Database(tmp_path / "t.db"); db.ensure_schema()
    key = _s.token_bytes(16)
    fake = FakeDownloadILink(aes_ecb_encrypt(b"PK\x03\x04xlsx", key))
    await handle_inbound(db, Cfg(tmp_path), None, None,
                         _media_msg(13, _file_item(key)), ilink=fake)
    prompt = _tasks(db)[0]["prompt"]
    assert "用户发来文件 报表.xlsx" in prompt and "已保存到" in prompt
    saved = list((tmp_path / "data" / "media" / "inbound").glob("file-*.xlsx"))
    assert len(saved) == 1
    assert db._conn.execute(
        "SELECT media_path FROM messages").fetchone()["media_path"] == str(saved[0])


async def test_video_message_creates_task_with_ffmpeg_hint(tmp_path):
    import secrets as _s
    db = Database(tmp_path / "t.db"); db.ensure_schema()
    key = _s.token_bytes(16)
    raw = b"\x00\x00\x00\x18ftypmp42" + _s.token_bytes(32)
    fake = FakeDownloadILink(aes_ecb_encrypt(raw, key))
    item = {"type": 5, "video_item": {
        "media": {"encrypt_query_param": "EQ",
                  "aes_key": __import__("base64").b64encode(key.hex().encode()).decode()}}}
    await handle_inbound(db, Cfg(tmp_path), None, None, _media_msg(14, item),
                         ilink=fake)
    prompt = _tasks(db)[0]["prompt"]
    assert "用户发来视频" in prompt and "ffmpeg" in prompt
    assert list((tmp_path / "data" / "media" / "inbound").glob("vid-*.mp4"))


async def test_file_with_text_appends_to_prompt(tmp_path):
    import secrets as _s
    db = Database(tmp_path / "t.db"); db.ensure_schema()
    key = _s.token_bytes(16)
    fake = FakeDownloadILink(aes_ecb_encrypt(b"PK\x03\x04", key))
    await handle_inbound(db, Cfg(tmp_path), None, None,
                         _media_msg(15, _file_item(key), text="这份报表汇总下"),
                         ilink=fake)
    prompt = _tasks(db)[0]["prompt"]
    assert prompt.startswith("这份报表汇总下")
    assert "用户发来文件" in prompt


async def test_file_download_failure_receipt_no_task(tmp_path):
    import secrets as _s
    db = Database(tmp_path / "t.db"); db.ensure_schema()
    bad = FakeDownloadILink(b"garbage")
    await handle_inbound(db, Cfg(tmp_path), None, None,
                         _media_msg(16, _file_item(_s.token_bytes(16))),
                         ilink=bad)
    assert _tasks(db) == []
    assert any("文件" in r["text"] for r in db._conn.execute("SELECT text FROM outbox"))
