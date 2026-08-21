"""E2E：入站微信消息 → 落盘去重 → 路由 → 任务池 → fake claude → outbox。
M1 验收标准中可自动化部分 + M2 strict 审批往返 / /bg 冒烟（真机微信验收另行手动做）。
outbox → iLink 投递链路已在 test_outbound 覆盖，此处 outbound=None 直查 outbox 表。"""
import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

from common.db import Database
from common.mdclean import md_clean
from common.models import Budget
from gateway.app import handle_inbound
from gateway.outbound import OutboundLoop
from worker.pool import WorkerPool
from worker.runner import TaskRunner, _PROMPT_SUFFIX

FIXTURES = Path(__file__).parent / "fixtures"
ROOT = Path(__file__).resolve().parents[1]


class FakeCfg:
    """形状与 load_config 产物一致：TaskRunner/WorkerPool 直接吃。"""

    def __init__(self, tmp_path, monkeypatch):
        self.repo_root = tmp_path
        self.whitelist = {"u@im.wechat"}
        self.default_cwd = str(tmp_path)
        self.claude_bin = [sys.executable, str(FIXTURES / "fake_claude.py")]
        self.secrets = {"ANTHROPIC_API_KEY": "sk"}
        self.throttle = {"progress_window_s": 0.0, "page_char_limit": 2000,
                         "min_send_interval_s": 0.0, "daily_send_limit": 500,
                         "merge_window_s": 0.0}
        self.budget = Budget()
        self.worker = {"concurrency": 2, "poll_interval_s": 0.01}
        self.reconnect = {"session_duration_s": 86400}
        monkeypatch.setenv("FAKE_CLAUDE_SCRIPT", str(FIXTURES / "review_stream.jsonl"))
        monkeypatch.setenv("FAKE_CLAUDE_STDIN_LOG", str(tmp_path / "stdin.log"))
        monkeypatch.setenv("FAKE_CLAUDE_ARGS_LOG", str(tmp_path / "args.log"))


def inbound(msg_id, text, token="CTX"):
    return {"message_id": msg_id, "seq": msg_id, "from_user_id": "u@im.wechat",
            "message_type": 1, "context_token": token,
            "item_list": [{"type": 1, "text_item": {"text": text}}]}


def _count(db, table):
    return db._conn.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"]


def _texts(db):
    return [r["text"] for r in db._conn.execute("SELECT text FROM outbox")]


async def _wait_done(db, timeout):
    async def done():
        while True:
            states = [r["state"] for r in db._conn.execute(
                "SELECT state FROM tasks WHERE state IN ('pending','running')")]
            if not states:
                return True
            await asyncio.sleep(0.05)
    await asyncio.wait_for(done(), timeout)


async def test_full_pipeline_chat_and_command(tmp_path, monkeypatch):
    cfg = FakeCfg(tmp_path, monkeypatch)
    db = Database(tmp_path / "e2e.db")
    db.ensure_schema()
    runner = TaskRunner(db, cfg, process_registry={})
    pool = WorkerPool(db, cfg, runner=runner, concurrency=2, poll_interval_s=0.01)
    loop_task = asyncio.create_task(pool.run_forever())

    # 1) 普通文本 → chat 任务 + ACK（本地秒回，不等 Claude）
    await handle_inbound(db, cfg, pool, None, inbound(1, "你好"))
    assert any("收到" in t for t in _texts(db))
    # 2) /review → 转发为 command 任务（slash_commands 预置：生产中由首次 init
    #    事件同步，此处显式落 state 使路由确定、不与任务执行竞态）。config/mcp
    #    与真实 init 清单一致（I1 防回归：代理必须赢过转发层）
    db.set_state("slash_commands",
                 json.dumps(["review", "model", "config", "mcp"], ensure_ascii=False))
    await handle_inbound(db, cfg, pool, None, inbound(2, "/review"))
    n_msgs, n_tasks = _count(db, "messages"), _count(db, "tasks")
    assert n_tasks == 2
    # 3) 去重：同 message_id 重投 → 无新消息、无新任务
    await handle_inbound(db, cfg, pool, None, inbound(2, "/review"))
    assert _count(db, "messages") == n_msgs
    assert _count(db, "tasks") == n_tasks
    # 4) 白名单外忽略  5) 群消息忽略（均不落盘、不入队）
    outside = inbound(3, "hi"); outside["from_user_id"] = "stranger"
    group = inbound(4, "hi"); group["group_id"] = "g1"
    await handle_inbound(db, cfg, pool, None, outside)
    await handle_inbound(db, cfg, pool, None, group)
    assert _count(db, "messages") == n_msgs
    assert _count(db, "tasks") == n_tasks
    # 6) 等任务跑完：fake claude 回放流 → 结果入 outbox
    await _wait_done(db, timeout=10)
    loop_task.cancel()
    await asyncio.gather(loop_task, return_exceptions=True)

    texts = _texts(db)
    assert any("审查完成" in t for t in texts), texts
    states = {r["state"] for r in db._conn.execute("SELECT state FROM tasks")}
    assert states == {"done"}, states
    # 转发形态：slash 命令按 "/<命令>" 原样作为 prompt 经 stdin 传给 claude
    # （两个任务同 session 串行，后执行的 /review 是 stdin.log 最终内容）
    assert (tmp_path / "stdin.log").read_text(encoding="utf-8") == "/review"


