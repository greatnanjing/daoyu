"""M2 Task 3: /bg 长任务——路由/桥命令、runner 启动分支、watcher 轮询、取消。

全部用 fake：fake_bg_claude 模拟 claude --bg 的 stdout；watcher 的 agents --json
与 resume 兜底、cancel 的 claude stop 均以注入 fake 函数替身，不碰真实 CLI。
"""
import asyncio
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from common.models import Budget
from gateway.bridge import execute_bridge
from gateway.router import Route, route
from worker.pool import WorkerPool
from worker.runner import TaskRunner

FIXTURES = Path(__file__).parent / "fixtures"
NOW_MS = lambda: int(time.time() * 1000)   # noqa: E731  agents 条目 startedAt 是 ms


def _texts(db):
    return [r["text"] for r in db._conn.execute("SELECT text FROM outbox")]


def _route(cmd, args="", kind="bridge"):
    return Route(kind=kind, command=cmd, args=args, detail={})


class NullRunner:
    """watcher 测试用：不真正执行任务（bg 任务由 watcher 接管，不经 runner）。"""

    procs = {}

    async def run(self, task, session):
        pass


# ---------------- Step 1: 路由与桥命令 ----------------

def test_bg_routes_to_bridge():
    r = route("/bg 跑个大任务", set())
    assert r.kind == "bridge" and r.command == "bg" and r.args == "跑个大任务"


def test_bg_beats_forward_layer():
    # 桥命令层优先：即便 headless slash_commands 里也有 bg，仍本地处理
    r = route("/bg x", {"bg"})
    assert r.kind == "bridge"


class FakeBridgeCfg:
    default_cwd = "/repo"
    reconnect = {"session_duration_s": 86400}


class RecordingPool:
    def __init__(self):
        self.submitted = 0

    async def submit_check(self):
        self.submitted += 1


async def test_bg_without_args_shows_usage(db):
    db.get_or_create_session("u@im.wechat", "/repo")
    pool = RecordingPool()
    reply = await execute_bridge(db, pool, _route("bg", ""), "u@im.wechat",
                                 FakeBridgeCfg())
    assert "用法" in reply
    assert db._conn.execute("SELECT COUNT(*) c FROM tasks").fetchone()["c"] == 0


async def test_bg_creates_bg_task(db):
    db.get_or_create_session("u@im.wechat", "/repo")
    db.set_active_cwd("u@im.wechat", "/repo")
    pool = RecordingPool()
    reply = await execute_bridge(db, pool, _route("bg", "跑个大活"),
                                 "u@im.wechat", FakeBridgeCfg())
    row = db._conn.execute("SELECT * FROM tasks").fetchone()
    assert row["kind"] == "bg" and row["state"] == "pending"
    assert row["prompt"] == "跑个大活"
    assert db.get_session(row["session_id"]).cwd == "/repo"
    assert pool.submitted == 1                    # 已唤醒调度
    assert "/tasks" in reply and "/cancel" in reply


# ---------------- Step 2: runner bg 启动分支 ----------------

class BgConfig:
    def __init__(self, tmp_path, monkeypatch):
        self.claude_bin = [sys.executable, str(FIXTURES / "fake_bg_claude.py")]
        self.secrets = {"ANTHROPIC_API_KEY": "sk-test"}
        self.repo_root = tmp_path
        self.throttle = {"progress_window_s": 0.0, "page_char_limit": 2000}
        self.budget = Budget(max_turns=10, max_usd=1.0)
        self.worker = {}
        self.args_log = tmp_path / "bg_args.log"
        monkeypatch.setenv("FAKE_BG_ARGS_LOG", str(self.args_log))


@pytest.fixture
def cfg_bg(tmp_path, monkeypatch):
    return BgConfig(tmp_path, monkeypatch)


async def test_runner_bg_launch_returns_immediately(db, cfg_bg):
    s = db.get_or_create_session("u@im.wechat", str(cfg_bg.repo_root))
    t = db.create_task(None, s.id, "跑个大活", kind="bg")
    db.claim_next_pending({s.id})                 # 生产路径领取 → running
    runner = TaskRunner(db, cfg_bg, process_registry={})
    await asyncio.wait_for(runner.run(db.get_task(t), s), timeout=10)  # 立即返回

    task = db.get_task(t)
    assert task.state == "running"                # 不 finish，watcher 接管
    assert task.claude_bg_id == "ab12cd34"
    # 回执入 outbox（含任务号）
    assert any("后台" in x and f"#{t}" in x for x in _texts(db)), _texts(db)
    # prompt 走 argv 参数（--bg <prompt>），无 stdin 需求
    argv = json.loads(cfg_bg.args_log.read_text(encoding="utf-8"))["argv"]
    assert "--bg" in argv and argv[-1] == "跑个大活"
    # 保守 flag 集：--bare + 预算 + 权限档（无 -p 全量 flag）
    assert "--bare" in argv and "--max-turns" in argv and "--max-budget-usd" in argv
    assert "--permission-mode" in argv


