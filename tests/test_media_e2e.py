"""M3 媒体 E2E：
1) 入站：图消息 → 下载落盘 → chat 任务 prompt 带路径 → fake claude 跑完。
2) 出站：enqueue_media → OutboundLoop._drain_once → FakeMediaILink 断言上传+发送参数。
3) MCP：send_image 子进程往返 → outbox 媒体行 → _drain_once 投出。
4) M5B 入站：file 消息 → 下载落盘 → 任务 prompt 带原始名 → fake claude 跑完。
5) M5B 出站：send_file 子进程 → outbox kind='file' → _drain_once → file 条结构断言。
（真机微信端验收见 spec §5 待实测清单，另行手动做。）"""
import secrets
import sys
from pathlib import Path

from common.db import Database
from common.models import InboundMessage
from gateway.app import handle_inbound
from gateway.media import aes_ecb_decrypt, aes_ecb_encrypt
from gateway.outbound import OutboundLoop

FIXTURES = Path(__file__).parent / "fixtures"

USER = "u@im.wechat"
_PNG = b"\x89PNG\r\n\x1a\n" + secrets.token_bytes(64)


class E2EILink:
    """入站下载 + 出站上传/发送全 fake。"""

    def __init__(self, inbound_ciphertext: bytes):
        self._inbound_ct = inbound_ciphertext
        self.upload_kwargs = None
        self.uploaded_ct = None
        self.sent_texts = []
        self.sent_images = []
        self.sent_media_items = []

    async def cdn_download(self, url):
        return self._inbound_ct

    async def getuploadurl(self, **kw):
        self.upload_kwargs = kw
        return {"upload_full_url": "https://cdn/up"}

    async def cdn_upload(self, url, ciphertext):
        self.uploaded_ct = ciphertext
        return "E2E-DL-PARAM"

    async def sendmessage(self, to_user, ctx, text, token=None, base_url=None):
        self.sent_texts.append(text)
        return True

    async def send_image_message(self, to_user, ctx, *, download_param,
                                 aes_key_hex, size_cipher, token=None,
                                 base_url=None):
        self.sent_images.append({"download_param": download_param,
                                  "aes_key_hex": aes_key_hex,
                                  "size_cipher": size_cipher})
        return True

    async def send_media_message(self, to_user, ctx, *, item, token=None,
                                 base_url=None):
        self.sent_media_items.append((to_user, item))
        return True

    async def getconfig(self, *a, **kw):
        return ""

    async def sendtyping(self, *a, **kw):
        return None


class _Cfg:
    def __init__(self, tmp_path, monkeypatch):
        self.repo_root = tmp_path
        self.whitelist = {USER}
        self.default_cwd = str(tmp_path)
        self.claude_bin = [sys.executable, str(FIXTURES / "fake_claude.py")]
        self.secrets = {"ANTHROPIC_API_KEY": "sk"}
        self.throttle = {"progress_window_s": 0.0, "page_char_limit": 2000,
                         "min_send_interval_s": 0.0, "daily_send_limit": 500}
        from common.models import Budget
        self.budget = Budget()
        self.worker = {"concurrency": 2, "poll_interval_s": 0.01}
        self.reconnect = {}
        monkeypatch.setenv("FAKE_CLAUDE_SCRIPT", str(FIXTURES / "review_stream.jsonl"))
        monkeypatch.setenv("FAKE_CLAUDE_STDIN_LOG", str(tmp_path / "stdin.log"))
        monkeypatch.setenv("FAKE_CLAUDE_ARGS_LOG", str(tmp_path / "args.log"))


async def _wait_done(db, timeout=10):
    import asyncio
    async def done():
        while True:
            n = db._conn.execute(
                "SELECT COUNT(*) c FROM tasks WHERE state IN ('pending','running')"
            ).fetchone()["c"]
            if not n:
                return True
            await asyncio.sleep(0.05)
    await asyncio.wait_for(done(), timeout)


