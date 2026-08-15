"""单任务执行：组装 argv → 子进程 → 实时逐行解析流 → 节流推进度 → 最终回复入 outbox。"""
import asyncio
import json
import os

from worker.cli_builder import build_argv
from worker.stream import StreamParser, Throttle

# 进度推送里工具命令 JSON 的截断长度（够认出在跑什么即可）
_PROGRESS_DETAIL_LIMIT = 60
# 失败诊断里 stderr 尾部的截断长度（字符）
_STDERR_TAIL_CHARS = 500
# stderr 并发收取时内存里保留的尾部字节数（防子进程刷屏撑大内存）
_STDERR_TAIL_BYTES = 8192


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
        # stderr 与 stdout 消费并发收取（只保尾部）：若等进程退出后再读，子进程
        # 把 stderr 写满管道缓冲后会反过来卡死自己的 stdout → 任务整体挂死。
        stderr_task = asyncio.create_task(self._drain_stderr_tail(proc.stderr))

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
        try:
            # prompt 经 stdin 原样传入（不走 argv，无 shell 转义）；cwd = 会话绑定目录
            try:
                proc.stdin.write(task.prompt.encode("utf-8"))
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass   # 子进程已先退出致管道断裂：stdout 随即 EOF，走退出码路径
            finally:
                proc.stdin.close()

            # 实时逐行消费 stdout（FR-8）：进度在循环内即时节流推送，不等进程
            # 退出——长任务全程不再静默。result 是最后一行，流中遇到即处理。
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                ev = parser.feed_line(line.decode("utf-8", "replace"))
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
            await proc.wait()
            stderr_tail = (await stderr_task).decode("utf-8", "replace").strip()
        finally:
            self._procs.pop(task.id, None)
            stderr_task.cancel()   # 正常路径已 await 完；此处兜底异常路径不泄漏

        if entry.killed or self._db.get_task(task.id).state == "canceled":
            # 取消不推尾批进度与半截 result，只告知终态
            self._db.finish_task(task.id, "canceled")
            self._push(task, session.wechat_user, "已取消。")
            return

        if proc.returncode != 0:
            err = f"claude 退出码 {proc.returncode}"
            if stderr_tail:
                err += f": {stderr_tail[-_STDERR_TAIL_CHARS:]}"
            await self._fail(task, session.wechat_user, err)
            return

        flush(force=True)   # 尾批兜底：节流窗口内残留的最后一批也送达（仅成功路径）
        for page in split_text(result_text or "(空回复)", self._cfg.page_char_limit):
            self._push(task, session.wechat_user, page)
        # 先置位再完结：消除"done 但未置位 → 下次对已存在会话误用 --session-id"窗口
        self._db.set_state(f"claude_session_inited:{session.claude_uuid}", "1")
        self._db.finish_task(task.id, "done")
        self._db.touch_session(session.id)

    @staticmethod
    async def _drain_stderr_tail(stream: asyncio.StreamReader) -> bytes:
        """stderr 读到 EOF，内存里只保留尾部（诊断用，不参与进度）。"""
        buf = bytearray()
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                break
            buf.extend(chunk)
            del buf[:-_STDERR_TAIL_BYTES]
        return bytes(buf)

    def _push(self, task, to_user: str, text: str) -> None:
        self._db.enqueue(task.id, to_user, text)

    async def _fail(self, task, to_user: str, err: str) -> None:
        self._db.finish_task(task.id, "failed")   # 未耗尽重试次数 → 回 pending 由 db 决定
        self._db.audit("task_failed", f"task={task.id} err={err}")
        self._push(task, to_user, f"❌ 任务失败：{err}")
