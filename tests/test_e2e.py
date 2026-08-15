"""E2E：入站微信消息 → 落盘去重 → 路由 → 任务池 → fake claude → outbox。
验证 M1 验收标准中可自动化部分（真机微信验收另行手动做）。
outbox → iLink 投递链路已在 test_outbound 覆盖，此处 outbound=None 直查 outbox 表。"""
import asyncio
import json
import sys
from pathlib import Path

from common.db import Database
from common.models import Budget
from gateway.app import handle_inbound
from worker.pool import WorkerPool
from worker.runner import TaskRunner

FIXTURES = Path(__file__).parent / "fixtures"


class FakeCfg:
    """形状与 load_config 产物一致：TaskRunner/WorkerPool 直接吃。"""

    def __init__(self, tmp_path, monkeypatch):
        self.repo_root = tmp_path
        self.whitelist = {"u@im.wechat"}
        self.default_cwd = str(tmp_path)
        self.claude_bin = [sys.executable, str(FIXTURES / "fake_claude.py")]
        self.secrets = {"ANTHROPIC_API_KEY": "sk"}
        self.throttle = {"progress_window_s": 0.0, "page_char_limit": 2000,
                         "min_send_interval_s": 0.0, "daily_send_limit": 500}
        self.budget = Budget()
        self.worker = {"concurrency": 2, "poll_interval_s": 0.01}
        self.reconnect = {"session_duration_s": 86400}
        monkeypatch.setenv("FAKE_CLAUDE_SCRIPT", str(FIXTURES / "review_stream.jsonl"))
        monkeypatch.setenv("FAKE_CLAUDE_STDIN_LOG", str(tmp_path / "stdin.log"))


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
    #    事件同步，此处显式落 state 使路由确定、不与任务执行竞态）
    db.set_state("slash_commands", json.dumps(["review", "model"], ensure_ascii=False))
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
