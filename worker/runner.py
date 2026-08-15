"""单任务执行：组装 argv → 子进程 → 解析流 → 节流推进度 → 最终回复入 outbox。"""
import asyncio
import json
import os

from worker.cli_builder import build_argv
from worker.stream import StreamParser, Throttle

# 进度推送里工具命令 JSON 的截断长度（够认出在跑什么即可）
_PROGRESS_DETAIL_LIMIT = 60


def split_text(text: str, limit: int) -> list[str]:
    """超长文本分页（M1 按字符数切，UTF-16 代理对安全——不切字节）。"""
    if len(text) <= limit:
        return [text]
    pages = [text[i:i + limit] for i in range(0, len(text), limit)]
    return [f"(第 {i}/{len(pages)} 页)\n{p}" for i, p in enumerate(pages, 1)]


class TrackedProcess:
    """process_registry 的条目：记录是否被外部 kill 过。

    Windows 上 TerminateProcess 的退出码（1）与子进程自身 exit(1) 无法区分，
    只能在 kill() 入口处立标记，供 runner 区分"取消"与"失败"两条路径。
    """

    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        self._proc = proc
        self.killed = False

    def kill(self) -> None:
        self.killed = True
        self._proc.kill()


class TaskRunner:
    def __init__(self, db, config, process_registry: dict[int, TrackedProcess]):
        self._db = db
        self._cfg = config
        self._procs = process_registry

    async def run(self, task, session) -> None:
        # 该 Claude 会话是否已被首次调用过（--session-id 建立后才能 --resume；
        # 对不存在的 UUID 直接 --resume 会报错，所以必须显式记录。
        # 不能用 task.attempts>0 判定：claim_next_pending 领取时已把 attempts 置 ≥1）
        resume = self._db.get_state(f"claude_session_inited:{session.claude_uuid}") is not None
        argv = build_argv(
            session_uuid=session.claude_uuid,
            resume=resume,
            policy=session.policy,
            budget=self._cfg.budget,
            mcp_config=self._cfg.repo_root / "claude" / "mcp.json",
            settings=self._cfg.repo_root / "claude" / "settings.json",
        )
        bin_ = self._cfg.claude_bin
        prefix = bin_ if isinstance(bin_, list) else [bin_]
        env = os.environ.copy()
        env.update(self._cfg.secrets)

        try:
            proc = await asyncio.create_subprocess_exec(
                *prefix, *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=session.cwd, env=env)
        except OSError as e:
            await self._fail(task, session.wechat_user, f"无法启动 claude 子进程: {e}")
            return

        entry = TrackedProcess(proc)
        self._procs[task.id] = entry
        try:
            # prompt 经 stdin 原样传入（不走 argv，无 shell 转义）；cwd = 会话绑定目录
            stdout, _stderr = await proc.communicate(task.prompt.encode("utf-8"))
        finally:
            self._procs.pop(task.id, None)

        # 流内可记账的先记（cost / slash_commands），最终回复去留由退出路径决定
        result_text = self._consume(task, session, stdout.decode("utf-8", "replace"))

        if entry.killed or self._db.get_task(task.id).state == "canceled":
            self._db.finish_task(task.id, "canceled")
            self._push(task, session.wechat_user, "已取消。")
            return

        if proc.returncode != 0:
            await self._fail(task, session.wechat_user, f"claude 退出码 {proc.returncode}")
            return

        for page in split_text(result_text or "(空回复)", self._cfg.page_char_limit):
            self._push(task, session.wechat_user, page)
        self._db.finish_task(task.id, "done")
        self._db.set_state(f"claude_session_inited:{session.claude_uuid}", "1")
        self._db.touch_session(session.id)

    def _consume(self, task, session, raw: str) -> str:
        """逐行解析 stdout：同步 slash_commands、节流推工具进度、费用入审计。

        返回 result 文本（最终回复由调用方按退出路径决定推送与否）。
        """
        parser = StreamParser()   # 有状态（tool_use 增量回填），每次 run 新建
        throttle = Throttle(float(self._cfg.throttle["progress_window_s"]))
        pending: list = []        # 待推送的 tool 事件（text 会被后续增量延迟填充）

        def flush(force: bool = False) -> None:
            if not pending:
                return
            if not force and not throttle.allow():
                return
            lines = []
            for ev in pending:
                detail = " ".join(ev.text.split())[:_PROGRESS_DETAIL_LIMIT]
                lines.append(f"🔧 {ev.tool_name}" + (f" {detail}" if detail else ""))
            self._push(task, session.wechat_user, "\n".join(lines))
            pending.clear()

        result_text = ""
        for line in raw.splitlines():
            ev = parser.feed_line(line)
            if ev is None:
                continue
            if ev.type == "init":
                if ev.slash_commands:
                    self._db.set_state("slash_commands",
                                       json.dumps(ev.slash_commands, ensure_ascii=False))
                if ev.session_id and ev.session_id != session.claude_uuid:
                    self._db.audit("session_uuid_drift",
                                   f"expected={session.claude_uuid} got={ev.session_id}")
            elif ev.type == "tool":
                pending.append(ev)
                flush()
            elif ev.type == "text":
                pass  # 增量文本不推（微信端不可编辑，等最终版）
            elif ev.type == "result":
                result_text = ev.text
                if ev.cost_usd is not None:
                    self._db.audit("cost", json.dumps({"task_id": task.id, "usd": ev.cost_usd}))
        flush(force=True)   # 尾部兜底：节流窗口内残留的最后一批也送达

        return result_text

    def _push(self, task, to_user: str, text: str) -> None:
        self._db.enqueue(task.id, to_user, text)

    async def _fail(self, task, to_user: str, err: str) -> None:
        self._db.finish_task(task.id, "failed")   # 未耗尽重试次数 → 回 pending 由 db 决定
        self._db.audit("task_failed", f"task={task.id} err={err}")
        self._push(task, to_user, f"❌ 任务失败：{err}")
