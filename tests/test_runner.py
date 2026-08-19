import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

import pytest

from common.models import Budget
from worker.runner import TaskRunner

FIXTURES = Path(__file__).parent / "fixtures"


class FakeConfig:
    def __init__(self, tmp_path, monkeypatch):
        self.claude_bin = [sys.executable, str(FIXTURES / "fake_claude.py")]
        self.secrets = {"ANTHROPIC_API_KEY": "sk-test"}
        self.repo_root = tmp_path
        # 形状与真实 load_config 一致：page_char_limit 只在 throttle dict 内（无顶层属性）
        self.throttle = {"progress_window_s": 0.0, "page_char_limit": 2000}  # 窗口=0 → 每条都推
        self.budget = Budget(max_turns=10, max_usd=1.0)
        self.fake_script = str(FIXTURES / "review_stream.jsonl")
        self.stdin_log = tmp_path / "stdin.log"
        monkeypatch.setenv("FAKE_CLAUDE_SCRIPT", self.fake_script)
        monkeypatch.setenv("FAKE_CLAUDE_STDIN_LOG", str(self.stdin_log))


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    return FakeConfig(tmp_path, monkeypatch)


def _outbox_ids(db):
    rows = db._conn.execute("SELECT id FROM outbox ORDER BY id").fetchall()
    return [r["id"] for r in rows]


def _outbox_texts(db):
    return [db.get_outbox(i).text for i in _outbox_ids(db)]


async def test_run_task_success(db, cfg):
    s = db.get_or_create_session("u@im.wechat", str(cfg.repo_root))
    t = db.create_task(None, s.id, "/review", kind="command")
    runner = TaskRunner(db, cfg, process_registry={})
    await runner.run(db.get_task(t), s)

    assert db.get_task(t).state == "done"
    # stdin 收到原样 prompt（不经过 shell）
    assert cfg.stdin_log.read_text(encoding="utf-8") == "/review"
    # 最终回复入 outbox（含 result 文本）
    sent = [db.get_outbox(i) for i in _outbox_ids(db)]
    final = [o for o in sent if "审查完成" in o.text]
    assert final, f"未找到最终回复: {[o.text for o in sent]}"
    # 费用入审计
    assert db.today_cost_usd() == pytest.approx(0.21)
    # slash_commands 已同步进 state
    assert "review" in (db.get_state("slash_commands") or "")
    # 进度（工具事件）也推过
    progress = [o for o in sent if "Bash" in o.text]
    assert progress


async def test_subprocess_env_redirects_claude_config_dir(db, cfg, tmp_path, monkeypatch):
    """C3：机制化隔离宿主 ~/.claude——isolate_claude_config 开关开启时子进程 env
    带 CLAUDE_CONFIG_DIR 指向 <repo_root>/data/claude-home/；默认关（本机 Windows
    代理环境实测该重定向致 claude 启动挂死，Linux 部署实测后再开）。"""
    args_log = tmp_path / "args.log"
    monkeypatch.setenv("FAKE_CLAUDE_ARGS_LOG", str(args_log))
    cfg.worker = {"isolate_claude_config": True}          # 开关开 → 注入
    s = db.get_or_create_session("u@im.wechat", str(cfg.repo_root))
    t = db.create_task(None, s.id, "hi")
    runner = TaskRunner(db, cfg, process_registry={})
    await runner.run(db.get_task(t), s)

    log = json.loads(args_log.read_text(encoding="utf-8"))
    expected = str(cfg.repo_root / "data" / "claude-home")
    assert log["claude_config_dir"] == expected       # 子进程 env 已注入
    assert (cfg.repo_root / "data" / "claude-home").is_dir()   # 目录已自动创建


