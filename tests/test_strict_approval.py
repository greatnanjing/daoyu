"""strict 档审批接线（M2 Task 2，M3 起四档通用）：
- runner：四档任务都生成临时合并 mcp config（静态清单 + daoyu server 条目；
  strict=approve,send_image,send_file,notify，其余=send_image,send_file,notify）传给 claude 子进程，任务结束即删。
- gateway：pending 审批存在时 Y/N 单字拦截本地秒回（decide + 回执），其他文本
  照常路由。
- 拼接冒烟：runner 生成的 daoyu 条目手工起真实 approval_mcp 子进程，握手 +
  tools/call + 共享 db 决策 + 返回（claude↔MCP 真实连接留给真机验收）。
"""
import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

from common.models import Budget
from gateway.app import handle_inbound
from worker.cli_builder import APPROVAL_MCP_SERVER, APPROVAL_PROMPT_TOOL
from worker.runner import TaskRunner

FIXTURES = Path(__file__).parent / "fixtures"
ROOT = Path(__file__).resolve().parents[1]
USER = "u@im.wechat"


class RunnerCfg:
    """形状与 test_runner.FakeConfig 一致；static_servers 非 None 时另建静态
    claude/mcp.json（runner 的 repo_root 指向 tmp_path）。"""

    def __init__(self, tmp_path, monkeypatch, static_servers: dict | None = None):
        self.claude_bin = [sys.executable, str(FIXTURES / "fake_claude.py")]
        self.secrets = {"ANTHROPIC_API_KEY": "sk-test"}
        self.repo_root = tmp_path
        self.throttle = {"progress_window_s": 0.0, "page_char_limit": 2000}
        self.budget = Budget(max_turns=10, max_usd=1.0)
        if static_servers is not None:
            d = tmp_path / "claude"
            d.mkdir(exist_ok=True)
            (d / "mcp.json").write_text(
                json.dumps({"mcpServers": static_servers}, ensure_ascii=False),
                encoding="utf-8")
        monkeypatch.setenv("FAKE_CLAUDE_SCRIPT", str(FIXTURES / "review_stream.jsonl"))
        monkeypatch.setenv("FAKE_CLAUDE_STDIN_LOG", str(tmp_path / "stdin.log"))
        monkeypatch.setenv("FAKE_CLAUDE_ARGS_LOG", str(tmp_path / "args.log"))


def _args_log(cfg) -> dict:
    return json.loads((Path(cfg.repo_root) / "args.log").read_text(encoding="utf-8"))


def _texts(db) -> list[str]:
    return [r["text"] for r in db._conn.execute("SELECT text FROM outbox")]


# ---- runner：临时 mcp config ----

async def test_strict_task_temp_merged_mcp_config_and_cleanup(db, tmp_path, monkeypatch):
    static = {"chrome-devtools": {"type": "stdio", "command": "cmd",
                                  "args": ["/c", "npx", "chrome-devtools-mcp@latest"],
                                  "env": {}}}
    cfg = RunnerCfg(tmp_path, monkeypatch, static_servers=static)
    s = db.get_or_create_session(USER, str(tmp_path))
    db.set_policy(s.id, "strict")
    s = db.get_session(s.id)   # 重取：runner 以传入对象的 policy 为准（生产中 pool 领取后重取）
    t = db.create_task(None, s.id, "/review", kind="command")
    await TaskRunner(db, cfg, process_registry={}).run(db.get_task(t), s)

    assert db.get_task(t).state == "done"
    log = _args_log(cfg)
    argv = log["argv"]
    # strict 档追加审批工具引用；引用与临时 config 的 server 键严格一致
    # （不一致时 Claude 找不到审批工具 → 无审批通道 → 该次工具调用被 deny）
    i = argv.index("--permission-prompt-tool")
    assert argv[i + 1] == APPROVAL_PROMPT_TOOL
    assert APPROVAL_PROMPT_TOOL == f"mcp__{APPROVAL_MCP_SERVER}__approve"
    # --mcp-config 指向临时文件而非静态 mcp.json
    j = argv.index("--mcp-config")
    tmp_cfg_path = argv[j + 1]
    assert tmp_cfg_path != str(tmp_path / "claude" / "mcp.json")
    # 遗留#4：临时文件带 daoyu-mcp- 前缀（kill 残留靠启动时按前缀清扫）
    assert os.path.basename(tmp_cfg_path).startswith("daoyu-mcp-")
    # 子进程存活期快照的临时 config 内容：静态清单完整合并 + daoyu 审批条目
    servers = log["mcp_config"]["mcpServers"]
    # 静态清单完整合并：锚定身份字段（type/command/args 前缀）。Linux 有 chrome
    # 装配（~/chrome-libs + headless-shell 在场）时 env/args 会被 inject_linux_chrome
    # 注入增量（env 加 LD_LIBRARY_PATH/清代理、args 追加 --headless 等）——生产
    # 期望行为（服务器实测依赖），不锚定 env。
    merged = servers["chrome-devtools"]
    assert merged["type"] == static["chrome-devtools"]["type"]
    assert merged["command"] == static["chrome-devtools"]["command"]
    assert merged["args"][:len(static["chrome-devtools"]["args"])] == \
        static["chrome-devtools"]["args"]
    entry = servers[APPROVAL_MCP_SERVER]
    assert entry["type"] == "stdio"
    assert entry["command"] == sys.executable
    assert entry["args"] == [str(ROOT / "worker" / "approval_mcp.py")]
    assert entry["env"] == {"DAOYU_DB": os.path.abspath(db.path),
                            "DAOYU_TASK_ID": str(t),
                            "DAOYU_TO_USER": USER,
                            "DAOYU_TOOLS": "approve,send_image,send_file,notify"}
    # 任务结束：临时文件已删，静态文件原样保留
    assert not os.path.exists(tmp_cfg_path)
    assert (tmp_path / "claude" / "mcp.json").exists()


