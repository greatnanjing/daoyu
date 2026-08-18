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
