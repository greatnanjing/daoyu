import asyncio
import json
import sys
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
        self.throttle = {"progress_window_s": 0.0}   # 测试窗口=0 → 每条都推
        self.budget = Budget(max_turns=10, max_usd=1.0)
        self.page_char_limit = 2000
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
    cfg.throttle = {"progress_window_s": 0.5}
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
