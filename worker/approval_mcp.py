"""刀鱼审批 MCP server（stdio）。claude 子进程经 --permission-prompt-tool 调用。

与主进程共享同一 SQLite（WAL 多进程安全）：approve 被调用 → 写 approvals 行 +
写 outbox 推微信 → 轮询用户 Y/N → 返回 approved/denied；300s 超时 = expired = 拒绝。
"""
import json
import os
import sqlite3
import sys
import time

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
    return {"tools": [{
        "name": "approve",
        "description": "请求用户批准一次工具调用（微信 Y/N，5 分钟超时拒绝）",
        "inputSchema": {"type": "object",
                        "properties": {"tool_name": {"type": "string"},
                                       "input": {"type": "string"}},
                        "required": ["tool_name"]},
    }]}


def _approve(conn, args):
    task_id = int(os.environ.get("DAOYU_TASK_ID", "0"))
    to_user = os.environ.get("DAOYU_TO_USER", "")
    tool = args.get("tool_name", "?")
    inp = str(args.get("input", ""))[:300]
    cur = conn.execute(
        "INSERT INTO approvals(task_id, to_user, tool_name, input_json, created_at) "
        "VALUES(?,?,?,?,?)", (task_id, to_user, tool, inp, int(time.time())))
    conn.execute(
        "INSERT INTO outbox(task_id, to_user, text, created_at) VALUES(?,?,?,?)",
        (task_id, to_user,
         f"🔐 审批请求 #{cur.lastrowid}：允许执行 {tool}？\n{inp}\n回复 Y 允许 / N 拒绝",
         int(time.time())))
    conn.commit()
    aid = cur.lastrowid
    deadline = time.time() + TIMEOUT_S
    while time.time() < deadline:
        row = conn.execute("SELECT state FROM approvals WHERE id=?", (aid,)).fetchone()
        if row["state"] == "approved":
            return "approved"
        if row["state"] in ("denied", "expired"):
            return "denied"
        time.sleep(POLL_S)
    conn.execute("UPDATE approvals SET state='expired', decided_at=? "
                 "WHERE id=? AND state='pending'", (int(time.time()), aid))
    conn.commit()
    return "denied（超时 5 分钟未应答）"


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
            if name != "approve":
                _resp(msg["id"], {"content": [{"type": "text",
                                               "text": "unknown tool"}],
                                  "isError": True})
                continue
            verdict = _approve(conn, args)
            _resp(msg["id"], {"content": [{"type": "text", "text": verdict}]})
        elif m == "ping":
            _resp(msg["id"], {})
        else:
            _resp(msg["id"], {})


if __name__ == "__main__":
    main()