async def test_bridge_command_local_instant(tmp_path, monkeypatch):
    cfg = FakeCfg(tmp_path, monkeypatch)
    db = Database(tmp_path / "e2e.db")
    db.ensure_schema()
    pool = WorkerPool(db, cfg, concurrency=2)   # 真实接线；不启动调度循环
    await handle_inbound(db, cfg, pool, None, inbound(1, "/status"))
    texts = _texts(db)
    assert any("队列" in t for t in texts)      # /status 文字版回复
    assert db.queue_depth() == 0                # 桥命令本地秒回，不入队
    assert _count(db, "tasks") == 0


# ---------------- M2：strict 审批往返（gateway 全链路 + 真实 approval server） ----------------

async def test_full_pipeline_strict_approval_roundtrip(tmp_path, monkeypatch):
    """strict 审批三段拼接 E2E：段1 /policy strict（桥命令）→ strict 任务经任务池，
    runner 生成临时合并 mcp config（fake claude 子进程侧快照——任务结束即删）；
    段2 按快照条目原样起真实 approval_mcp 子进程，tools/call approve → approvals 行
    + outbox 🔐 审批请求；段3 微信回 Y → gateway 拦截 decide → server 轮询收终态
    回 approved。claude↔MCP server 的真实连接（claude 拉起孙进程）留真机验收。"""
    cfg = FakeCfg(tmp_path, monkeypatch)
    d = tmp_path / "claude"                      # runner 的 strict 合并读静态 mcp.json
    d.mkdir(exist_ok=True)
    (d / "mcp.json").write_text(json.dumps({"mcpServers": {
        "context7": {"type": "stdio", "command": "cmd",
                     "args": ["/c", "npx", "-y", "@upstash/context7-mcp"],
                     "env": {}}}}, ensure_ascii=False), encoding="utf-8")
    db = Database(tmp_path / "e2e.db")
    db.ensure_schema()
    runner = TaskRunner(db, cfg, process_registry={})
    pool = WorkerPool(db, cfg, runner=runner, concurrency=2, poll_interval_s=0.01)
    loop_task = asyncio.create_task(pool.run_forever())
    try:
        # 段1：/policy strict 秒回 → strict chat 任务（普通流，审批无关）跑完
        await handle_inbound(db, cfg, pool, None, inbound(1, "/policy strict"))
        assert any("strict" in t for t in _texts(db))
        await handle_inbound(db, cfg, pool, None, inbound(2, "跑个任务"))
        await _wait_done(db, timeout=15)
        states = {r["state"] for r in db._conn.execute("SELECT state FROM tasks")}
        assert states == {"done"}
        assert any("审查完成" in t for t in _texts(db))
        snap = json.loads((tmp_path / "args.log").read_text(encoding="utf-8"))
        argv = snap["argv"]
        i = argv.index("--permission-prompt-tool")   # strict 任务带审批工具引用
        assert argv[i + 1] == "mcp__daoyu__approve"
        assert "context7" in snap["mcp_config"]["mcpServers"]   # 静态清单完整合并
        entry = snap["mcp_config"]["mcpServers"]["daoyu"]

        # 段2：按快照条目（command/args/env）原样起真实 approval server 子进程
        env = os.environ.copy()
        env.update(entry["env"])
        p = await asyncio.create_subprocess_exec(
            entry["command"], *entry["args"],
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, cwd=str(ROOT), env=env)
        try:
            async def send(obj) -> None:
                p.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
                await p.stdin.drain()

            async def recv(timeout: float = 60.0) -> dict:
                # 60s：真实子进程 spawn 在负载高的机器上可远超 10s（同
                # test_strict_approval 结论），拼接测的是接线正确性非速度
                line = await asyncio.wait_for(p.stdout.readline(), timeout)
                assert line, "approval server 无响应或提前退出"
                return json.loads(line.decode("utf-8"))

            await send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {}})
            assert (await recv())["result"]["serverInfo"]["name"] == "daoyu-approval"
            await send({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                        "params": {"name": "approve",
                                   "arguments": {"tool_name": "Bash",
                                                 "input": '{"command":"echo hi"}'}}})
            row = None
            for _ in range(600):                 # server 写行（WAL 共享可见）
                row = db.pending_approval("u@im.wechat")
                if row:
                    break
                await asyncio.sleep(0.05)
            assert row and row["tool_name"] == "Bash" and row["state"] == "pending"
            assert row["task_id"] == int(entry["env"]["DAOYU_TASK_ID"])
            assert any("🔐" in t and "审批请求" in t for t in _texts(db))

            # 段3：微信回 Y → gateway 拦截 decide → server 2s 轮询收终态回 behavior JSON
            await handle_inbound(db, cfg, pool, None, inbound(3, "Y"))
            assert db.get_approval(row["id"])["state"] == "approved"
            assert any("已允许" in t for t in _texts(db))
            assert db.queue_depth() == 0         # Y/N 拦截本地秒回，不入队
            resp = await recv()
            assert resp["id"] == 2
            verdict = json.loads(resp["result"]["content"][0]["text"])
            assert verdict["behavior"] == "allow"          # C1：behavior JSON 契约
            assert verdict["updatedInput"] == {"command": "echo hi"}
        finally:
            p.terminate()
            await p.wait()
    finally:
        loop_task.cancel()
        await asyncio.gather(loop_task, return_exceptions=True)


