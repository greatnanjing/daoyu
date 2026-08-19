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
        self.worker = {"isolate_claude_config": True}
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
    log = json.loads(cfg_bg.args_log.read_text(encoding="utf-8"))
    argv = log["argv"]
    assert "--bg" in argv and argv[-1] == "跑个大活"
    # flag 集：--bare + 预算 + 权限档 + --settings（I3：硬 deny 清单与 -p 同样生效）
    assert "--bare" in argv and "--max-turns" in argv and "--max-budget-usd" in argv
    assert "--permission-mode" in argv
    assert argv[argv.index("--settings") + 1] == \
        str(cfg_bg.repo_root / "claude" / "settings.json")
    assert "--disallowedTools" not in argv        # 非 bypass 档不加
    # C3：bg 子进程在 isolate_claude_config 开关开启时同样注入（默认关，见 runner）
    assert log["claude_config_dir"] == str(cfg_bg.repo_root / "data" / "claude-home")
    assert (cfg_bg.repo_root / "data" / "claude-home").is_dir()


async def test_runner_bg_bypass_adds_disallowed_tools(db, cfg_bg):
    """I3：bypass 档 bg 带 --disallowedTools 工具级兜底（与 -p 同源常量）。"""
    s = db.get_or_create_session("u@im.wechat", str(cfg_bg.repo_root))
    db.set_policy(s.id, "bypass")
    s = db.get_session(s.id)
    t = db.create_task(None, s.id, "跑个大活", kind="bg")
    db.claim_next_pending({s.id})
    runner = TaskRunner(db, cfg_bg, process_registry={})
    await asyncio.wait_for(runner.run(db.get_task(t), s), timeout=10)
    argv = json.loads(cfg_bg.args_log.read_text(encoding="utf-8"))["argv"]
    tools = argv[argv.index("--disallowedTools") + 1]
    assert "Read(//etc/**)" in tools and "Bash(rm -rf ~)" in tools


async def test_runner_bg_prompt_leading_dash_gets_space(db, cfg_bg):
    """M2：bg prompt 走 argv，以 "-" 开头会被 CLI 解析成 flag → 前置空格防误读。"""
    s = db.get_or_create_session("u@im.wechat", str(cfg_bg.repo_root))
    t = db.create_task(None, s.id, "-flag-like-prompt", kind="bg")
    db.claim_next_pending({s.id})
    runner = TaskRunner(db, cfg_bg, process_registry={})
    await asyncio.wait_for(runner.run(db.get_task(t), s), timeout=10)
    assert db.get_task(t).claude_bg_id == "ab12cd34"   # 正常启动未被当 flag
    argv = json.loads(cfg_bg.args_log.read_text(encoding="utf-8"))["argv"]
    assert argv[-1] == " -flag-like-prompt"
    assert argv[argv.index("--bg") + 1] == " -flag-like-prompt"


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


async def test_runner_bg_strict_receipt_notes_no_approval(db, cfg_bg):
    """M5：strict 档下 /bg 无审批通道，回执必须明示（不静默降档）。"""
    s = db.get_or_create_session("u@im.wechat", str(cfg_bg.repo_root))
    db.set_policy(s.id, "strict")
    s = db.get_session(s.id)                      # 重取（set_policy 不回写旧对象）
    t = db.create_task(None, s.id, "跑个大活", kind="bg")
    db.claim_next_pending({s.id})
    runner = TaskRunner(db, cfg_bg, process_registry={})
    await asyncio.wait_for(runner.run(db.get_task(t), s), timeout=10)
    assert db.get_task(t).state == "running"
    assert any("后台" in x and "不走微信审批" in x for x in _texts(db))  # 回执含如实提示


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


async def test_watcher_done_state_realistic_entry(db):
    """真机采样回归锁（2026-08-19，生产服务器 CLI 2.1.233，task #10 实证）：
    完结态 state 值是 "done"（非 M2 假设的 "completed"——watcher 每轮空转、
    任务永卡 running），条目十字段无任何输出/cost 字段 → 必走 resume 总结。"""
    t = _make_bg_task(db, cwd="/repo")
    pool = make_watch_pool(db)
    calls = []
    pool._resume_summary = lambda cwd, sid: calls.append(sid) or "数完：3 个文件"
    pool._agents_json = lambda: [_entry(
        state="done", pid=1054635, cwd="/repo", kind="background",
        started_ms=1787103088037, name="directory file count", status="idle")]

    await pool._bg_watch_round()
    assert db.get_task(t).state == "done"
    assert calls == ["S-UUID-1"]                  # 无输出字段 → resume 是常态路径
    assert any("数完：3 个文件" in x for x in _texts(db))


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


