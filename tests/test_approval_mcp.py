"""approvals 表 + 审批 MCP server（stdio JSON-RPC）测试：db 层 CRUD + 子进程级真实握手。"""
import asyncio
import json
import os
import sys
import time
from pathlib import Path

import pytest

from common.db import Database
from common.models import InboundMessage

ROOT = Path(__file__).resolve().parents[1]
TO_USER = "u@im.wechat"


def test_approval_crud(db):
    db.insert_message(InboundMessage(msg_id="1", from_user="u@im.wechat",
                                     text="hi", context_token="c", received_at=1))
    aid = db.create_approval(task_id=1, to_user="u@im.wechat",
                             tool_name="Bash", input_json='{"command":"rm -rf /tmp/x"}')
    row = db.pending_approval("u@im.wechat")
    assert row["id"] == aid and row["state"] == "pending"
    assert db.decide_approval(aid, "approved") is True
    assert db.decide_approval(aid, "denied") is False      # 终态不可再改
    assert db.pending_approval("u@im.wechat") is None


def test_approval_earliest_pending_first(db):
    a1 = db.create_approval(task_id=1, to_user="u@im.wechat", tool_name="Bash", input_json="{}")
    db.create_approval(task_id=1, to_user="u@im.wechat", tool_name="Write", input_json="{}")
    assert db.pending_approval("u@im.wechat")["id"] == a1  # 最早一条优先（Y/N 顺序审批）
    assert db.pending_approval("other@im.wechat") is None  # 他人无 pending
    assert db.decide_approval(a1, "expired") is True
    assert db.get_approval(a1)["state"] == "expired" and db.get_approval(a1)["decided_at"]


def test_pending_approval_ignores_stale_rows(db):
    """I4：主进程被 cgroup 整组杀死时 server 来不及自置 expired → 永久 pending
    行不得劫持 Y/N 拦截。超 330s（300 超时 + 30 余量）的 pending 视为 stale。"""
    aid = db.create_approval(task_id=1, to_user="u@im.wechat",
                             tool_name="Bash", input_json="{}")
    db._conn.execute("UPDATE approvals SET created_at=? WHERE id=?",
                     (int(time.time()) - 400, aid))
    db._conn.commit()
    assert db.pending_approval("u@im.wechat") is None        # 400s 前的 stale 行
    fresh = db.create_approval(task_id=1, to_user="u@im.wechat",
                               tool_name="Write", input_json="{}")
    assert db.pending_approval("u@im.wechat")["id"] == fresh  # 新行不受影响