async def test_e2e_inbound_image_full_pipeline(tmp_path, monkeypatch):
    import asyncio
    from worker.pool import WorkerPool
    from worker.runner import TaskRunner
    cfg = _Cfg(tmp_path, monkeypatch)
    db = Database(tmp_path / "e2e.db"); db.ensure_schema()
    key = secrets.token_bytes(16)
    ilink = E2EILink(aes_ecb_encrypt(_PNG, key))
    runner = TaskRunner(db, cfg, process_registry={})
    pool = WorkerPool(db, cfg, runner=runner, concurrency=2, poll_interval_s=0.01)
    loop_task = asyncio.create_task(pool.run_forever())
    try:
        await handle_inbound(db, cfg, pool, None, {
            "message_id": 1, "seq": 1, "from_user_id": USER,
            "message_type": 1, "context_token": "CTX",
            "item_list": [{"type": 2, "image_item": {
                "aeskey": key.hex(), "media": {"encrypt_query_param": "EQ"}}}],
        }, ilink=ilink)
        await _wait_done(db)
        prompt = db._conn.execute("SELECT prompt FROM tasks").fetchone()["prompt"]
        assert "用户发来图片" in prompt
        path = prompt.split("已保存到 ")[1].split("，")[0]
        assert Path(path).read_bytes() == _PNG        # 落盘内容 = 解密明文
        states = db._conn.execute("SELECT state FROM tasks").fetchone()
        assert states["state"] == "done"
    finally:
        loop_task.cancel()


async def test_e2e_outbound_image_via_outbox(db, tmp_path):
    img = tmp_path / "reply.png"; img.write_bytes(_PNG)
    db.insert_message(InboundMessage(
        msg_id="m1", from_user=USER, text="hi", context_token="CTX", received_at=1))
    db.enqueue_media(None, USER, str(img), "结果截图")
    ilink = E2EILink(b"")
    class OCfg:
        throttle = {"min_send_interval_s": 0.0, "page_char_limit": 2000,
                    "daily_send_limit": 500}
        whitelist = {USER}
    loop = OutboundLoop(db, ilink, OCfg(), {"token": "T", "base_url": ""}, {})
    await loop._drain_once()
    assert ilink.sent_texts == ["结果截图"]                    # caption 先
    assert len(ilink.sent_images) == 1
    sent = ilink.sent_images[0]
    assert sent["download_param"] == "E2E-DL-PARAM"
    # aes_key_hex 是 hex32（sendmessage 报 base64(hex32 ASCII)，防空白图回退）
    assert len(sent["aes_key_hex"]) == 32
    key = bytes.fromhex(sent["aes_key_hex"])
    assert len(key) == 16
    assert aes_ecb_decrypt(ilink.uploaded_ct, key) == _PNG    # 上传密文可解回原图
    assert db._conn.execute("SELECT state FROM outbox").fetchone()["state"] == "sent"


async def test_e2e_send_image_mcp_to_delivery(db, tmp_path):
    """MCP 工具落库的媒体行能被 OutboundLoop 投出（跨进程 SQLite 通路）。"""
    import asyncio, json, os
    img = tmp_path / "mcp.png"; img.write_bytes(_PNG)
    env = os.environ.copy()
    env.update({"DAOYU_DB": str(db.path), "DAOYU_TASK_ID": "0",
                "DAOYU_TO_USER": USER, "DAOYU_TOOLS": "send_image"})
    db.insert_message(InboundMessage(
        msg_id="m1", from_user=USER, text="hi", context_token="CTX", received_at=1))
    proc = await asyncio.create_subprocess_exec(
        sys.executable, str(Path(__file__).resolve().parents[1] / "worker" / "approval_mcp.py"),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, env=env)
    async def rpc(i, method, params=None):
        line = json.dumps({"jsonrpc": "2.0", "id": i, "method": method,
                           **({"params": params} if params else {})})
        proc.stdin.write(line.encode() + b"\n")
        await proc.stdin.drain()
        out = await proc.stdout.readline()
        return json.loads(out)["result"]
    try:
        await rpc(1, "initialize")
        out = await rpc(2, "tools/call", {"name": "send_image", "arguments": {
            "path": str(img), "caption": "来自 MCP"}})
        assert "已排队发送" in out["content"][0]["text"]
    finally:
        proc.kill()
    ilink = E2EILink(b"")
    class OCfg:
        throttle = {"min_send_interval_s": 0.0, "page_char_limit": 2000,
                    "daily_send_limit": 500}
        whitelist = {USER}
    loop = OutboundLoop(db, ilink, OCfg(), {"token": "T", "base_url": ""}, {})
    await loop._drain_once()
    assert ilink.sent_texts == ["来自 MCP"] and len(ilink.sent_images) == 1


