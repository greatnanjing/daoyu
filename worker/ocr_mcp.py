"""daoyu-ocr MCP server（stdio）：ocr（本地 RapidOCR 图片文字识别，中英混识）。

能力面工具（区别于 daoyu 控制面的审批/发图）——独立进程隔离，模型加载开销
只在真调 ocr 时发生（lazy import）。无 DB / 任务 env 依赖；runner 经临时 mcp
config 恒注入本 server（系统条目，不受 /mcp on/off 管辖）。
实测（2026-08-19，rapidocr-onnxruntime 1.4.4）：bytes 直传；result 每行
[box, text, score]，文本取 row[1]；模型随 pip 包分发（无下载步骤）。
"""
import json
import sys
from pathlib import Path

# 本 server 由 `python <repo>/worker/ocr_mcp.py` 拉起：sys.path[0] 是 worker/，
# _ocr 里 `from gateway.media import sniff_image` 需要 repo 根——自举，幂等
# （照 approval_mcp 先例）。
_REPO_ROOT = str(Path(__file__).resolve().parents[1])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

_engine = None


def _get_engine():
    global _engine
    if _engine is None:
        from rapidocr_onnxruntime import RapidOCR   # lazy：首次调用才加载
        _engine = RapidOCR()
    return _engine


def _resp(id_, result):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": id_, "result": result}) + "\n")
    sys.stdout.flush()


def _tools():
    return {"tools": [{
        "name": "ocr",
        "description": "识别图片中的文字（本地 RapidOCR，中英混识，返回按行文本）",
        "inputSchema": {"type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"]},
    }]}


def _ocr(args) -> str:
    from gateway.media import MAX_IMAGE_BYTES, sniff_image
    path = str(args.get("path", ""))
    try:
        raw = Path(path).read_bytes()
    except OSError as e:
        return f"识别失败: 读文件失败 {e}"
    if len(raw) > MAX_IMAGE_BYTES:   # M-1：与入站/send_image 同一 20MB 上限，防巨图喂引擎
        return f"识别失败: 图片超过 {MAX_IMAGE_BYTES // 1024 // 1024}MB 上限"
    ext = sniff_image(raw)            # 白名单 PNG/JPEG/GIF/WebP（magic bytes）
    if ext not in ("png", "jpg"):     # 收紧：GIF/WebP 动图非 OCR 合理输入
        return f"识别失败: 不支持的图片格式 {ext}（OCR 仅支持 PNG/JPEG）"
    result, _elapsed = _get_engine()(raw)     # bytes 直传（实测支持）
    if not result:
        return "识别失败: 未识别出文字"
    return "\n".join(str(row[1]) for row in result)


def main():
    sys.stdin.reconfigure(encoding="utf-8")    # Windows 管道默认 cp936，JSON-RPC 必须 UTF-8
    sys.stdout.reconfigure(encoding="utf-8")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        m = msg.get("method", "")
        if "id" not in msg:            # 通知（如 notifications/initialized）不回包
            continue
        if m == "initialize":
            _resp(msg["id"], {"protocolVersion": "2024-11-05",
                              "capabilities": {"tools": {}},
                              "serverInfo": {"name": "daoyu-ocr",
                                             "version": "0.1.0"}})
        elif m == "tools/list":
            _resp(msg["id"], _tools())
        elif m == "tools/call":
            name = (msg.get("params") or {}).get("name")
            args = (msg.get("params") or {}).get("arguments") or {}
            # 兜底（照 approval_mcp 先例）：磁盘满/引擎崩溃等不得击穿 server
            # 进程——返回 isError 文本（text 带「识别失败: 」前缀，Claude 可读）。
            try:
                if name == "ocr":
                    _resp(msg["id"], {"content": [{"type": "text",
                                                   "text": _ocr(args)}]})
                else:
                    _resp(msg["id"], {"content": [{"type": "text",
                                                   "text": "unknown tool"}],
                                      "isError": True})
            except Exception as e:
                _resp(msg["id"], {"content": [{"type": "text",
                                               "text": f"识别失败: {e!r}"}],
                                  "isError": True})
        elif m == "ping":
            _resp(msg["id"], {})
        else:
            _resp(msg["id"], {})


if __name__ == "__main__":
    main()