async def test_runner_bg_launch_nonzero_exit_fails(db, cfg_bg, monkeypatch):
    monkeypatch.setenv("FAKE_BG_EXIT_CODE", "1")
    s = db.get_or_create_session("u@im.wechat", str(cfg_bg.repo_root))
    t = db.create_task(None, s.id, "跑个大活", kind="bg")
    db.claim_next_pending({s.id})
    runner = TaskRunner(db, cfg_bg, process_registry={})
    await asyncio.wait_for(runner.run(db.get_task(t), s), timeout=10)
    assert db.get_task(t).state in ("failed", "pending")   # 走 _fail（可重试）
    assert any(x.startswith("❌") for x in _texts(db))


async def test_runner_bg_no_id_in_stdout_fails(db, cfg_bg, monkeypatch):
    monkeypatch.setenv("FAKE_BG_NO_ID", "1")
    s = db.get_or_create_session("u@im.wechat", str(cfg_bg.repo_root))
    t = db.create_task(None, s.id, "跑个大活", kind="bg")
    db.claim_next_pending({s.id})
    runner = TaskRunner(db, cfg_bg, process_registry={})
    await asyncio.wait_for(runner.run(db.get_task(t), s), timeout=10)
    assert db.get_task(t).state in ("failed", "pending")
    assert db.get_task(t).claude_bg_id is None


# ---------------- Step 3: bg watcher ----------------

def _make_bg_task(db, cwd="/repo", bg_id="ab12cd34"):
    s = db.get_or_create_session("u@im.wechat", cwd)
    t = db.create_task(None, s.id, "长活", kind="bg")
    db.claim_next_pending({s.id})
    db.set_bg_id(t, bg_id)
    return t


def _entry(bg_id="ab12cd34", state="working", started_ms=None, **extra):
    e = {"id": bg_id, "state": state, "sessionId": "S-UUID-1",
         "startedAt": NOW_MS() if started_ms is None else started_ms,
         "cwd": "/repo", "kind": "main"}
    e.update(extra)
    return e


def make_watch_pool(db, **cfg_over):
    cfg = SimpleNamespace(worker={"bg_poll_s": 10, "bg_blocked_timeout_s": 1800},
                          throttle={"page_char_limit": 2000})
    for k, v in cfg_over.items():
        setattr(cfg, k, v)
    return WorkerPool(db, config=cfg, runner=NullRunner(), poll_interval_s=30)


async def test_watcher_working_then_completed_with_entry_output(db):
    t = _make_bg_task(db)
    pool = make_watch_pool(db)
    resumed = []
    pool._resume_summary = lambda cwd, sid: resumed.append((cwd, sid)) or "fallback"
    agents = [_entry(state="working")]
    pool._agents_json = lambda: agents

    await pool._bg_watch_round()
    assert db.get_task(t).state == "running"      # working：不动

    agents[0] = _entry(state="completed", result="全部搞定")
    await pool._bg_watch_round()
    assert db.get_task(t).state == "done"
    assert any("全部搞定" in x for x in _texts(db))
    assert resumed == []                          # 条目自带输出 → 不走 resume 兜底


async def test_watcher_completed_falls_back_to_resume(db):
    t = _make_bg_task(db, cwd="/repo")
    pool = make_watch_pool(db)
    calls = []
    pool._resume_summary = lambda cwd, sid: calls.append((cwd, sid)) or f"总结：{sid}"
    pool._agents_json = lambda: [_entry(state="completed")]   # 无输出字段

    await pool._bg_watch_round()
    assert db.get_task(t).state == "done"
    assert calls == [("/repo", "S-UUID-1")]       # cwd 同会话、sessionId 取条目
    assert any("总结：S-UUID-1" in x for x in _texts(db))


async def test_watcher_result_paginated(db):
    t = _make_bg_task(db)
    pool = make_watch_pool(db)                    # page_char_limit=2000
    big = "x" * 4500
    pool._resume_summary = lambda cwd, sid: big
    pool._agents_json = lambda: [_entry(state="completed")]

    await pool._bg_watch_round()
    pages = [x for x in _texts(db) if "(第 " in x]
    assert len(pages) == 3                        # 4500 / 2000
    assert db.get_task(t).state == "done"


async def test_watcher_blocked_timeout_fails(db):
    t = _make_bg_task(db)
    pool = make_watch_pool(db)                    # bg_blocked_timeout_s=1800
    old = NOW_MS() - 31 * 60 * 1000
    pool._agents_json = lambda: [_entry(state="blocked", started_ms=old)]

    await pool._bg_watch_round()
    assert db.get_task(t).state in ("failed", "pending")   # finish failed（可重试语义）
    assert any("阻塞" in x for x in _texts(db))


async def test_watcher_blocked_fresh_keeps_running(db):
    t = _make_bg_task(db)
    pool = make_watch_pool(db)
    pool._agents_json = lambda: [_entry(state="blocked")]   # 刚开始 blocked
    await pool._bg_watch_round()
    assert db.get_task(t).state == "running"