async def test_subprocess_env_no_config_dir_by_default(db, cfg, tmp_path, monkeypatch):
    """开关默认关 → 不注入 CLAUDE_CONFIG_DIR（本机环境该重定向挂死，见 m2-final-review）。"""
    args_log = tmp_path / "args.log2"
    monkeypatch.setenv("FAKE_CLAUDE_ARGS_LOG", str(args_log))
    s = db.get_or_create_session("u@im.wechat", str(cfg.repo_root))
    t = db.create_task(None, s.id, "hi")
    runner = TaskRunner(db, cfg, process_registry={})
    await runner.run(db.get_task(t), s)

    log = json.loads(args_log.read_text(encoding="utf-8"))
    assert not log.get("claude_config_dir")           # 未注入


def test_runner_init_cleans_stale_mcp_temp_files(db, cfg):
    """遗留#4：主进程被 kill 时临时 mcp config 残留（finally 不执行）——
    新 runner 启动时按 daoyu-mcp- 前缀清扫。"""
    stale = Path(tempfile.gettempdir()) / "daoyu-mcp-stale-cleanup-test.json"
    keep = Path(tempfile.gettempdir()) / "unrelated-not-daoyu.json"
    stale.write_text("{}", encoding="utf-8")
    keep.write_text("{}", encoding="utf-8")
    try:
        TaskRunner(db, cfg, process_registry={})
        assert not stale.exists()            # 同前缀残留被清扫
        assert keep.exists()                 # 其他文件绝不碰
    finally:
        for p in (stale, keep):
            try:
                p.unlink()
            except FileNotFoundError:
                pass


async def test_run_task_subprocess_fail(db, cfg):
    cfg.claude_bin = [sys.executable, "-c",
                      "import sys; sys.stdin.read(); sys.exit(1)"]
    s = db.get_or_create_session("u@im.wechat", str(cfg.repo_root))
    t = db.create_task(None, s.id, "hello")
    runner = TaskRunner(db, cfg, process_registry={})
    await runner.run(db.get_task(t), s)
    # 子进程失败 → 任务 failed（回 pending 或 dead 由 attempts 决定）+ 错误回复入 outbox
    assert db.get_task(t).state in ("failed", "pending")
    assert any("失败" in (db.get_outbox(i).text or "")
               for i in _outbox_ids(db))


async def test_run_task_subprocess_fail_stderr_in_error(db, cfg, tmp_path):
    # stderr 尾部并入错误消息与 audit（排障可见）。
    # 60 行 × 16B = 960B（strip 尾换行后 959 > 500）→ 500 字符截断真实生效
    failer = tmp_path / "fail_claude.py"
    failer.write_text("import sys\nsys.stdin.read()\n"
                      "sys.stderr.write('boom: bad model\\n' * 60)\n"
                      "sys.exit(2)\n", encoding="utf-8")
    cfg.claude_bin = [sys.executable, str(failer)]
    s = db.get_or_create_session("u@im.wechat", str(cfg.repo_root))
    t = db.create_task(None, s.id, "hello")
    runner = TaskRunner(db, cfg, process_registry={})
    await runner.run(db.get_task(t), s)
    texts = _outbox_texts(db)
    err = [x for x in texts if x.startswith("❌")]
    assert err and "退出码 2" in err[0] and "boom: bad model" in err[0]
    # 精确断言：恰保留尾部 500 字符（若未截断则为前缀+959，可区分）
    assert len(err[0]) == len("❌ 任务失败：claude 退出码 2: ") + 500
    audit = db._conn.execute(
        "SELECT detail FROM audit_log WHERE kind='task_failed'").fetchall()
    assert audit and "boom: bad model" in audit[-1]["detail"]