def _fork_fail(pool):
    """blocked 旧路径（取结果持续失败→超时兜底）测试用：_resume_summary 恒失败
    （置 error detail 返回 ""），watcher 不得完结、走计时/超时。"""
    def boom(cwd, sid):
        pool._resume_error_detail = "resume cli down"
        return ""
    pool._resume_summary = boom


async def test_watcher_blocked_takes_result_and_completes(db):
    """真机实证（2026-08-19，2.1.233，task #12）：blocked = 会话等用户输入
    （bg 无输入通道即永久挂起）→ 首次观察即取结果完结 + stop 条目（防孤儿）。
    _resume_summary 恒 fork（bg 会话被 daemon 持有，直接 resume 被拒）。"""
    t = _make_bg_task(db)
    pool = make_watch_pool(db)
    stopped = []
    pool._stop_bg = lambda bg_id: stopped.append(bg_id) or ""
    calls = []
    pool._resume_summary = lambda cwd, sid: \
        calls.append((cwd, sid)) or "目录不是 git 仓库"
    pool._agents_json = lambda: [_entry(state="blocked")]

    await pool._bg_watch_round()
    assert db.get_task(t).state == "done"
    assert calls == [("/repo", "S-UUID-1")]        # 取 blocked 会话结果
    assert stopped == ["ab12cd34"]                 # 结果已取到 → stop 条目
    assert any("补充信息" in x and "目录不是 git 仓库" in x for x in _texts(db))
    assert db.get_state(f"bg_blocked_since:{t}") is None   # 计时已清


async def test_watcher_blocked_timeout_fails(db):
    """blocked fork 持续失败 → 超时（以"首次观察到 blocked"计时，I2）→ 失败
    可重试；且先 claude stop 旧条目再 finish（M2，不留 daemon 孤儿）。"""
    t = _make_bg_task(db)
    pool = make_watch_pool(db)                    # bg_blocked_timeout_s=1800
    stopped = []
    pool._stop_bg = lambda bg_id: stopped.append(bg_id) or ""
    _fork_fail(pool)
    pool._agents_json = lambda: [_entry(state="blocked")]
    # 预置首次观察到 blocked 在 31 分钟前（此后持续 blocked 未恢复）
    db.set_state(f"bg_blocked_since:{t}", str(time.time() - 31 * 60))

    await pool._bg_watch_round()
    assert db.get_task(t).state in ("failed", "pending")   # finish failed（可重试语义）
    assert stopped == ["ab12cd34"]
    assert any("阻塞" in x for x in _texts(db))


async def test_watcher_blocked_fresh_keeps_running(db):
    t = _make_bg_task(db)
    pool = make_watch_pool(db)
    _fork_fail(pool)
    pool._agents_json = lambda: [_entry(state="blocked")]   # 刚开始 blocked
    await pool._bg_watch_round()
    assert db.get_task(t).state == "running"


async def test_watcher_failed_entry_fails_task(db):
    """M3 真机采样（2026-08-19，CLI 2.1.226）：daemon 条目 state="failed"——
    M2 版 watcher 无此分支，每轮空转、任务永卡 running。现按失败推进（可重试
    语义）；条目已是 daemon 终态，无需 stop。"""
    t = _make_bg_task(db)
    pool = make_watch_pool(db)
    stopped = []
    pool._stop_bg = lambda bg_id: stopped.append(bg_id) or ""
    pool._agents_json = lambda: [_entry(state="failed")]

    await pool._bg_watch_round()
    assert db.get_task(t).state in ("failed", "pending")   # finish failed（可重试语义）
    assert stopped == []                                   # 不 stop（非孤儿）
    assert any("执行失败" in x for x in _texts(db))


async def test_watcher_blocked_timer_counts_from_first_sight(db):
    """I2：长任务（startedAt 40 分钟前）刚进 blocked 且 fork 失败 → 只记时刻不杀
    （计时从首次观察到 blocked 的本地时刻起算），持续失败满 timeout 下一轮才杀。"""
    t = _make_bg_task(db)
    pool = make_watch_pool(db)                    # bg_blocked_timeout_s=1800
    stopped = []
    pool._stop_bg = lambda bg_id: stopped.append(bg_id) or ""
    _fork_fail(pool)
    old = NOW_MS() - 40 * 60 * 1000               # 任务 40 分钟前启动（主用例形态）
    states = [_entry(state="blocked", started_ms=old)]
    pool._agents_json = lambda: states

    await pool._bg_watch_round()                  # 刚进 blocked：只记时刻，不杀
    assert db.get_task(t).state == "running"
    assert db.get_state(f"bg_blocked_since:{t}") is not None
    assert stopped == []

    # 模拟又过了 timeout：把首次观察时刻拨回 31 分钟前 → 下一轮才杀
    db.set_state(f"bg_blocked_since:{t}", str(time.time() - 31 * 60))
    await pool._bg_watch_round()
    assert db.get_task(t).state in ("failed", "pending")
    assert stopped == ["ab12cd34"]
    assert any("阻塞" in x for x in _texts(db))


