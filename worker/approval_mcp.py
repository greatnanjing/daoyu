"""刀鱼 daoyu MCP server（stdio）：approve（审批）+ send_image（发图），按 DAOYU_TOOLS 装配。

approve 由 claude 子进程经 --permission-prompt-tool 调用：与主进程共享同一
SQLite（WAL 多进程安全）→ 写 approvals 行 + 写 outbox 推微信 → 轮询用户 Y/N →
返回 behavior JSON；300s 超时 = expired = 拒绝。返回契约（claude 2.1.233 实测
报错原文）：必须是
  {"behavior": "allow", "updatedInput": <object>} 或 {"behavior": "deny", "message": <str>}
——纯文本 "approved" 会被 claude 判 invalid permission result，决策实际从未生效。
send_image 是普通工具（非 permission prompt tool），返回纯文本结果。
"""
import json
import os
import secrets
import sqlite3
import sys
import time
from pathlib import Path

POLL_S = 2
TIMEOUT_S = int(os.environ.get("DAOYU_APPROVAL_TIMEOUT", "300"))


def _conn():
    c = sqlite3.connect(os.environ["DAOYU_DB"], timeout=30)
    c.row_factory = sqlite3.Row
    return c


def _resp(id_, result):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": id_, "result": result}) + "\n")
    sys.stdout.flush()


def _tools():
    enabled = {t.strip() for t in os.environ.get("DAOYU_TOOLS", "approve").split(",")
               if t.strip()}
    tools = []
    if "approve" in enabled:
        tools.append({
            "name": "approve",
            "description": "请求用户批准一次工具调用（微信 Y/N，5 分钟超时拒绝）",
            "inputSchema": {"type": "object",
                            "properties": {"tool_name": {"type": "string"},
                                           "input": {"type": "string"}},
                            "required": ["tool_name"]},
        })
    if "send_image" in enabled:
        tools.append({
            "name": "send_image",
            "description": "发送本地图片文件到用户微信（自动上传 CDN 原图；"
                           "支持 PNG/JPEG/GIF/WebP，≤20MB）",
            "inputSchema": {"type": "object",
                            "properties": {"path": {"type": "string"},
                                           "caption": {"type": "string"}},
                            "required": ["path"]},
        })
    return {"tools": tools}


def _behavior_allow(updated_input) -> str:
    return json.dumps({"behavior": "allow", "updatedInput": updated_input},
                      ensure_ascii=False)


def _behavior_deny(message: str) -> str:
    return json.dumps({"behavior": "deny", "message": message}, ensure_ascii=False)


def _approve(conn, args):
    task_id = int(os.environ.get("DAOYU_TASK_ID", "0"))
    to_user = os.environ.get("DAOYU_TO_USER", "")
    tool = args.get("tool_name", "?")
    raw_input = args.get("input", "")
    inp = str(raw_input)[:300]
    # updatedInput 必须是对象（claude 契约）：input 通常是 JSON 字符串，能解析成
    # dict 则原样回传（claude 拿到完整入参继续执行）；否则包一层 {"raw": ...}。
    if isinstance(raw_input, dict):
        updated_input = raw_input
    else:
        try:
            parsed = json.loads(str(raw_input))
        except ValueError:
            parsed = None
        updated_input = parsed if isinstance(parsed, dict) else {"raw": str(raw_input)}
    now = int(time.time())
    cur = conn.execute(
        "INSERT INTO approvals(task_id, to_user, tool_name, input_json, created_at) "
        "VALUES(?,?,?,?,?)", (task_id, to_user, tool, inp, now))
    aid = cur.lastrowid
    conn.execute(
        "INSERT INTO outbox(task_id, to_user, text, created_at) VALUES(?,?,?,?)",
        (task_id, to_user,
         f"🔐 审批请求 #{aid}：允许执行 {tool}？\n{inp}\n回复 Y 允许 / N 拒绝",
         now))
    # 审计同一 commit：审批请求除 approvals 行外也入 audit_log（检索统一性）
    conn.execute(
        "INSERT INTO audit_log(ts, kind, detail) VALUES(?,?,?)",
        (now, "approval_request", f"approval={aid} task={task_id} tool={tool}"))
    conn.commit()
    deadline = time.time() + TIMEOUT_S
    while time.time() < deadline:
        row = conn.execute("SELECT state FROM approvals WHERE id=?", (aid,)).fetchone()
        if row["state"] == "approved":
            return _behavior_allow(updated_input)
        if row["state"] in ("denied", "expired"):
            return _behavior_deny("用户拒绝")
        time.sleep(POLL_S)
    conn.execute("UPDATE approvals SET state='expired', decided_at=? "
                 "WHERE id=? AND state='pending'", (int(time.time()), aid))
    conn.commit()
    return _behavior_deny(f"超时 {TIMEOUT_S // 60} 分钟未应答")