async def test_non_strict_task_merges_send_image_only(db, tmp_path, monkeypatch):
    """M3：非 strict 档同样走临时合并 config（daoyu=send_image,send_file,notify，无审批
    工具引用），静态 mcp.json 不删不改、临时文件结束即删。"""
    static = {"context7": {"type": "stdio", "command": "cmd", "args": [], "env": {}}}
    cfg = RunnerCfg(tmp_path, monkeypatch, static_servers=static)
    s = db.get_or_create_session(USER, str(tmp_path))   # 默认 policy=auto
    t = db.create_task(None, s.id, "hi")
    await TaskRunner(db, cfg, process_registry={}).run(db.get_task(t), s)

    assert db.get_task(t).state == "done"
    log = _args_log(cfg)
    j = log["argv"].index("--mcp-config")
    tmp_cfg_path = log["argv"][j + 1]
    assert tmp_cfg_path != str(tmp_path / "claude" / "mcp.json")
    assert "--permission-prompt-tool" not in log["argv"]   # 审批工具引用仅 strict 档
    servers = log["mcp_config"]["mcpServers"]
    assert servers["context7"] == static["context7"]       # 静态清单完整合并
    assert servers[APPROVAL_MCP_SERVER]["env"]["DAOYU_TOOLS"] == "send_image,send_file,notify"
    static_file = tmp_path / "claude" / "mcp.json"
    assert static_file.exists()   # 静态文件不删不改
    assert json.loads(static_file.read_text(encoding="utf-8"))["mcpServers"]["context7"]
    assert not os.path.exists(tmp_cfg_path)   # 临时文件任务结束即删


async def test_strict_task_failure_cleans_temp_config(db, tmp_path, monkeypatch):
    # 失败路径同样不留临时文件（每次失败都泄漏一个 %TEMP% 文件不可接受）
    cfg = RunnerCfg(tmp_path, monkeypatch, static_servers={})
    monkeypatch.setenv("FAKE_CLAUDE_EXIT_CODE", "1")
    s = db.get_or_create_session(USER, str(tmp_path))
    db.set_policy(s.id, "strict")
    s = db.get_session(s.id)
    t = db.create_task(None, s.id, "boom")
    await TaskRunner(db, cfg, process_registry={}).run(db.get_task(t), s)

    assert db.get_task(t).state in ("failed", "pending")
    argv = _args_log(cfg)["argv"]
    tmp_cfg_path = argv[argv.index("--mcp-config") + 1]
    assert not os.path.exists(tmp_cfg_path)


# ---- gateway：审批 Y/N 拦截 ----

def _inbound(msg_id, text):
    return {"message_id": msg_id, "seq": msg_id, "from_user_id": USER,
            "message_type": 1, "context_token": "CTX",
            "item_list": [{"type": 1, "text_item": {"text": text}}]}


class FakeOutbound:
    def __init__(self):
        self.notified = 0

    def notify(self):
        self.notified += 1


def _gw_cfg(tmp_path):
    return SimpleNamespace(whitelist={USER}, default_cwd=str(tmp_path))


