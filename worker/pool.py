"""任务调度池：按 session 分组串行、跨 session 并行、并发上限、取消。
同一 Claude 会话（同 UUID）任务必须串行（--resume 并发会冲突）——TRD 硬约束。
同 session 串行依赖本进程内存状态，单实例假设——勿起第二个池实例。

bg 长任务另有两条通道（M2）：启动走 runner 的 --bg 分支（run() 立即返回），
完结走本模块 _bg_watcher——每 bg_poll_s 轮询 claude agents --json --all，
按条目状态推进：done/completed → 取结果完结 / blocked（=等待用户输入，
2.1.233 实测）→ fork 副本取结果完结 / failed → 失败 / 消失 → 取消。
轮询失败（None 哨兵）≠ 空列表：失败整轮跳过，绝不进消失判定（防集体误杀）。
"""
import asyncio
import json
import os
import subprocess
import time
from typing import TYPE_CHECKING

import common.models as M
from common.models import Budget
from common.text import split_text
from worker.cli_builder import build_argv, claude_config_dir
from worker.stream import StreamParser

if TYPE_CHECKING:
    from worker.runner import TrackedProcess

# bg_id 刚落盘时 agents 列表可能尚未见到该条目（守护进程注册竞态），宽限期内
# 条目缺失不算"被外部停止"（updated_at 由 set_bg_id 刷新，作 bg_id 写入时间用）
_BG_MISSING_GRACE_S = 60.0
# completed/done 条目自带输出/cost 字段的键名：扫常见候选，取首个命中。
# 真机采样（2026-08-19，生产服务器 CLI 2.1.233）done 条目只有
# pid/id/cwd/kind/startedAt(毫秒)/sessionId/name/status/state 十个字段，
# **无任何输出/cost 字段** → resume 兜底是常态路径而非兜底；候选扫描留给
# 未来版本可能自带输出的形态。
_BG_RESULT_KEYS = ("result", "output", "lastMessage", "text", "summary")
_BG_COST_KEYS = ("costUsd", "cost_usd", "total_cost_usd", "costUSD")
# 结果取回 prompt（--max-turns 2 限定回合）。TRD 原口径"≤500 字总结"真机
# 实证不可用（task #14）：Claude 把文件清单压缩成统计摘要——用户要的是清单
# 本身。改为要求逐项列出 + 放宽到 1500 字（微信分页兜底，2000 字/页）。
_BG_SUMMARY_PROMPT = ("请给出你的最终结果：若属清单必须逐项列出（紧凑格式，"
                      "不要概括或省略）；总长控制在1500字内，超出时优先保留"
                      "结论与关键条目")


def _extract_bg_result(entry: dict) -> str | None:
    for k in _BG_RESULT_KEYS:
        v = entry.get(k)
        if isinstance(v, str) and v.strip():
            return v
    return None


def _extract_bg_cost(entry: dict) -> float | None:
    for k in _BG_COST_KEYS:
        v = entry.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
    return None