def test_approval_schema_in_master(db):
    names = {r[0] for r in db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "approvals" in names
    idx = {r[0] for r in db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    assert "idx_approvals_state" in idx


# ---- 子进程级：真实 stdio JSON-RPC 握手与审批往返 ----
# 不用 conftest 的 db fixture：需要把库路径经 env 传给 server 子进程（WAL 多进程共享）。

def _srv_env(db_path: str, timeout_s: str = "300") -> dict:
    e = os.environ.copy()
    e["DAOYU_DB"] = db_path
    e["DAOYU_TASK_ID"] = "1"
    e["DAOYU_TO_USER"] = TO_USER
    e["DAOYU_APPROVAL_TIMEOUT"] = timeout_s
    return e


async def _start(env) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        sys.executable, str(ROOT / "worker" / "approval_mcp.py"),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, cwd=str(ROOT), env=env)


async def _send(p, obj) -> None:
    p.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
    await p.stdin.drain()


async def _recv(p, timeout: float = 10.0) -> dict:
    line = await asyncio.wait_for(p.stdout.readline(), timeout)
    assert line, "server 无响应或提前退出"
    return json.loads(line.decode("utf-8"))


async def _wait_pending(db, timeout: float = 5.0):
    """等子进程落 approvals 行（每条 SELECT 都看到最新已提交数据）。"""
    for _ in range(int(timeout / 0.05)):
        row = db.pending_approval(TO_USER)
        if row:
            return row
        await asyncio.sleep(0.05)
    raise AssertionError("approvals 行未出现")


async def test_mcp_handshake_and_approve_roundtrip(tmp_path):
    db = Database(tmp_path / "mcp.db")
    db.ensure_schema()
    p = await _start(_srv_env(db.path))
    try:
        # initialize：按请求 id 回 serverInfo
        await _send(p, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        resp = await _recv(p)
        assert resp["id"] == 1
        assert resp["result"]["serverInfo"]["name"] == "daoyu-approval"

        # notifications/initialized 是通知：不回包
        await _send(p, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(p.stdout.readline(), 0.5)

        # tools/list：approve 在列
        await _send(p, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        resp = await _recv(p)
        assert resp["id"] == 2
        assert "approve" in [t["name"] for t in resp["result"]["tools"]]

        # tools/call approve：子进程写行+推 outbox 后阻塞轮询；主进程决定 → 回
        # behavior JSON（C1：纯文本 "approved" 会被 claude 判 invalid permission result）
        await _send(p, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                        "params": {"name": "approve",
                                   "arguments": {"tool_name": "Bash",
                                                 "input": '{"command":"rm -rf /tmp/x"}'}}})
        row = await _wait_pending(db)
        assert row["tool_name"] == "Bash"
        assert row["task_id"] == 1 and row["state"] == "pending"
        push = db._conn.execute(
            "SELECT text FROM outbox ORDER BY id DESC LIMIT 1").fetchone()
        assert push and "Bash" in push["text"]      # 审批请求已推 outbox
        kinds = {r["kind"] for r in db._conn.execute("SELECT kind FROM audit_log")}
        assert "approval_request" in kinds          # M3：审批请求入审计（同 commit）

        assert db.decide_approval(row["id"], "approved") is True
        resp = await _recv(p)
        assert resp["id"] == 3
        content = resp["result"]["content"]
        assert content[0]["type"] == "text"
        verdict = json.loads(content[0]["text"])
        assert verdict["behavior"] == "allow"
        assert verdict["updatedInput"] == {"command": "rm -rf /tmp/x"}
    finally:
        p.terminate()
        await p.wait()


async def test_mcp_denied_verdict_is_behavior_json(tmp_path):
    """C1 契约：denied 分支同样必须返回合法 behavior JSON（deny 带 message）。"""
    db = Database(tmp_path / "mcp.db")
    db.ensure_schema()
    p = await _start(_srv_env(db.path, timeout_s="30"))
    try:
        await _send(p, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        assert (await _recv(p))["id"] == 1
        await _send(p, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                        "params": {"name": "approve",
                                   "arguments": {"tool_name": "Bash", "input": "不是JSON"}}})
        row = await _wait_pending(db)
        assert db.decide_approval(row["id"], "denied") is True
        resp = await _recv(p)
        verdict = json.loads(resp["result"]["content"][0]["text"])
        assert verdict == {"behavior": "deny", "message": "用户拒绝"}
    finally:
        p.terminate()
        await p.wait()


async def test_mcp_approval_timeout_denies(tmp_path):
    db = Database(tmp_path / "mcp.db")
    db.ensure_schema()
    p = await _start(_srv_env(db.path, timeout_s="2"))
    try:
        await _send(p, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        assert (await _recv(p))["id"] == 1

        await _send(p, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                        "params": {"name": "approve",
                                   "arguments": {"tool_name": "Write", "input": "x.txt"}}})
        row = await _wait_pending(db)
        # 不决定，等子进程 2s 超时自置 expired（读包给足裕量）
        resp = await _recv(p, timeout=15)
        assert resp["id"] == 2
        verdict = json.loads(resp["result"]["content"][0]["text"])
        assert verdict["behavior"] == "deny"
        assert "超时" in verdict["message"]
        final = db.get_approval(row["id"])
        assert final["state"] == "expired" and final["decided_at"] is not None
    finally:
        p.terminate()
        await p.wait()