# ---------------- M2：/bg 冒烟 ----------------

class FakeBgCfg(FakeCfg):
    """claude_bin 指向 fake_bg_claude（--bg 形态：prompt 走 argv）；bg_poll_s 拉满
    使 watcher 本 E2E 不起轮询子进程（完结推送路径已有 test_bg 单测覆盖）。"""

    def __init__(self, tmp_path, monkeypatch):
        super().__init__(tmp_path, monkeypatch)
        self.claude_bin = [sys.executable, str(FIXTURES / "fake_bg_claude.py")]
        self.worker = {"concurrency": 2, "poll_interval_s": 0.01, "bg_poll_s": 3600}
        self.args_log = tmp_path / "bg_args.log"
        monkeypatch.setenv("FAKE_BG_ARGS_LOG", str(self.args_log))


async def test_full_pipeline_bg_smoke(tmp_path, monkeypatch):
    """/bg 冒烟 E2E：微信入站 → 桥命令建 bg 任务（秒回执）→ 任务池领取 → runner
    启动 claude --bg（fake：stdout 吐 backgrounded id）→ bg_id 落盘 + 🚀 回执 →
    /tasks 显示 [bg]。任务保持 running 交 watcher 接管（单测已覆盖，不重复）。"""
    cfg = FakeBgCfg(tmp_path, monkeypatch)
    db = Database(tmp_path / "e2e.db")
    db.ensure_schema()
    runner = TaskRunner(db, cfg, process_registry={})
    pool = WorkerPool(db, cfg, runner=runner, concurrency=2, poll_interval_s=0.01)
    loop_task = asyncio.create_task(pool.run_forever())
    try:
        await handle_inbound(db, cfg, pool, None, inbound(1, "/bg 写总结"))
        assert any("已转后台" in t for t in _texts(db))    # 桥命令秒回执

        async def launched():
            while True:
                row = db._conn.execute(
                    "SELECT kind, state, claude_bg_id FROM tasks "
                    "WHERE kind='bg'").fetchone()
                if row and row["claude_bg_id"]:
                    return row
                await asyncio.sleep(0.05)
        row = await asyncio.wait_for(launched(), timeout=15)
        assert row["claude_bg_id"] == "ab12cd34"
        for _ in range(200):                              # 🚀 回执紧随 bg_id 落盘
            if any(t.startswith("🚀") for t in _texts(db)):
                break
            await asyncio.sleep(0.05)
        assert any(t.startswith("🚀") and "ab12cd34" in t for t in _texts(db))
        # 启动命令形态：--bg <prompt>，prompt 走 argv（非 stdin）；--settings
        # 使硬 deny 清单与 -p 一致生效（I3）
        argv = json.loads(cfg.args_log.read_text(encoding="utf-8"))["argv"]
        assert "--bg" in argv and argv[-1] == "写总结"
        assert "--settings" in argv

        await handle_inbound(db, cfg, pool, None, inbound(2, "/tasks"))
        assert any("[bg]" in t and "写总结" in t for t in _texts(db))
        assert row["state"] == "running"                  # watcher 接管，不在此完结
    finally:
        loop_task.cancel()
        await asyncio.gather(loop_task, return_exceptions=True)