async def test_progress_streamed_before_exit(db, cfg, tmp_path):
    # FR-8 回归：工具进度必须在子进程仍在运行时就已推送（不等退出后批量解析）
    tool_line = ('{"type":"stream_event","event":{"type":"content_block_start",'
                 '"index":0,"content_block":{"type":"tool_use","id":"t1","name":"Bash"}}}')
    slow = tmp_path / "slow_claude.py"
    slow.write_text("import sys, time\n"
                    "sys.stdin.read()\n"
                    f"print({tool_line!r}, flush=True)\n"
                    "time.sleep(300)\n", encoding="utf-8")
    cfg.claude_bin = [sys.executable, str(slow)]
    s = db.get_or_create_session("u@im.wechat", str(cfg.repo_root))
    t = db.create_task(None, s.id, "long")
    registry = {}
    runner = TaskRunner(db, cfg, process_registry=registry)
    task_async = asyncio.create_task(runner.run(db.get_task(t), s))

    deadline = time.monotonic() + 5
    progress = []
    while time.monotonic() < deadline:
        progress = [x for x in _outbox_texts(db) if "Bash" in x]
        if progress:
            break
        await asyncio.sleep(0.05)
    # 进度到达时进程仍在 sleep(300)、任务未完结 → 确为实时推送
    assert progress, "子进程运行期间未收到进度（非实时流式）"
    assert db.get_task(t).state == "pending"   # 未经 claim，pending 即"未完结"
    assert not [x for x in _outbox_texts(db) if "审查完成" in x or "never" in x]

    registry[t].kill()
    await asyncio.wait_for(task_async, timeout=5)


async def test_cancel_kills_process(db, cfg, tmp_path):
    # 长运行 fake（slow 模式）：echo 一行后 hang（写 tmp_path，不在仓库留痕）
    hang = tmp_path / "hang_claude.py"
    hang.write_text("import sys, time\nsys.stdin.read()\n"
                    "print('{\"type\":\"result\",\"result\":\"never\"}', flush=True)\n"
                    "time.sleep(300)\n", encoding="utf-8")
    cfg.claude_bin = [sys.executable, str(hang)]
    cfg.throttle = {"progress_window_s": 0.5, "page_char_limit": 2000}
    s = db.get_or_create_session("u@im.wechat", str(cfg.repo_root))
    t = db.create_task(None, s.id, "long")
    registry = {}
    runner = TaskRunner(db, cfg, process_registry=registry)
    task_async = asyncio.create_task(runner.run(db.get_task(t), s))
    await asyncio.sleep(0.5)          # 等子进程注册
    assert t in registry
    registry[t].kill()
    await asyncio.wait_for(task_async, timeout=5)
    assert db.get_task(t).state in ("canceled", "failed")
    texts = _outbox_texts(db)
    assert "已取消。" in texts
    assert not any("never" in x for x in texts)   # 半截 result 不推


async def test_long_result_line_over_64kb(db, cfg, tmp_path):
    # 回归（readline 64KB 行上限曾抛 ValueError → 孤儿进程 + 卡 running）：
    # result 行内嵌 ~70KB 完整回复（长报告/重写大文件常态），必须整行读完、
    # 任务 done、内容分页完整送达
    payload = "x" * 70000
    result_line = json.dumps({"type": "result", "result": payload,
                              "total_cost_usd": 0.1, "is_error": False})
    assert len(result_line.encode("utf-8")) > 64 * 1024   # 确实越过旧 64KB 上限
    big = tmp_path / "big_claude.py"
    big.write_text("import sys\nsys.stdin.read()\n"
                   f"print({result_line!r}, flush=True)\n", encoding="utf-8")
    cfg.claude_bin = [sys.executable, str(big)]
    s = db.get_or_create_session("u@im.wechat", str(cfg.repo_root))
    t = db.create_task(None, s.id, "report")
    registry = {}
    runner = TaskRunner(db, cfg, process_registry=registry)
    await runner.run(db.get_task(t), s)

    assert db.get_task(t).state == "done"
    assert not registry                                        # 进程已回收注销
    pages = [x for x in _outbox_texts(db) if "(第 " in x]
    assert len(pages) == 35                                    # 70000 / 2000
    assert all(p.endswith("x" * 2000) for p in pages)          # 每页内容无缺损