async def test_watcher_blocked_timer_resets_on_recovery(db):
    """I2：blocked→working 恢复清计时；再进 blocked 从零重计，不累计旧值。"""
    t = _make_bg_task(db)
    pool = make_watch_pool(db)
    _fork_fail(pool)
    states = [_entry(state="blocked")]
    pool._agents_json = lambda: states

    await pool._bg_watch_round()                  # 首次观察 → 记时刻
    assert db.get_state(f"bg_blocked_since:{t}") is not None

    db.set_state(f"bg_blocked_since:{t}", str(time.time() - 31 * 60))  # 计时已老化
    states[0] = _entry(state="working")
    await pool._bg_watch_round()                  # 恢复 working → 清计时，不杀
    assert db.get_state(f"bg_blocked_since:{t}") is None
    assert db.get_task(t).state == "running"

    states[0] = _entry(state="blocked")
    await pool._bg_watch_round()                  # 再进 blocked → 重新计时（非超时）
    assert db.get_state(f"bg_blocked_since:{t}") is not None
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


async def test_watcher_agents_none_skips_round(db):
    """C1：轮询失败返回 None（真实 _agents_json 失败的形态，不抛异常）≠ 空列表
    ——整轮跳过；哪怕任务早已过消失宽限期也不得误进消失判定集体误杀。"""
    t = _make_bg_task(db)
    pool = make_watch_pool(db)
    pool._agents_json = lambda: None
    db._conn.execute("UPDATE tasks SET updated_at=? WHERE id=?",
                     (int(time.time()) - 3600, t))   # 早已过 60s 宽限
    db._conn.commit()

    await pool._bg_watch_round()                  # 轮询失败 → 整轮跳过
    assert db.get_task(t).state == "running"      # 若误当空列表会变 canceled
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


async def test_cancel_bg_race_watcher_won_keeps_done(db):
    """M3：stop 的 await 窗口里 watcher 已把任务完结 → cancel 不翻写终态。"""
    t = _make_bg_task(db)
    pool = make_watch_pool(db)

    def slow_stop(bg_id):
        db.finish_task(t, "done")                 # 模拟窗口内 watcher 先完结
        return "stopped"

    pool._stop_bg = slow_stop
    reply = await pool.cancel(t)
    assert "已由后台监视完结" in reply
    assert db.get_task(t).state == "done"         # 不被翻成 canceled
    assert not any("取消" in x for x in _texts(db))


async def test_watcher_completed_race_cancel_won_keeps_canceled(db):
    """M3：resume 兜底的 await 窗口里 /cancel 先落 canceled → watcher 不推
    结果、不翻写终态（先落者胜）。"""
    t = _make_bg_task(db)

    def slow_resume(cwd, sid):
        db.finish_task(t, "canceled")             # 模拟窗口内 cancel 先落终态
        return "总结"

    pool = make_watch_pool(db)
    pool._resume_summary = slow_resume
    pool._agents_json = lambda: [_entry(state="completed")]   # 无输出字段 → 走兜底
    await pool._bg_watch_round()
    assert db.get_task(t).state == "canceled"     # 不被翻成 done
    assert not any("总结" in x for x in _texts(db))   # 取消后不推结果


async def test_cancel_bg_without_bg_id_falls_back(db):
    """running 但 bg_id 尚未写入（启动竞态）：走原进程句柄路径的提示。"""
    s = db.get_or_create_session("u@im.wechat", "/repo")
    t = db.create_task(None, s.id, "长活", kind="bg")
    db.claim_next_pending({s.id})                 # running、无 bg_id
    pool = make_watch_pool(db)
    reply = await pool.cancel(t)
    assert "稍后再试" in reply                     # 无句柄可 kill 的既有语义