# ---------------- M5A：通知通道（CLI 子进程 → outbox → 出站链路） ----------------

class _NotifyILink:
    """最小 fake：对齐 OutboundLoop 用到的三个 ilink 签名。"""

    def __init__(self):
        self.sent = []    # (to_user, context_token, text)

    async def sendmessage(self, to_user, context_token, text, token=None, base_url=None):
        self.sent.append((to_user, context_token, text))
        return True

    async def getconfig(self, ilink_user_id, context_token, token=None, base_url=None):
        return "TICKET" if context_token else ""

    async def sendtyping(self, ilink_user_id, ticket, status, token=None, base_url=None):
        pass


async def test_notify_cli_e2e(tmp_path):
    """M5A E2E：CLI 子进程真写 outbox → 出站协程拾取 → sendmessage 收到 🔔。"""
    from common.models import InboundMessage   # 顶部仅 import Budget，按需局部引
    db = Database(tmp_path / "e2e.db")
    db.ensure_schema()
    db.insert_message(InboundMessage(msg_id="n1", from_user="u@im.wechat",
                                     text="hi", context_token="CTX", received_at=1))
    env = {**os.environ, "DAOYU_DB": str(db.path),
           "DAOYU_WHITELIST": "u@im.wechat"}
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "gateway.notify_cli", "部署完成", "耗时 3 分钟",
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        cwd=str(ROOT), env=env)
    _, err = await proc.communicate()
    assert proc.returncode == 0, err.decode("utf-8", "replace")

    fake = _NotifyILink()
    out_cfg = SimpleNamespace(throttle={
        "min_send_interval_s": 0.0, "page_char_limit": 2000,
        "daily_send_limit": 500, "progress_window_s": 0.0})
    # token_ref 必须带非空 token：_drain_once 的 I-1 守卫对空 token 直接 return
    # 不 claim（test_outbound 空窗期用例专测该守卫），此处走正常发送路径
    outbound = OutboundLoop(db, fake, out_cfg, {"token": "T", "base_url": ""}, {})
    loop_task = asyncio.create_task(outbound.run_forever())
    try:
        for _ in range(200):    # ≤10s：出站协程 0.5s 批读
            if any("🔔 部署完成" in t for _, _, t in fake.sent):
                break
            await asyncio.sleep(0.05)
        assert any(t == "🔔 部署完成\n耗时 3 分钟" for _, _, t in fake.sent), fake.sent
    finally:
        loop_task.cancel()
        await asyncio.gather(loop_task, return_exceptions=True)


async def test_e2e_merge_two_messages_single_task(tmp_path, monkeypatch):
    """M5C1 E2E：连发两条 chat → 单任务 prompt 含两段 → fake claude 跑完。"""
    cfg = FakeCfg(tmp_path, monkeypatch)
    cfg.throttle["merge_window_s"] = 0.05      # 测试用短窗口（覆盖 FakeCfg 默认 0.0）
    db = Database(tmp_path / "e2e.db"); db.ensure_schema()
    runner = TaskRunner(db, cfg, process_registry={})
    pool = WorkerPool(db, cfg, runner=runner, concurrency=2, poll_interval_s=0.01)
    loop_task = asyncio.create_task(pool.run_forever())
    try:
        await handle_inbound(db, cfg, pool, None, inbound(1, "第一步"))
        await handle_inbound(db, cfg, pool, None, inbound(2, "第二步"))
        await asyncio.sleep(0.15)                    # 过合并窗口 flush 建任务
        await _wait_done(db, timeout=10)
        prompts = [r["prompt"] for r in db._conn.execute("SELECT prompt FROM tasks")]
        assert prompts == ["第一步\n第二步"]
    finally:
        loop_task.cancel()
        await asyncio.gather(loop_task, return_exceptions=True)