async def test_result_line_over_stream_limit_fails_gracefully(db, cfg, tmp_path):
    # 兜底路径：单行 > 8MB 上限（真实回复不会到，纯防御）→ readline 异常必须被
    # 捕获 → 进程 kill 收尸、任务落终态、用户有反馈（而非孤儿进程 + 零反馈）
    huge = tmp_path / "huge_claude.py"
    huge.write_text(
        "import sys\n"
        "sys.stdin.read()\n"
        "print('{\"type\":\"result\",\"result\":\"' + 'x' * (8 * 1024 * 1024 + 100)"
        " + '\"}', flush=True)\n", encoding="utf-8")
    cfg.claude_bin = [sys.executable, str(huge)]
    s = db.get_or_create_session("u@im.wechat", str(cfg.repo_root))
    t = db.create_task(None, s.id, "huge")
    registry = {}
    runner = TaskRunner(db, cfg, process_registry=registry)
    await runner.run(db.get_task(t), s)   # 正常返回 = kill + wait 已收尸

    assert db.get_task(t).state in ("failed", "pending")       # 不卡 running
    assert not registry
    err = [x for x in _outbox_texts(db) if x.startswith("❌")]
    assert err and "输出流读取/解析异常" in err[0]               # 用户有反馈


async def test_budget_exhausted_dead_no_retry(db, cfg, tmp_path):
    # I-3 回归：--max-budget-usd 耗尽 → result subtype=error_max_budget_usd、
    # 进程非零退出。预算闸是每次调用的上限，重试=带全新预算再烧一遍（违背
    # NFR-5 每任务上限）→ 必须直接 dead 不回 pending，且 result 文本（claude
    # 印出的失败原因，stderr 可能为空）要进错误消息与 audit。
    result_line = json.dumps({"type": "result", "subtype": "error_max_budget_usd",
                              "result": "Budget limit of $1.00 exceeded",
                              "total_cost_usd": 1.0, "is_error": True})
    budget_claude = tmp_path / "budget_claude.py"
    budget_claude.write_text("import sys\nsys.stdin.read()\n"
                             "sys.stderr.write('max budget exceeded\\n')\n"
                             f"print({result_line!r}, flush=True)\n"
                             "sys.exit(1)\n", encoding="utf-8")
    cfg.claude_bin = [sys.executable, str(budget_claude)]
    s = db.get_or_create_session("u@im.wechat", str(cfg.repo_root))
    t = db.create_task(None, s.id, "big job")
    claimed = db.claim_next_pending({s.id})           # 生产路径：pool 领取后执行
    assert claimed.id == t and claimed.attempts == 1
    runner = TaskRunner(db, cfg, process_registry={})
    await runner.run(db.get_task(t), s)

    row = db.get_task(t)
    assert row.state == "dead"                        # 非 pending（未回队重试）
    assert row.attempts == 1                          # 没有第二次领取/重跑
    err = [x for x in _outbox_texts(db) if x.startswith("❌")]
    assert err and "Budget limit" in err[0]
    assert "error_max_budget_usd" in err[0] and "预算/回合上限" in err[0]
    audit = db._conn.execute(
        "SELECT detail FROM audit_log WHERE kind='task_failed'").fetchall()
    assert audit and "Budget limit" in audit[-1]["detail"]


async def test_fail_error_message_includes_result_text(db, cfg, tmp_path):
    # I-3：普通失败路径（无 error_* subtype）的错误消息并入 result 文本——
    # 失败原因印在 result 里时 stderr 可能为空，只给 "claude 退出码 N" 会丢排障信息
    result_line = json.dumps({"type": "result", "result": "API 抖动，请稍后重试",
                              "total_cost_usd": 0.0, "is_error": False})
    quitter = tmp_path / "quiet_fail_claude.py"
    quitter.write_text("import sys\nsys.stdin.read()\n"
                       f"print({result_line!r}, flush=True)\n"
                       "sys.exit(3)\n", encoding="utf-8")
    cfg.claude_bin = [sys.executable, str(quitter)]
    s = db.get_or_create_session("u@im.wechat", str(cfg.repo_root))
    t = db.create_task(None, s.id, "hello")
    runner = TaskRunner(db, cfg, process_registry={})
    await runner.run(db.get_task(t), s)

    assert db.get_task(t).state == "pending"          # 普通失败：attempts<3 回 pending
    err = [x for x in _outbox_texts(db) if x.startswith("❌")]
    assert err and "退出码 3" in err[0] and "API 抖动" in err[0]