async def test_approval_yes_intercepted(db, tmp_path):
    db.get_or_create_session(USER, str(tmp_path))
    aid = db.create_approval(1, USER, "Bash", '{"command":"rm -rf /tmp/x"}')
    outbound = FakeOutbound()
    await handle_inbound(db, _gw_cfg(tmp_path), None, outbound, _inbound(1, "Y"))

    assert db.get_approval(aid)["state"] == "approved"
    assert any("已允许" in x for x in _texts(db))
    assert db.queue_depth() == 0            # 不入任务队列
    assert outbound.notified == 1           # 即时唤醒出站


async def test_approval_no_intercepted(db, tmp_path):
    db.get_or_create_session(USER, str(tmp_path))
    aid = db.create_approval(1, USER, "Bash", "{}")
    await handle_inbound(db, _gw_cfg(tmp_path), None, FakeOutbound(), _inbound(1, "n"))
    assert db.get_approval(aid)["state"] == "denied"
    assert any("已拒绝" in x for x in _texts(db))
    assert db.queue_depth() == 0


async def test_approval_other_text_not_intercepted(db, tmp_path):
    db.get_or_create_session(USER, str(tmp_path))
    aid = db.create_approval(1, USER, "Bash", "{}")
    await handle_inbound(db, _gw_cfg(tmp_path), None, None, _inbound(1, "随便看看"))
    assert db.get_approval(aid)["state"] == "pending"   # 审批不动
    assert db.queue_depth() == 1                        # 照常入队 chat 任务


async def test_y_without_pending_routes_normally(db, tmp_path):
    # 无 pending 审批时 Y 是普通聊天，不拦截
    db.get_or_create_session(USER, str(tmp_path))
    await handle_inbound(db, _gw_cfg(tmp_path), None, None, _inbound(1, "Y"))
    assert db.queue_depth() == 1


# ---- 拼接冒烟：临时 config 条目手工起真实 approval server ----

async def test_temp_config_entry_boots_real_approval_server(db, tmp_path, monkeypatch):
    """runner 生成的 daoyu 条目（command/args/env）按原样起真实 approval_mcp
    子进程：握手 → tools/call approve → 用条目 env 落 approvals 行（共享 db 可见）
    → decide → server 返回 approved。覆盖 Task 2 与 Task 1 产物的真实拼接。"""
    cfg = RunnerCfg(tmp_path, monkeypatch, static_servers={})
    s = db.get_or_create_session(USER, str(tmp_path))
    db.set_policy(s.id, "strict")
    s = db.get_session(s.id)
    t = db.create_task(None, s.id, "/review", kind="command")
    await TaskRunner(db, cfg, process_registry={}).run(db.get_task(t), s)
    entry = _args_log(cfg)["mcp_config"]["mcpServers"][APPROVAL_MCP_SERVER]

    env = os.environ.copy()
    env.update(entry["env"])
    p = await asyncio.create_subprocess_exec(
        entry["command"], *entry["args"],
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, cwd=str(ROOT), env=env)

    async def send(obj) -> None:
        p.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
        await p.stdin.drain()

    async def recv(timeout: float = 60.0) -> dict:
        # 60s：真实子进程 spawn + 模块导入在负载高/杀软扫描的机器上可远超 10s
        # （实测全量套件在争用机器上整体慢 30x），拼接冒烟测的是接线正确性非速度
        line = await asyncio.wait_for(p.stdout.readline(), timeout)
        assert line, "server 无响应或提前退出"
        return json.loads(line.decode("utf-8"))

    try:
        await send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        assert (await recv())["result"]["serverInfo"]["name"] == "daoyu-approval"
        await send({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                    "params": {"name": "approve",
                               "arguments": {"tool_name": "Bash",
                                             "input": '{"command":"rm -rf /tmp/x"}'}}})
        # server 用条目 env 的 DAOYU_TASK_ID/DAOYU_TO_USER 落审批行（WAL 共享可见）
        row = None
        for _ in range(600):
            row = db.pending_approval(USER)
            if row:
                break
            await asyncio.sleep(0.05)
        assert row and row["task_id"] == t and row["tool_name"] == "Bash"
        assert db.decide_approval(row["id"], "approved") is True
        resp = await recv()
        assert resp["id"] == 2
        # C1：返回必须是 behavior JSON（纯文本会被 claude 判 invalid permission result）
        verdict = json.loads(resp["result"]["content"][0]["text"])
        assert verdict["behavior"] == "allow"
        assert verdict["updatedInput"] == {"command": "rm -rf /tmp/x"}
    finally:
        p.terminate()
        await p.wait()