async def test_e2e_inbound_file_full_pipeline(tmp_path, monkeypatch):
    """M5B 入站文件全链路：file 消息 → 下载落盘 → 任务 prompt 带原始名。"""
    import asyncio
    import base64
    from worker.pool import WorkerPool
    from worker.runner import TaskRunner
    cfg = _Cfg(tmp_path, monkeypatch)
    db = Database(tmp_path / "e2e.db"); db.ensure_schema()
    key = secrets.token_bytes(16)
    raw = b"PK\x03\x04xlsx-content"
    ilink = E2EILink(aes_ecb_encrypt(raw, key))
    runner = TaskRunner(db, cfg, process_registry={})
    pool = WorkerPool(db, cfg, runner=runner, concurrency=2, poll_interval_s=0.01)
    loop_task = asyncio.create_task(pool.run_forever())
    try:
        await handle_inbound(db, cfg, pool, None, {
            "message_id": 1, "seq": 1, "from_user_id": USER,
            "message_type": 1, "context_token": "CTX",
            "item_list": [{"type": 4, "file_item": {
                "file_name": "报表.xlsx",
                "media": {"encrypt_query_param": "EQ",
                          "aes_key": base64.b64encode(key.hex().encode()).decode()}}}],
        }, ilink=ilink)
        await _wait_done(db)
        prompt = db._conn.execute("SELECT prompt FROM tasks").fetchone()["prompt"]
        assert "用户发来文件 报表.xlsx" in prompt
        path = prompt.split("已保存到 ")[1].split("，")[0]
        assert Path(path).read_bytes() == raw
    finally:
        loop_task.cancel()
        await asyncio.gather(loop_task, return_exceptions=True)


async def test_e2e_send_file_outbound_full_pipeline(tmp_path, monkeypatch):
    """M5B 出站全链路：MCP send_file 子进程 → outbox kind='file' →
    OutboundLoop → fake 断言 media_type 与条目结构（file 条 len 明文串）。"""
    import asyncio
    import json as _json
    import os
    from types import SimpleNamespace
    cfg = _Cfg(tmp_path, monkeypatch)
    db = Database(tmp_path / "e2e.db"); db.ensure_schema()
    db.insert_message(InboundMessage(msg_id="n1", from_user=USER, text="hi",
                                     context_token="CTX", received_at=1))
    src = tmp_path / "结果.pdf"
    src.write_bytes(b"%PDF-1.4 e2e")
    env = {**os.environ, "DAOYU_DB": str(db.path), "DAOYU_TASK_ID": "1",
           "DAOYU_TO_USER": USER, "DAOYU_TOOLS": "send_file"}
    proc = await asyncio.create_subprocess_exec(
        sys.executable, str(FIXTURES.parent.parent / "worker" / "approval_mcp.py"),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, cwd=str(FIXTURES.parents[1]), env=env)
    req = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
           "params": {"name": "send_file",
                      "arguments": {"path": str(src), "caption": "结果文件"}}}
    out, err = await proc.communicate(
        (_json.dumps(req) + "\n").encode("utf-8"))
    resp = _json.loads(out.decode("utf-8").splitlines()[0])
    assert not resp["result"].get("isError"), err.decode("utf-8", "replace")

    ilink = E2EILink(b"unused")
    out_cfg = SimpleNamespace(
        throttle={"min_send_interval_s": 0.0, "page_char_limit": 2000,
                  "daily_send_limit": 500, "progress_window_s": 0.0})
    loop = OutboundLoop(db, ilink, out_cfg,
                        token_ref={"token": "T", "base_url": ""}, typing_state={})
    await loop._drain_once()
    assert ilink.upload_kwargs["media_type"] == 3          # pdf → 文件条路由
    _, item = ilink.sent_media_items[-1]
    assert item["type"] == 4
    assert item["file_item"]["file_name"] == "结果.pdf"
    assert item["file_item"]["len"] == str(src.stat().st_size)   # len 是明文串
    assert "结果文件" in ilink.sent_texts            # caption 文本条先发
    assert db._conn.execute(
        "SELECT state FROM outbox WHERE kind='file'").fetchone()["state"] == "sent"