# ---------------- M5C2/M5C3：清洗留存不变量 + 别名全链路 ----------------

async def test_e2e_markdown_result_kept_raw_in_outbox(tmp_path, monkeypatch):
    """M5C2：fake claude 回 Markdown 全语法 → outbox 恒存**原文**（清洗发生在
    投递层——test_outbound.test_outbound_cleans_markdown 已断言清洗后投递，
    两段拼起来即全链；此处钉住「原文留存」与清洗函数对该产物的正确性）。"""
    cfg = FakeCfg(tmp_path, monkeypatch)   # 先构造（内部 setenv 默认流）
    monkeypatch.setenv("FAKE_CLAUDE_SCRIPT",   # 再覆盖为 Markdown 回放流
                       str(FIXTURES / "md_result_stream.jsonl"))
    db = Database(tmp_path / "e2e.db")
    db.ensure_schema()
    runner = TaskRunner(db, cfg, process_registry={})
    pool = WorkerPool(db, cfg, runner=runner, concurrency=2, poll_interval_s=0.01)
    loop_task = asyncio.create_task(pool.run_forever())
    try:
        await handle_inbound(db, cfg, pool, None, inbound(1, "跑部署"))
        await _wait_done(db, timeout=10)
        md = ("## 部署报告\n\n**状态**：`成功`，详见 [日志](http://x/y)。\n\n"
              "| 环境 | 版本 |\n|---|---|\n| prod | 2.1.235 |\n\n"
              "```bash\nsystemctl restart daoyu\n```")
        assert any(t == md for t in _texts(db))       # outbox 原文
        assert md_clean(md) == (
            "【部署报告】\n\n状态：「成功」，详见 日志(http://x/y)。\n\n"
            "• 环境：prod\n• 版本：2.1.235\n\n"
            "    systemctl restart daoyu")
    finally:
        loop_task.cancel()
        await asyncio.gather(loop_task, return_exceptions=True)


async def test_e2e_alias_full_pipeline(tmp_path, monkeypatch):
    """/alias add go <prompt>（桥命令秒回）→ /go 展开建任务 → fake claude 收到
    展开后 prompt（stdin.log）；/t 内置别名等价 /tasks 秒回。"""
    cfg = FakeCfg(tmp_path, monkeypatch)
    db = Database(tmp_path / "e2e.db")
    db.ensure_schema()
    runner = TaskRunner(db, cfg, process_registry={})
    pool = WorkerPool(db, cfg, runner=runner, concurrency=2, poll_interval_s=0.01)
    loop_task = asyncio.create_task(pool.run_forever())
    try:
        await handle_inbound(db, cfg, pool, None, inbound(1, "/alias add go 跑全量测试并总结"))
        assert any("已定义 /go" in t for t in _texts(db))
        await handle_inbound(db, cfg, pool, None, inbound(2, "/go"))
        await _wait_done(db, timeout=10)
        # fake claude 的 stdin 收到展开后 prompt（不是 /go）；展开后是普通文本
        # → runner 恒追加 _PROMPT_SUFFIX 环境约定后缀（brief 精确相等断言未计
        # 入该既有机制，此处按实际形态钉住）
        assert (tmp_path / "stdin.log").read_text(encoding="utf-8") == \
            "跑全量测试并总结" + _PROMPT_SUFFIX
    finally:
        loop_task.cancel()
        await asyncio.gather(loop_task, return_exceptions=True)


async def test_e2e_builtin_alias_t(tmp_path, monkeypatch):
    cfg = FakeCfg(tmp_path, monkeypatch)
    db = Database(tmp_path / "e2e.db")
    db.ensure_schema()
    pool = WorkerPool(db, cfg, concurrency=2)   # 真实接线；不启动调度循环
    await handle_inbound(db, cfg, pool, None, inbound(1, "/t"))
    assert any("没有运行中或排队的任务" in t for t in _texts(db))
    assert _count(db, "tasks") == 0             # bridge 秒回不入队
