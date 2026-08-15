import asyncio
import sys
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


async def test_run_task_subprocess_fail(db, cfg, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_STDERR", "boom")
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


async def test_cancel_kills_process(db, cfg, monkeypatch, tmp_path):
    # 长运行 fake（slow 模式）：echo 一行后 hang（写 tmp_path，不在仓库留痕）
    hang = tmp_path / "hang_claude.py"
    hang.write_text("import sys, time\nsys.stdin.read()\n"
                    "print('{\"type\":\"result\",\"result\":\"never\"}', flush=True)\n"
                    "time.sleep(300)\n", encoding="utf-8")
    monkeypatch.setenv("FAKE_CLAUDE_SCRIPT", str(FIXTURES / "review_stream.jsonl"))
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
