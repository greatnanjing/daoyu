"""单任务执行：组装 argv → 子进程 → 实时逐行解析流 → 节流推进度 → 最终回复入 outbox。"""
import asyncio
import json
import os

from common.text import split_text
from worker.cli_builder import build_argv
from worker.stream import StreamParser, Throttle

# 进度推送里工具命令 JSON 的截断长度（够认出在跑什么即可）
_PROGRESS_DETAIL_LIMIT = 60
# stdout/stderr StreamReader 行上限（默认 64KB 太小：result 行内嵌完整回复，
# 长报告/重写大文件时 > 64KB 很常见，超限 readline 抛 ValueError）
_STREAM_LIMIT = 8 * 1024 * 1024
# 失败诊断里 stderr 尾部的截断长度（字符）
_STDERR_TAIL_CHARS = 500
# 失败诊断里 result 文本（claude 印出的失败原因）的截断长度（字符）
_RESULT_TAIL_CHARS = 500
# stderr 并发收取时内存里保留的尾部字节数（防子进程刷屏撑大内存）
_STDERR_TAIL_BYTES = 8192
# result subtype 中"确定性失败"的前缀：预算/回合耗尽重试无意义（每次调用带
# 全新预算，重跑=再烧一份上限，违背 NFR-5 每任务上限语义）→ 直接死信不回队
_NO_RETRY_SUBTYPES = ("error_max_turns", "error_max_budget_usd")


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

    @property
    def procs(self):
        """取消注册表（pool 的 /cancel 经此 kill 运行中任务）。"""
        return self._procs

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
                limit=_STREAM_LIMIT,
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
        result_subtype: str | None = None
        result_is_error = False
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
                    result_subtype = ev.subtype
                    result_is_error = ev.is_error
                    if ev.cost_usd is not None:
                        self._db.audit("cost", json.dumps({"task_id": task.id, "usd": ev.cost_usd}))
            await proc.wait()
            stderr_tail = (await stderr_task).decode("utf-8", "replace").strip()
        except Exception as e:
            # 流读取/解析任何异常（如单行超 _STREAM_LIMIT 的 ValueError）：kill 防
            # 孤儿进程、wait 收尸、走 _fail —— 任务不卡 running、用户有反馈。
            try:
                proc.kill()
            except ProcessLookupError:
                pass   # 进程已自行退出，kill 只是兜底
            await proc.wait()
            stderr_task.cancel()
            await self._fail(task, session.wechat_user, f"输出流读取/解析异常: {e!r}")
            return
        finally:
            self._procs.pop(task.id, None)
            stderr_task.cancel()   # 正常路径已 await 完；此处兜底异常路径不泄漏

        if entry.killed or self._db.get_task(task.id).state == "canceled":
            # 取消不推尾批进度与半截 result，只告知终态
            self._db.finish_task(task.id, "canceled")
            self._push(task, session.wechat_user, "已取消。")
            return

        # I-3：预算/回合耗尽（result subtype 明示）是确定性失败——重试只会带
        # 全新预算再烧一遍 → 不重试，直接死信。失败原因印在 result 文本里
        # （stderr 可能为空），必须并入错误消息，否则排障信息全丢。
        if result_subtype and result_subtype.startswith(_NO_RETRY_SUBTYPES):
            await self._dead(task, session.wechat_user,
                             f"预算/回合上限（{result_subtype}）: "
                             f"{result_text[:_RESULT_TAIL_CHARS]}")
            return

        if proc.returncode != 0:
            if result_is_error and result_subtype and result_subtype.startswith("error_"):
                # 其余运行内错误（error_during_execution 等）：同为确定性失败，
                # 不重试（官方文档：运行内失败印在 result 且非零退出）
                await self._dead(task, session.wechat_user,
                                 f"claude 运行错误（{result_subtype}）: "
                                 f"{result_text[:_RESULT_TAIL_CHARS]}")
                return
            err = f"claude 退出码 {proc.returncode}"
            if result_text:
                err += f": {result_text[:_RESULT_TAIL_CHARS]}"
            if stderr_tail:
                err += f": {stderr_tail[-_STDERR_TAIL_CHARS:]}"
            await self._fail(task, session.wechat_user, err)
            return

        flush(force=True)   # 尾批兜底：节流窗口内残留的最后一批也送达（仅成功路径）
        for page in split_text(result_text or "(空回复)",
                               self._cfg.throttle["page_char_limit"]):
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

    async def _dead(self, task, to_user: str, err: str) -> None:
        """确定性失败（预算/回合耗尽等）：直接死信不回队。finish_task 传 "dead"
        而非 "failed"，天然绕过未耗尽→pending 的重试回退（无需 db 加参数）。"""
        self._db.finish_task(task.id, "dead")
        self._db.audit("task_failed", f"task={task.id} err={err}")
        self._push(task, to_user, f"❌ 任务失败（不重试）：{err}")