async def test_watcher_missing_entry_grace_then_cancel(db):
    t = _make_bg_task(db)
    pool = make_watch_pool(db)
    pool._agents_json = lambda: []                # 条目不在列表

    await pool._bg_watch_round()                  # bg_id 刚写入 → 宽限期内不动
    assert db.get_task(t).state == "running"

    db._conn.execute("UPDATE tasks SET updated_at=? WHERE id=?",
                     (int(time.time()) - 120, t))
    db._conn.commit()
    await pool._bg_watch_round()
    assert db.get_task(t).state == "canceled"
    assert any("取消" in x for x in _texts(db))


async def test_watcher_agents_error_skips_round(db):
    t = _make_bg_task(db)

    def boom():
        raise RuntimeError("claude agents 崩了")

    pool = make_watch_pool(db)
    pool._agents_json = boom
    await pool._bg_watch_round()                  # 异常吞掉，本轮跳过
    assert db.get_task(t).state == "running"
    kinds = [r["kind"] for r in db._conn.execute(
        "SELECT kind FROM audit_log").fetchall()]
    assert "bg_agents_error" in kinds


async def test_watcher_costs_audited_when_present(db):
    t = _make_bg_task(db)
    pool = make_watch_pool(db)
    pool._resume_summary = lambda cwd, sid: "ok"
    pool._agents_json = lambda: [_entry(state="completed", costUsd=0.42)]
    await pool._bg_watch_round()
    assert db.get_task(t).state == "done"
    assert db.today_cost_usd() == pytest.approx(0.42)


async def test_watcher_spawned_by_run_forever(db):
    """run_forever 常驻拉起 _bg_watcher（接线回归：不只靠手动调 round）。"""
    t = _make_bg_task(db)
    cfg = SimpleNamespace(worker={"bg_poll_s": 0.02, "bg_blocked_timeout_s": 1800},
                          throttle={"page_char_limit": 2000})
    pool = WorkerPool(db, config=cfg, runner=NullRunner(), poll_interval_s=30)
    pool._agents_json = lambda: [_entry(state="completed", result="后台完成")]

    loop_task = asyncio.create_task(pool.run_forever())
    for _ in range(200):                          # 最长 10s
        if db.get_task(t).state == "done":
            break
        await asyncio.sleep(0.05)
    loop_task.cancel()
    await asyncio.gather(loop_task, return_exceptions=True)
    assert db.get_task(t).state == "done"
    assert any("后台完成" in x for x in _texts(db))


# ---------------- Step 4: /cancel 对 bg ----------------

async def test_cancel_bg_stops_and_cancels(db):
    t = _make_bg_task(db)
    pool = make_watch_pool(db)
    stopped = []

    def fake_stop(bg_id):
        stopped.append(bg_id)
        return f"stopped {bg_id}"

    pool._stop_bg = fake_stop
    reply = await pool.cancel(t)
    assert stopped == ["ab12cd34"]                # claude stop <bg_id> 已发
    assert db.get_task(t).state == "canceled"
    assert "取消" in reply
    assert any("取消" in x for x in _texts(db))   # runner 已返回 → 回执由 cancel 推


async def test_cancel_bg_without_bg_id_falls_back(db):
    """running 但 bg_id 尚未写入（启动竞态）：走原进程句柄路径的提示。"""
    s = db.get_or_create_session("u@im.wechat", "/repo")
    t = db.create_task(None, s.id, "长活", kind="bg")
    db.claim_next_pending({s.id})                 # running、无 bg_id
    pool = make_watch_pool(db)
    reply = await pool.cancel(t)
    assert "稍后再试" in reply                     # 无句柄可 kill 的既有语义


# ---------------- /tasks 显示 ----------------

async def test_tasks_shows_bg_marker(db):
    s = db.get_or_create_session("u@im.wechat", "/repo")
    db.create_task(None, s.id, "跑个大活", kind="bg")

    class SnapPool:
        def snapshot(self):
            return db.active_tasks()

        def running_session_ids(self):
            return set()

    reply = await execute_bridge(db, SnapPool(), _route("tasks"), "u@im.wechat",
                                 FakeBridgeCfg())
    assert "[bg]" in reply and "跑个大活" in reply


# ---------------- 崩溃恢复：bg 任务不重跑 ----------------

def test_reset_running_keeps_bg_with_id(db):
    s = db.get_or_create_session("u@im.wechat", "/repo")
    chat_t = db.create_task(None, s.id, "普通活")
    db.claim_next_pending({s.id})
    bg_t = _make_bg_task(db)
    bg_no_id = db.create_task(None, s.id, "启动一半的bg", kind="bg")
    db.claim_next_pending({s.id})

    n = db.reset_running_tasks()
    assert n == 2                                 # 普通活 + 无 bg_id 的 bg 重跑
    assert db.get_task(chat_t).state == "pending"
    assert db.get_task(bg_t).state == "running"   # 已在后台跑 → watcher 接管，不重跑
    assert db.get_task(bg_no_id).state == "pending"