async def test_daoyu_mcp_merged_all_policies(db, cfg, tmp_path, monkeypatch):
    """M3：四档（-p 路径）都合并 daoyu 条目——strict=approve,send_image，
    其余=send_image；静态清单 server 保留。"""
    args_log = tmp_path / "mcp_args.log"
    monkeypatch.setenv("FAKE_CLAUDE_ARGS_LOG", str(args_log))
    claude_dir = cfg.repo_root / "claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    (claude_dir / "mcp.json").write_text(json.dumps({"mcpServers": {"context7": {
        "type": "stdio", "command": "x", "args": []}}}), encoding="utf-8")
    for policy, expect_tools in (("auto", "send_image"),
                                 ("strict", "approve,send_image"),
                                 ("bypass", "send_image"),
                                 ("plan", "send_image")):
        s = db.get_or_create_session("u@im.wechat", str(cfg.repo_root))
        db.set_policy(s.id, policy)
        s = db.get_session(s.id)                 # 刷新拿带 policy 的绑定
        t = db.create_task(None, s.id, "hi", kind="chat")
        runner = TaskRunner(db, cfg, process_registry={})
        await runner.run(db.get_task(t), s)
        log = json.loads(args_log.read_text(encoding="utf-8"))
        servers = log["mcp_config"]["mcpServers"]     # fake_claude 已快照文件内容
        assert "context7" in servers, policy          # 静态清单保留
        entry = servers["daoyu"]
        assert entry["type"] == "stdio"
        assert entry["env"]["DAOYU_TOOLS"] == expect_tools, policy
        assert entry["env"]["DAOYU_TASK_ID"] == str(t)


async def test_daoyu_mcp_config_bad_static_json_fails_open(db, cfg, tmp_path, monkeypatch):
    """I-2/F2：静态 mcp.json 存在但 JSON 损坏 → 按空清单合并（daoyu-only），
    任务不因解析异常穿透而 failed——四档全任务都走该装配路径，坏文件须与
    缺文件同策略 fail-open。"""
    args_log = tmp_path / "mcp_bad_args.log"
    monkeypatch.setenv("FAKE_CLAUDE_ARGS_LOG", str(args_log))
    claude_dir = cfg.repo_root / "claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    (claude_dir / "mcp.json").write_text("{oops: 不是合法 JSON", encoding="utf-8")
    s = db.get_or_create_session("u@im.wechat", str(cfg.repo_root))
    t = db.create_task(None, s.id, "hi", kind="chat")
    runner = TaskRunner(db, cfg, process_registry={})
    await runner.run(db.get_task(t), s)

    assert db.get_task(t).state == "done"          # 任务不失败（fail-open）
    log = json.loads(args_log.read_text(encoding="utf-8"))
    servers = log["mcp_config"]["mcpServers"]      # fake_claude 已快照文件内容
    assert set(servers) == {"daoyu"}               # 坏清单按空合并，无残留条目
    assert servers["daoyu"]["env"]["DAOYU_TOOLS"] == "send_image"   # auto 档


async def test_mcp_disabled_filtered_and_platform_expanded(
        db, cfg, tmp_path, monkeypatch):
    """余项 A：静态 mcp.json 的 disabled 条目不进临时 mcp config；
    Windows 白名单命令（npx）在合并层包 cmd /c，Linux 直传。
    fake_claude 快照的是合并后文件内容——disabled 键本身绝不进临时文件。"""
    args_log = tmp_path / "mcp_a2.log"
    monkeypatch.setenv("FAKE_CLAUDE_ARGS_LOG", str(args_log))
    monkeypatch.setattr("worker.runner.sys.platform", "win32")
    claude_dir = cfg.repo_root / "claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    (claude_dir / "mcp.json").write_text(json.dumps({
        "mcpServers": {
            "context7": {"type": "stdio", "command": "npx",
                          "args": ["-y", "@upstash/context7-mcp"], "env": {}},
            "web-reader": {"type": "stdio", "command": "uvx",
                            "args": ["mcp-server-fetch"], "env": {}},
        },
        "disabled": ["web-reader", "ghost"],   # ghost：残留名静默忽略
    }), encoding="utf-8")
    s = db.get_or_create_session("u@im.wechat", str(cfg.repo_root))
    t = db.create_task(None, s.id, "hi", kind="chat")
    runner = TaskRunner(db, cfg, process_registry={})
    await runner.run(db.get_task(t), s)

    assert db.get_task(t).state == "done"
    log = json.loads(args_log.read_text(encoding="utf-8"))
    raw = log["mcp_config"]                      # fake_claude 快照的文件内容
    servers = raw["mcpServers"]
    assert "context7" in servers                 # 启用条目保留
    assert servers["context7"]["command"] == "cmd"          # Windows 包装
    assert servers["context7"]["args"][0] == "/c"
    assert "web-reader" not in servers           # disabled 过滤
    assert "ghost" not in servers                # 残留名忽略（本就不在清单）
    assert "disabled" not in raw                 # disabled 键不进临时文件
    assert "daoyu" in servers                    # 系统条目恒注入