class WorkerPool:
    def __init__(self, db, config, runner=None, concurrency: int = 3,
                 poll_interval_s: float = 0.5):
        self._db = db
        self._cfg = config
        # task_id → TrackedProcess 注册表。自建 runner 时与其共享同一 dict，
        # cancel 才能拿到句柄 kill；注入 runner 时采用其公开 procs（若有）。
        self._procs: "dict[int, TrackedProcess]" = {}
        if runner is None:
            from worker.runner import TaskRunner
            runner = TaskRunner(db, config, process_registry=self._procs)
        elif isinstance(getattr(runner, "procs", None), dict):
            self._procs = runner.procs
        self._runner = runner
        self._concurrency = concurrency
        self._interval = poll_interval_s
        worker_cfg = getattr(config, "worker", None) or {}
        self._bg_poll_s = float(worker_cfg.get("bg_poll_s", 10))
        self._bg_blocked_timeout_s = float(worker_cfg.get("bg_blocked_timeout_s", 1800))
        self._running_sessions: set[int] = set()
        # agents/resume 子进程失败的细节暂存：to_thread 工作线程内赋值、await
        # 返回后在事件循环线程侧读取审计——线程内不碰共享 SQLite 连接（db.py
        # 约定所有访问都在事件循环线程；M1）。
        self._agents_error_detail = ""
        self._resume_error_detail = ""
        # 裸 create_task 的事件循环只持弱引用：不存强引用则任务可能在执行中被
        # GC 回收 → finally 不执行 → _running_sessions 永不 discard → 该 session
        # 永久卡死（7×24 常驻不可接受）。
        self._live: set[asyncio.Task] = set()
        self._wake = asyncio.Event()

    async def run_forever(self) -> None:
        # bg 监视协程随调度循环常驻（存强引用防 GC，同 _live 约定）
        watch = asyncio.create_task(self._bg_watcher(), name="bg-watcher")
        self._live.add(watch)
        watch.add_done_callback(self._live.discard)
        try:
            while True:
                try:
                    made = self._claim_one_round()
                except Exception as e:   # claim 循环自身不许拖垮调度（_run_one 另有兜底）
                    made = False
                    try:
                        self._db.audit("pool_claim_error", repr(e))
                    except Exception:
                        pass
                if not made:
                    try:
                        await asyncio.wait_for(self._wake.wait(), timeout=self._interval)
                    except asyncio.TimeoutError:
                        pass
                    self._wake.clear()
        finally:
            watch.cancel()
            try:
                await watch
            except (asyncio.CancelledError, Exception):
                pass

    async def submit_check(self) -> None:
        """有新任务入队时唤醒一次扫描（不等下一个轮询周期）。"""
        self._wake.set()

    def _claim_one_round(self) -> bool:
        running = len(self._running_sessions)
        free = self._concurrency - running
        started = False
        for sid in self._db.pending_sessions():
            if free <= 0:
                break
            if sid in self._running_sessions:
                continue
            task = self._db.claim_next_pending({sid})
            if task is None:
                continue
            session = self._db.get_session(sid)
            self._running_sessions.add(sid)
            started = True
            free -= 1
            t = asyncio.create_task(self._run_one(task, session))
            self._live.add(t)                # 持强引用防 GC（见 __init__ 注释）
            t.add_done_callback(self._live.discard)
        return started

    async def _run_one(self, task, session) -> None:
        try:
            await self._runner.run(task, session)
        except Exception as e:  # 保姆代码不允许崩掉整个池
            self._db.finish_task(task.id, "failed")
            self._db.audit("runner_crash", f"task={task.id} err={e!r}")
        finally:
            # bg 的 run() 启动后即返回：session 槽位随 finally 照常释放——bg 本体在
            # 后台守护进程里且用独立 claude 会话，与本会话 -p 任务无 --resume
            # 串行冲突；完结进度由 _bg_watcher 按 tasks 表追踪，不占调度槽。
            self._running_sessions.discard(task.session_id)
            self._wake.set()

    def running_session_ids(self) -> set[int]:
        return set(self._running_sessions)

    def snapshot(self) -> list[M.Task]:
        return self._db.active_tasks()

    async def cancel(self, task_id: int) -> str:
        task = self._db.get_task(task_id)
        if task is None:
            return "没有这个任务。"
        if task.state == "pending":
            self._db.cancel_task(task_id)
            return f"已取消任务 #{task_id}。"
        if task.state == "running":
            if task.kind == "bg" and task.claude_bg_id:
                # bg：本体在后台守护进程里，本地无句柄 → claude stop <bg_id>。
                # 无论 stop 结果如何本地都落终态（stop 对已完成条目本就无效）。
                note = ""
                try:
                    await asyncio.to_thread(self._stop_bg, task.claude_bg_id)
                except Exception as e:
                    self._db.audit("bg_stop_error", f"task={task_id} err={e!r}")
                    note = "（停止指令发送失败，请稍后用 /tasks 确认）"
                # M3：stop 的 await 窗口（≤30s）里 watcher 可能已落 done/failed
                # → 先落者胜，cancel 不翻写也不重复推回执
                cur = self._db.get_task(task_id)
                if cur is not None and cur.state != "running":
                    return f"任务 #{task_id} 状态为 {cur.state}，已由后台监视完结。"
                self._db.finish_task(task_id, "canceled")
                session = self._db.get_session(task.session_id)
                if session:
                    self._db.enqueue(task_id, session.wechat_user,
                                     f"已取消后台任务 #{task_id}。{note}")
                return f"已取消后台任务 #{task_id}。{note}"
            proc = self._procs.get(task_id)
            if proc is None:
                return f"任务 #{task_id} 正在运行但进程句柄未注册，稍后再试。"
            self._db.finish_task(task_id, "canceled")
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            return f"已取消任务 #{task_id}，进程已终止。"
        return f"任务 #{task_id} 状态为 {task.state}，无需取消。"

    # ---- bg watcher（M2 /bg 长任务）----

    async def _bg_watcher(self) -> None:
        """常驻轮询：每 bg_poll_s 一轮，异常只审计不打断（下一轮重试）。"""
        while True:
            try:
                await self._bg_watch_round()
            except Exception as e:
                try:
                    self._db.audit("bg_watcher_error", repr(e))
                except Exception:
                    pass
            await asyncio.sleep(self._bg_poll_s)

    async def _bg_watch_round(self) -> None:
        tasks = self._db.running_bg_tasks()
        if not tasks:
            return   # 无 bg 任务时不碰 claude CLI（也兼容 config 缺省的测试形态）
        try:
            agents = await asyncio.to_thread(self._agents_json)
        except Exception as e:   # 注入替身等意外异常 → 同失败语义，整轮跳过
            self._db.audit("bg_agents_error", repr(e))
            return
        if agents is None:
            # 轮询失败 ≠ 空列表（C1）：空列表 = "真实无后台任务"，None = "本轮
            # 不知道"。失败必须整轮跳过——若当成空列表走消失判定，一次瞬时
            # 故障（节点忙/CLI 升级漂移/超时）会把活着的 bg 任务同轮集体误杀。
            self._db.audit("bg_agents_error",
                           self._agents_error_detail or "agents --json 调用失败（细节未知）")
            return
        now = time.time()
        for t in tasks:
            try:
                await self._bg_advance(t, agents, now)
            except Exception as e:
                self._db.audit("bg_task_error", f"task={t.id} err={e!r}")

    async def _bg_advance(self, t: M.Task, agents: list, now: float) -> None:
        entry = next((a for a in agents if str(a.get("id")) == t.claude_bg_id), None)
        session = self._db.get_session(t.session_id)
        to_user = session.wechat_user if session else ""
        cwd = session.cwd if session else os.getcwd()   # done/blocked 取结果都在会话 cwd
        blocked_key = f"bg_blocked_since:{t.id}"
        if entry is None:
            if now - t.updated_at >= _BG_MISSING_GRACE_S:
                # M3：agents 轮询的 await 窗口里 /cancel 可能已落终态 → 先落者胜
                if self._db.get_task(t.id).state != "running":
                    return
                self._db.delete_state(blocked_key)   # 清计时（终态路径，防残留）
                self._db.finish_task(t.id, "canceled")
                self._db.audit("bg_missing", f"task={t.id} bg_id={t.claude_bg_id}")
                self._db.enqueue(t.id, to_user,
                                 f"⚠️ 后台任务 #{t.id} 已不在后台运行列表"
                                 f"（可能被外部停止），标记为已取消。")
            return
        state = entry.get("state")
        if state != "blocked" and self._db.get_state(blocked_key) is not None:
            # working/completed 等非 blocked 态：清"首次观察 blocked"计时——
            # blocked→working 恢复后再 blocked 应从零重计，不累计旧值（I2）
            self._db.delete_state(blocked_key)
        # 真机采样（2026-08-19，CLI 2.1.233）：完结态 state 值是 "done"（M2 写码时
        # 假设的 "completed" 从未出现，watcher 每轮空转、任务永卡 running——task
        # #10 实证）；failed 条目值为 "failed"。两值都认，未来版本漂移再采样。
        if state in ("completed", "done"):
            result = _extract_bg_result(entry)
            if result is None:
                result = await asyncio.to_thread(
                    self._resume_summary, cwd, entry.get("sessionId") or "")
                if not result and self._resume_error_detail:
                    self._db.audit("bg_resume_error", self._resume_error_detail)
                    self._resume_error_detail = ""
            # M3：resume 的 await 窗口（≤300s）里 /cancel 可能已落 canceled →
            # 先落者胜：不再推结果、不翻写终态
            if self._db.get_task(t.id).state != "running":
                return
            cost = _extract_bg_cost(entry)
            if cost is not None:
                self._db.audit("cost", json.dumps({"task_id": t.id, "usd": cost}))
            header = f"✅ 后台任务 #{t.id} 完成：\n"
            for page in split_text(header + (result or "(结果摘要为空)"),
                                   self._page_limit()):
                self._db.enqueue(t.id, to_user, page)
            self._db.finish_task(t.id, "done")
            if session:
                self._db.touch_session(session.id)
        elif state == "failed":
            # M3 真机采样（2026-08-19，CLI 2.1.226）：条目 state="failed"——daemon
            # 侧失败（条目无 error detail 字段）。M2 版只覆盖 completed/blocked/
            # 消失，failed 每轮空转、任务永卡 running。按失败推进（attempts 未
            # 耗尽 → pending 重跑，耗尽 → dead 死信告警），无需 stop（条目已是
            # daemon 终态，不占资源）。未知其他状态值不处理（记观察）——CLI 版本
            # 漂移加新中间态时误判 failed 会重复烧预算，宁空转待采样。
            if self._db.get_task(t.id).state != "running":   # 先落者胜（M3）
                return
            self._db.delete_state(blocked_key)
            self._db.finish_task(t.id, "failed")
            self._db.audit("bg_failed", f"task={t.id} bg_id={t.claude_bg_id}")
            self._db.enqueue(t.id, to_user,
                             f"❌ 后台任务 #{t.id} 在后台执行失败（daemon 报 failed）。")
        elif state == "blocked":
            # 真机实证（2026-08-19，2.1.233，task #12）：blocked = 会话回合结束
            # 等待用户后续输入（Claude 结尾反问是常态）——bg 无输入通道，一旦
            # blocked 即永久挂起，任务其实已有产出。原 30min 超时对用户等于无
            # 回应。改为首次观察即取结果完结（_resume_summary 恒 fork，原会话
            # 被 daemon 持有直接 --resume 会被拒）；取到后 stop 条目防孤儿累积
            # （M2）。取结果失败（CLI 故障）不完结：走下方超时兜底，下一轮再试。
            result = await asyncio.to_thread(
                self._resume_summary, cwd, entry.get("sessionId") or "")
            err = self._resume_error_detail
            self._resume_error_detail = ""
            if result or not err:
                # 取到总结（或总结为空但 fork 本身成功）→ 完结
                # 两个 await 窗口（fork/stop）里 /cancel 可能已落终态 → 先落者胜
                if self._db.get_task(t.id).state != "running":
                    return
                try:
                    await asyncio.to_thread(self._stop_bg, t.claude_bg_id)
                except Exception as e:
                    self._db.audit("bg_stop_error", f"task={t.id} err={e!r}")
                if self._db.get_task(t.id).state != "running":
                    return
                self._db.delete_state(blocked_key)
                header = (f"⏸ 后台任务 #{t.id} 已完成当前部分、正等待你的补充"
                          f"信息（结果如下；要继续请发新指令）：\n")
                for page in split_text(header + (result or "(结果摘要为空)"),
                                       self._page_limit()):
                    self._db.enqueue(t.id, to_user, page)
                self._db.finish_task(t.id, "done")
                if session:
                    self._db.touch_session(session.id)
                return
            self._db.audit("bg_resume_error", err)   # fork 失败：下轮重试
            # ---- 超时兜底（fork 持续失败时的最终出路）----
            since = self._db.get_state(blocked_key)
            if since is None:
                # 首次观察到 blocked：以本地此刻计时（I2）。agents 条目只有
                # startedAt（任务启动时刻），长任务跑 30min+ 后一进 blocked 若按
                # startedAt 判超时会下一轮立即误杀 + 自动重发新 bg。
                self._db.set_state(blocked_key, str(now))
                return
            if now - float(since) >= self._bg_blocked_timeout_s:
                # fork 持续失败超时：先 stop 再 finish，条目还在 daemon 里，
                # 不 stop 会留孤儿，配合重试多次超时可累积多个 blocked 孤儿（M2）。
                try:
                    await asyncio.to_thread(self._stop_bg, t.claude_bg_id)
                except Exception as e:
                    self._db.audit("bg_stop_error", f"task={t.id} err={e!r}")
                # M3：stop 的 await 窗口里 /cancel 可能已落终态 → 先落者胜
                if self._db.get_task(t.id).state != "running":
                    return
                self._db.delete_state(blocked_key)   # 重试是全新 bg，计时须重置
                self._db.finish_task(t.id, "failed")
                self._db.audit("bg_blocked_timeout", f"task={t.id} bg_id={t.claude_bg_id}")
                self._db.enqueue(t.id, to_user,
                                 f"❌ 后台任务 #{t.id} 已阻塞超过 "
                                 f"{int(self._bg_blocked_timeout_s // 60)} 分钟，标记失败。")
        # working：仍在跑，下一轮再看

    def _page_limit(self) -> int:
        if self._cfg is None:
            return 2000
        return int((self._cfg.throttle or {}).get("page_char_limit", 2000))

    def _claude_prefix(self) -> list[str]:
        bin_ = self._cfg.claude_bin
        return list(bin_) if isinstance(bin_, list) else [bin_]

    def _claude_env(self) -> dict:
        env = os.environ.copy()
        if self._cfg is not None:
            env.update(self._cfg.secrets)
            # 与 runner 一致：机制化隔离宿主 ~/.claude（agents/stop/resume 子进程
            # 也是 claude CLI，同样受宿主配置穿透影响）。开关语义同 runner 的
            # isolate_claude_config（默认关，见 runner 注释）。
            if getattr(self._cfg, "worker", {}).get("isolate_claude_config", False):
                env["CLAUDE_CONFIG_DIR"] = claude_config_dir(self._cfg.repo_root)
        return env

    def _agents_json(self) -> "list[dict] | None":
        """同步跑 claude agents --json --all（watcher 经 to_thread 调）。
        返回 list = 成功；返回 None（哨兵）= 调用失败（spawn 失败/超时/rc≠0/
        输出非 JSON 列表）。失败 ≠ 空列表：调用方见 None 必须整轮跳过，绝不
        能进消失判定（C1）。失败细节写入 self._agents_error_detail（线程内
        赋值、await 返回后循环线程侧审计——此处不碰共享 SQLite 连接，M1）。"""
        try:
            cp = subprocess.run([*self._claude_prefix(), "agents", "--json", "--all"],
                                capture_output=True, timeout=30, env=self._claude_env())
            if cp.returncode != 0:
                tail = cp.stderr.decode("utf-8", "replace").strip()[-200:]
                self._agents_error_detail = f"rc={cp.returncode} stderr={tail}"
                return None
            data = json.loads(cp.stdout.decode("utf-8", "replace"))
            if not isinstance(data, list):
                self._agents_error_detail = f"输出非 JSON 列表: {type(data).__name__}"
                return None
            return data
        except Exception as e:
            self._agents_error_detail = repr(e)
            return None

    def _stop_bg(self, bg_id: str) -> str:
        """同步跑 claude stop <id>（cancel 经 to_thread 调）。返回输出文本（诊断用）。"""
        cp = subprocess.run([*self._claude_prefix(), "stop", bg_id],
                            capture_output=True, timeout=30, env=self._claude_env())
        return (cp.stdout + b"\n" + cp.stderr).decode("utf-8", "replace").strip()

    def _resume_summary(self, cwd: str, claude_uuid: str) -> str:
        """结果获取的常态路径：真机 done 条目无输出字段（2.1.233 采样，见
        _BG_RESULT_KEYS 注释）→ 回原 Claude 会话要一份 ≤500 字总结。
        恒 --fork-session：bg 会话（含 done/blocked 条目）被 daemon 持有时
        直接 --resume 被拒（实测 2.1.233，错误只在输出里、rc 还是 0 → 静默
        空结果，task #13 实证）；fork 总能分叉副本，daemon 已退出时同样可用。
        同步 subprocess（watcher 经 to_thread 调）；异常/空 → ""。无 result
        行（被拒/超时/输出形态漂移）置 _resume_error_detail 供调用方审计
        （线程内不碰 db，M1）。policy 固定 auto：只读回总结，不带审批 MCP。"""
        try:
            argv = build_argv(
                session_uuid=claude_uuid, resume=True, policy="auto",
                # --max-turns 2（TRD 兜底规范）：总结一次即答，防兜底自己跑飞
                budget=Budget(max_turns=2, max_usd=self._cfg.budget.max_usd),
                mcp_config=self._cfg.repo_root / "claude" / "mcp.json",
                settings=self._cfg.repo_root / "claude" / "settings.json",
                fork_session=True)
            cp = subprocess.run([*self._claude_prefix(), *argv],
                                input=_BG_SUMMARY_PROMPT.encode("utf-8"),
                                capture_output=True, timeout=300,
                                cwd=cwd, env=self._claude_env())
            parser = StreamParser()
            result = ""
            for line in cp.stdout.decode("utf-8", "replace").splitlines():
                ev = parser.feed_line(line)
                if ev is not None and ev.type == "result":
                    result = ev.text
            if not result:
                self._resume_error_detail = (
                    f"resume 无 result 行（rc={cp.returncode}）: "
                    + (cp.stdout + b"\n" + cp.stderr).decode("utf-8", "replace")[-200:])
            return result
        except Exception as e:
            self._resume_error_detail = repr(e)
            return ""
