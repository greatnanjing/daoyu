"""任务调度池：按 session 分组串行、跨 session 并行、并发上限、取消。
同一 Claude 会话（同 UUID）任务必须串行（--resume 并发会冲突）——TRD 硬约束。"""
import asyncio
from typing import TYPE_CHECKING

import common.models as M

if TYPE_CHECKING:
    from worker.runner import TrackedProcess


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
        self._running_sessions: set[int] = set()
        self._wake = asyncio.Event()

    async def run_forever(self) -> None:
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
            asyncio.create_task(self._run_one(task, session))
        return started

    async def _run_one(self, task, session) -> None:
        try:
            await self._runner.run(task, session)
        except Exception as e:  # 保姆代码不允许崩掉整个池
            self._db.finish_task(task.id, "failed")
            self._db.audit("runner_crash", f"task={task.id} err={e!r}")
        finally:
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