async def test_mcp_disabled_absent_means_all_enabled(
        db, cfg, tmp_path, monkeypatch):
    """旧文件无 disabled 键 → 全部启用（幂等兼容）。"""
    args_log = tmp_path / "mcp_a2b.log"
    monkeypatch.setenv("FAKE_CLAUDE_ARGS_LOG", str(args_log))
    claude_dir = cfg.repo_root / "claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    (claude_dir / "mcp.json").write_text(json.dumps({"mcpServers": {
        "context7": {"type": "stdio", "command": "x", "args": []}}}),
        encoding="utf-8")
    s = db.get_or_create_session("u@im.wechat", str(cfg.repo_root))
    t = db.create_task(None, s.id, "hi", kind="chat")
    runner = TaskRunner(db, cfg, process_registry={})
    await runner.run(db.get_task(t), s)

    log = json.loads(args_log.read_text(encoding="utf-8"))
    assert "context7" in log["mcp_config"]["mcpServers"]


async def test_mcp_disabled_non_string_elements_fail_open(
        db, cfg, tmp_path, monkeypatch):
    """审查 Minor-3：disabled 为 list 且含非字符串元素（5 可哈希、{} 不可
    哈希）时只收字符串——不抛 TypeError（与该层 fail-open 语义一致），
    真实条目名照常过滤、其余条目与 daoyu 系统条目不受影响。"""
    args_log = tmp_path / "mcp_a2c.log"
    monkeypatch.setenv("FAKE_CLAUDE_ARGS_LOG", str(args_log))
    claude_dir = cfg.repo_root / "claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    (claude_dir / "mcp.json").write_text(json.dumps({
        "mcpServers": {
            "ghost": {"type": "stdio", "command": "x", "args": []},
            "context7": {"type": "stdio", "command": "x", "args": []},
        },
        "disabled": ["ghost", 5, {}],   # 混合：真名 + 可哈希/不可哈希非字符串
    }), encoding="utf-8")
    s = db.get_or_create_session("u@im.wechat", str(cfg.repo_root))
    t = db.create_task(None, s.id, "hi", kind="chat")
    runner = TaskRunner(db, cfg, process_registry={})
    await runner.run(db.get_task(t), s)   # 旧实现 set([{}, ...]) 在此抛 TypeError

    assert db.get_task(t).state == "done"          # 不因坏元素失败
    log = json.loads(args_log.read_text(encoding="utf-8"))
    servers = log["mcp_config"]["mcpServers"]      # fake_claude 快照的文件内容
    assert "ghost" not in servers                  # 真实条目名照常被过滤
    assert "context7" in servers                   # 其余条目照常
    assert "daoyu" in servers                      # 系统条目恒在
