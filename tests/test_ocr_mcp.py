"""worker/ocr_mcp.py 测试：函数级（fake engine）+ 子进程协议级（不触发引擎加载）。"""
import asyncio
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

from worker import ocr_mcp


class _FakeEngine:
    def __init__(self, rows):
        self._rows = rows
        self.calls = []          # 记录收到的是 bytes（直传契约）

    def __call__(self, data):
        self.calls.append(type(data))
        return self._rows, [0.1]


def _png(tmp_path) -> Path:
    """sniff_image 白名单内的最小 PNG 头 + 填充（不须是真图——引擎是 fake）。"""
    p = tmp_path / "a.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
    return p


def test_ocr_joins_lines_without_score(monkeypatch, tmp_path):
    fake = _FakeEngine([[None, "文字A", 0.9], [None, "text B", 0.8]])
    monkeypatch.setattr(ocr_mcp, "_get_engine", lambda: fake)
    out = ocr_mcp._ocr({"path": str(_png(tmp_path))})
    assert out == "文字A\ntext B"
    assert fake.calls == [bytes]          # bytes 直传（实测 API 契约）


def test_ocr_missing_file(tmp_path):
    out = ocr_mcp._ocr({"path": str(tmp_path / "no.png")})
    assert out.startswith("识别失败: 读文件失败")


def test_ocr_rejects_gif(tmp_path):
    p = tmp_path / "a.gif"
    p.write_bytes(b"GIF89a" + b"\x00" * 16)
    out = ocr_mcp._ocr({"path": str(p)})
    assert out == "识别失败: 不支持的图片格式 gif（OCR 仅支持 PNG/JPEG）"


def test_ocr_no_text(monkeypatch, tmp_path):
    monkeypatch.setattr(ocr_mcp, "_get_engine", lambda: _FakeEngine([]))
    assert ocr_mcp._ocr({"path": str(_png(tmp_path))}) == "识别失败: 未识别出文字"


def test_ocr_rejects_oversize_image(monkeypatch, tmp_path):
    """M-1：超上限的图读入后即拒（与入站/send_image 同一 MAX_IMAGE_BYTES 阈值），
    不喂引擎——阈值临时调小到 1MB、假图 1MB+1 字节造最小用例。"""
    import gateway.media
    monkeypatch.setattr(gateway.media, "MAX_IMAGE_BYTES", 1024 * 1024)
    fake = _FakeEngine([])
    monkeypatch.setattr(ocr_mcp, "_get_engine", lambda: fake)
    p = tmp_path / "big.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * (1024 * 1024))
    assert ocr_mcp._ocr({"path": str(p)}) == "识别失败: 图片超过 1MB 上限"
    assert fake.calls == []                      # 未喂引擎


def test_ocr_lazy_import_server_module_only():
    """lazy 契约：import server 模块不加载 rapidocr（子进程隔离验证，防本进程污染）。"""
    code = (f"import sys; sys.path.insert(0, r'{ROOT}'); "
            "import worker.ocr_mcp; "
            "print('rapidocr_onnxruntime' in sys.modules)")
    out = subprocess.run([sys.executable, "-c", code],
                         capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"


async def test_ocr_server_protocol_handshake():
    """子进程级 stdio JSON-RPC：initialize / tools/list / ping（不调 ocr，引擎零加载）。"""
    p = await asyncio.create_subprocess_exec(
        sys.executable, str(ROOT / "worker" / "ocr_mcp.py"),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, cwd=str(ROOT))
    try:
        async def send(obj):
            p.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
            await p.stdin.drain()

        async def recv():
            line = await asyncio.wait_for(p.stdout.readline(), 10)
            assert line, "server 无响应或提前退出"
            return json.loads(line.decode("utf-8"))

        await send({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        resp = await recv()
        assert resp["id"] == 1
        assert resp["result"]["serverInfo"]["name"] == "daoyu-ocr"

        await send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        resp = await recv()
        tools = resp["result"]["tools"]
        assert [t["name"] for t in tools] == ["ocr"]
        assert tools[0]["inputSchema"]["required"] == ["path"]

        await send({"jsonrpc": "2.0", "id": 3, "method": "ping"})
        resp = await recv()
        assert resp["result"] == {}
    finally:
        p.kill()
        await p.wait()