def _send_image(conn, args) -> str:
    """普通工具（非审批）：返回纯文本结果。复制到 data/media/outbound/（防
    Claude 临时文件被清）→ 写 outbox 媒体行（主进程出站协程投递）。"""
    from gateway.media import MAX_IMAGE_BYTES, sniff_image
    task_id = int(os.environ.get("DAOYU_TASK_ID", "0"))
    to_user = os.environ.get("DAOYU_TO_USER", "")
    path = str(args.get("path", ""))
    caption = str(args.get("caption", ""))
    try:
        raw = Path(path).read_bytes()
    except OSError as e:
        return f"读文件失败: {e}"
    if len(raw) > MAX_IMAGE_BYTES:
        return f"图片超过 {MAX_IMAGE_BYTES // 1024 // 1024}MB 上限"
    try:
        ext = sniff_image(raw)
    except Exception as e:
        return f"不是支持的图片格式: {e}"
    out_dir = Path(os.environ["DAOYU_DB"]).parent / "media" / "outbound"
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"img-{secrets.token_hex(8)}.{ext}"
    dest.write_bytes(raw)
    # 与 common/db.py 的 enqueue_media 同构的裸 SQL（孙进程跨进程写主库）——
    # 改 outbox 表结构需同步两处。
    conn.execute(
        "INSERT INTO outbox(task_id, to_user, text, kind, media_path, caption, "
        "created_at) VALUES(?,?,?,?,?,?,?)",
        (task_id, to_user, "", "image", str(dest), caption, int(time.time())))
    conn.commit()
    return f"已排队发送：{dest.name}" + (f"（配文：{caption}）" if caption else "")


def main():
    sys.stdin.reconfigure(encoding="utf-8")     # Windows 管道默认 cp936，JSON-RPC 必须 UTF-8
    sys.stdout.reconfigure(encoding="utf-8")
    conn = _conn()
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
                              "serverInfo": {"name": "daoyu-approval",
                                             "version": "0.1.0"}})
        elif m == "tools/list":
            _resp(msg["id"], _tools())
        elif m == "tools/call":
            name = (msg.get("params") or {}).get("name")
            args = (msg.get("params") or {}).get("arguments") or {}
            # 兜底（M-1/F3）：磁盘满等 I/O 故障不得击穿 server 进程——server 一死
            # 该任务后续全部 mcp__daoyu__* 调用都失败（bg 长任务受损最大）。返回
            # isError 文本（fail-safe：不误发图、不误放行；approve 正常路径的
            # behavior JSON 契约不变，仅异常时降级为 isError——claude 会按失败
            # 处理，不会当成 allow）。
            try:
                if name == "approve":
                    verdict = _approve(conn, args)
                    _resp(msg["id"], {"content": [{"type": "text", "text": verdict}]})
                elif name == "send_image":
                    _resp(msg["id"], {"content": [{"type": "text",
                                                   "text": _send_image(conn, args)}]})
                else:
                    _resp(msg["id"], {"content": [{"type": "text",
                                                   "text": "unknown tool"}],
                                      "isError": True})
            except Exception as e:
                _resp(msg["id"], {"content": [{"type": "text",
                                               "text": f"daoyu server 内部错误: {e!r}"}],
                                  "isError": True})
        elif m == "ping":
            _resp(msg["id"], {})
        else:
            _resp(msg["id"], {})


if __name__ == "__main__":
    main()