async def test_cancel_bg_during_launch_kills_process(db, cfg_bg, monkeypatch):
    """M4：launch 阶段（bg_id 未落盘）进程已注册 → /cancel 走 kill 路径，
    任务落 canceled 且不被 _fail 翻成 pending 重试。"""
    monkeypatch.setenv("FAKE_BG_DELAY_MS", "3000")
    s = db.get_or_create_session("u@im.wechat", str(cfg_bg.repo_root))
    t = db.create_task(None, s.id, "长活", kind="bg")
    db.claim_next_pending({s.id})
    runner = TaskRunner(db, cfg_bg, process_registry={})
    pool = WorkerPool(db, config=cfg_bg, runner=runner, poll_interval_s=30)

    run_t = asyncio.create_task(runner.run(db.get_task(t), s))
    for _ in range(200):                          # 等 launch 进程注册（≤4s）
        if t in runner.procs:
            break
        await asyncio.sleep(0.02)
    assert t in runner.procs                      # 已注册 → cancel 可拿到句柄

    reply = await pool.cancel(t)
    assert "取消" in reply and "稍后再试" not in reply
    await asyncio.wait_for(run_t, timeout=10)     # kill → communicate 返回
    assert db.get_task(t).state == "canceled"     # 不走 failed→pending 重试
    assert t not in runner.procs                  # finally 已注销


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


async def test_runner_bg_excludes_mcp_config(db, cfg_bg):
    """M3 真机实证（2026-08-19，生产服务器）：--bg 下 daemon 异步拉起 worker，临时
    mcp config 在客户端返回即删 → daemon 读不到必崩（"exit 1 before init" 100%
    复现）→ bg 摘除 --mcp-config（回 M2 flag 集）。回归锁：argv 不带
    --mcp-config/--strict-mcp-config，也不留 daoyu-mcp- 临时文件。"""
    import tempfile
    s = db.get_or_create_session("u@im.wechat", str(cfg_bg.repo_root))
    t = db.create_task(None, s.id, "跑个大活", kind="bg")
    db.claim_next_pending({s.id})
    before = set(Path(tempfile.gettempdir()).glob("daoyu-mcp-*.json"))
    runner = TaskRunner(db, cfg_bg, process_registry={})
    await asyncio.wait_for(runner.run(db.get_task(t), s), timeout=10)
    argv = json.loads(cfg_bg.args_log.read_text(encoding="utf-8"))["argv"]
    assert "--mcp-config" not in argv
    assert "--strict-mcp-config" not in argv
    assert argv[-1] == "跑个大活"                # --bg 仍是最后一个 flag
    after = set(Path(tempfile.gettempdir()).glob("daoyu-mcp-*.json"))
    assert after == before                      # bg 不写临时 mcp config


def test_resume_summary_excludes_mcp_config(db, tmp_path, monkeypatch):
    """审查修正回归锁：_resume_summary 原直传静态 claude/mcp.json 作
    --mcp-config——静态清单已平台无关化（command 裸写 npx/uvx、Windows 包装
    下沉到 runner 合并层），而此路径不经合并层展开 → Windows 裸 npx 直启
    FileNotFoundError。且取结果是 max-turns 2 只读总结（prompt 固定、无需
    MCP 工具）→ 与启动分支同口径摘除（先例见上个测试）。同步方法直调，
    fake_claude 秒回不触 300s 超时。"""
    args_log = tmp_path / "resume_args.log"
    monkeypatch.setenv("FAKE_CLAUDE_ARGS_LOG", str(args_log))
    monkeypatch.setenv("FAKE_CLAUDE_SCRIPT", str(FIXTURES / "review_stream.jsonl"))
    monkeypatch.setenv("FAKE_CLAUDE_STDIN_LOG", str(tmp_path / "stdin.log"))
    cfg = SimpleNamespace(
        claude_bin=[sys.executable, str(FIXTURES / "fake_claude.py")],
        secrets={"ANTHROPIC_API_KEY": "sk-test"},
        repo_root=tmp_path,
        budget=Budget(max_turns=10, max_usd=1.0),
        worker={})
    pool = WorkerPool(db, config=cfg, runner=NullRunner(), poll_interval_s=30)

    result = pool._resume_summary(str(tmp_path), "S-UUID-1")   # 同步方法直调

    argv = json.loads(args_log.read_text(encoding="utf-8"))["argv"]
    assert "--mcp-config" not in argv
    assert "--strict-mcp-config" not in argv
    assert "--fork-session" in argv             # 恒 fork（daemon 持有时直 resume 被拒）
    assert result == "审查完成：3 个问题。"       # fake 回放含 result 行，解析正常
