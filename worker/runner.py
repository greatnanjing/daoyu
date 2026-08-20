"""单任务执行：组装 argv → 子进程 → 实时逐行解析流 → 节流推进度 → 最终回复入 outbox。"""
import asyncio
import glob
import json
import logging
import os
import re
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

from common.text import split_text
from common.config import host_claude_env, merge_claude_secrets
from worker.cli_builder import (APPROVAL_MCP_SERVER, BYPASS_DISALLOWED_TOOLS,
                                OCR_MCP_SERVER, POLICY_MODE, build_argv,
                                claude_config_dir, expand_platform,
                                inject_linux_chrome, inject_linux_playwright)
from worker.stream import StreamParser, Throttle

log = logging.getLogger(__name__)

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
# claude --bg stdout 首行的后台任务 id。实测（2026-08-16，claude 2.1.233）首行
# 形如 "backgrounded → <8hex>"；Windows cp936 管道下箭头会乱码，故只锚
# "backgrounded" 后按 UTF-8 errors=replace 解码再抓 ≥6 位 hex id。
_BG_ID_RE = re.compile(r"backgrounded.*?([0-9a-f]{6,})")
# 临时 mcp config 前缀：主进程被 kill 时 finally 不执行会残留，启动时按前缀清扫
_MCP_TMP_PREFIX = "daoyu-mcp-"

# -p 任务 prompt 的环境约定后缀（仅普通对话；斜杠命令转发不加——追加文本会
# 破坏命令解析）。CLAUDE.md 同款约定实测三次均被模型默认行为压过（截图场景
# claude 恒"存盘+描述"不调 send_image——软指令天花板），任务级 prompt 遵循度
# 最高，故在此强制注入（真机实证 2026-08-20）。
_PROMPT_SUFFIX = (
    "\n\n（刀鱼环境约定：用户在微信端，只能看到你回传的文本与 "
    "mcp__daoyu__send_image 工具发出的图片。任何截图/生成图片的操作，必须"
    "紧接着调用 mcp__daoyu__send_image(path, caption) 把原图回传微信——"
    "仅存盘用户看不到，描述不能替代原图。此为硬性要求。）"
)


def _cleanup_stale_mcp_configs() -> None:
    """清扫上次进程被 kill 时残留的临时 mcp config（正常路径每次任务结束即删）。
    只认 daoyu-mcp-*.json 前缀，绝不碰 tempdir 其他文件。"""
    for p in glob.glob(os.path.join(tempfile.gettempdir(), _MCP_TMP_PREFIX + "*.json")):
        try:
            os.unlink(p)
        except OSError:
            pass   # 竞态下已被他人删除/占用——幂等


def _spawn_kwargs() -> dict:
    """claude 子进程 spawn 的平台 kwargs。

    POSIX：start_new_session 建独立进程组（pgid==pid），/cancel 时 killpg 整树
    杀掉——claude 拉起的 MCP server 是孙进程，只杀 claude 本体会留孙进程持
    管道/占资源（M1 移交技术债，真机实证见 progress 台账）。Windows：不建组，
    杀树走 taskkill /T 按父子链遍历，与进程组无关。"""
    if sys.platform == "win32":
        return {}
    return {"start_new_session": True}


def kill_process_tree(proc: asyncio.subprocess.Process) -> None:
    """杀子进程整棵树（/cancel 与流异常孤儿兜底共用）。

    POSIX：killpg(pid)——spawn 带 start_new_session 时 pgid==pid，直接用 pid，
    不做 getpgid（进程已死时 getpgid 抛错的竞态点）。Windows：taskkill /F /T
    按父子链杀。组杀未命中（异常或 taskkill 非零退出）兜底杀直接子进程。"""
    if proc.returncode is not None:
        return   # 已退出，无需 kill
    group_killed = False
    try:
        if sys.platform == "win32":
            cp = subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True, timeout=10)
            group_killed = cp.returncode == 0
        else:
            os.killpg(proc.pid, signal.SIGKILL)
            group_killed = True
    except (OSError, subprocess.SubprocessError) as e:
        # ProcessLookupError/PermissionError 均为 OSError 子类；
        # taskkill 超时为 TimeoutExpired（SubprocessError 子类）
        log.warning("进程组 kill 异常，兜底杀直接子进程: pid=%s err=%r",
                    proc.pid, e)
    if not group_killed:
        try:
            proc.kill()
        except ProcessLookupError:
            pass   # 进程已自行退出，kill 只是兜底


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
        kill_process_tree(self._proc)


class TaskRunner:
    def __init__(self, db, config, process_registry: dict[int, TrackedProcess]):
        self._db = db
        self._cfg = config
        self._procs = process_registry
        # kill 残留的临时 mcp config 只能靠启动清扫（finally 在 kill 路径不执行）
        _cleanup_stale_mcp_configs()

    @property
    def procs(self):
        """取消注册表（pool 的 /cancel 经此 kill 运行中任务）。"""
        return self._procs

    async def run(self, task, session) -> None:
        if task.kind == "bg":
            return await self._run_bg(task, session)
        static_mcp = self._cfg.repo_root / "claude" / "mcp.json"
        # 四档通用：临时合并 mcp config（静态清单 + daoyu server 条目，含
        # 本机绝对路径与任务级 env——不能进 git 的静态 mcp.json）。run 的每条出口
        # （成功/失败/取消/异常）都在 finally 删除，不留临时文件。
        tmp_mcp: str | None = None
        try:
            strict = session.policy == "strict"
            # M3：四档都合并 daoyu server（strict=approve+send_image，其余=
            # send_image）。临时文件生命周期不变（finally 删 + 启动清扫）。
            tmp_mcp = self._write_daoyu_mcp_config(
                task, session, static_mcp,
                tools="approve,send_image" if strict else "send_image")
            # 该 Claude 会话是否已被首次调用过（--session-id 建立后才能 --resume；
            # 对不存在的 UUID 直接 --resume 会报错，所以必须显式记录。
            # 不能用 task.attempts>0 判定：claim_next_pending 领取时已把 attempts 置 ≥1）
            resume = self._db.get_state(f"claude_session_inited:{session.claude_uuid}") is not None
            argv = build_argv(
                session_uuid=session.claude_uuid,
                resume=resume,
                policy=session.policy,
                budget=self._cfg.budget,
                mcp_config=Path(tmp_mcp),
                settings=self._cfg.repo_root / "claude" / "settings.json",
                approval_mcp=strict,
            )
            bin_ = self._cfg.claude_bin
            prefix = bin_ if isinstance(bin_, list) else [bin_]
            env = os.environ.copy()
            # 凭据/模型：宿主 settings.json 动态层优先（key/模型映射会变），
            # secrets.env 兜底（见 merge_claude_secrets）
            env.update(merge_claude_secrets(self._cfg.secrets, host_claude_env()))
            # 机制化隔离宿主 ~/.claude（--bare/--settings 实测均不能隔离）。
            # ⚠️ 开关默认关：2026-08-16 本机（Windows+代理环境）实测 CLAUDE_CONFIG_DIR
            # 重定向后 claude 启动挂死（连上 API 后等响应、CPU 零增长；宿主形态正常）。
            # Linux 服务器部署时置 true 并实测；关闭时 strict 审批会被宿主 allow
            # 规则穿透（C3 已知代价，见 m2-final-review.md）。
            if getattr(self._cfg, "worker", {}).get("isolate_claude_config", False):
                env["CLAUDE_CONFIG_DIR"] = claude_config_dir(self._cfg.repo_root)

            try:
                proc = await asyncio.create_subprocess_exec(
                    *prefix, *argv,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    limit=_STREAM_LIMIT,
                    cwd=session.cwd, env=env, **_spawn_kwargs())
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
                # prompt 经 stdin 传入（不走 argv，无 shell 转义）；cwd = 会话绑定目录。
                # 斜杠命令转发原样传（追加文本破坏命令解析）；普通对话拼环境约定后缀
                prompt = (task.prompt if task.prompt.startswith("/")
                          else task.prompt + _PROMPT_SUFFIX)
                try:
                    proc.stdin.write(prompt.encode("utf-8"))
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
                # 孤儿进程（整树杀，MCP 孙进程不残留）、wait 收尸、走 _fail ——
                # 任务不卡 running、用户有反馈。
                kill_process_tree(proc)
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
                err = (f"预算/回合上限（{result_subtype}）: "
                       f"{result_text[:_RESULT_TAIL_CHARS]}")
                await self._dead(task, session.wechat_user, err)
                self._alert_all(f"⚠️ 任务 #{task.id} 预算/回合耗尽死信"
                                f"（{result_subtype}）：{result_text[:100]}")
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
        finally:
            if tmp_mcp is not None:
                try:
                    os.unlink(tmp_mcp)
                except FileNotFoundError:
                    pass   # 已被清理（如测试快照后的极端竞态）——幂等

    async def _run_bg(self, task, session) -> None:
        """bg 任务启动分支：claude --bg <prompt>（prompt 走 argv 参数，无 stdin）
        → stdout 解析后台 id 落盘 → 回执 → 立即返回。任务保持 running，由
        pool._bg_watcher 轮询 agents --json 接管，此处绝不 finish_task。

        flag 集：--bare + 预算 + --permission-mode + --settings（硬 deny 清单与
        -p 同样生效）+ bypass 档 --disallowedTools（与 -p 同源常量）。不传
        --permission-prompt-tool —— strict 档审批 MCP 在 bg 下暂不支持，回执明示；
        也不带 -p 全量 flag（--session-id/--resume 等会话由后台守护进程自管）。
        M3 真机实证（2026-08-19，生产服务器）：--bg 不传 --mcp-config——daemon 异步
        拉起 worker（客户端返回 ~1s 后才读 mcp config），临时文件在 run() 返回
        即删 → daemon "exit 1 before init" 100% 复现（daemon.log 三次三崩）；
        持久化文件+终态清理的替代方案其启动清扫会误删存活 bg 任务文件（gateway
        重启后 bg 仍活着），引入新竞态不值当。bg 会话因此无 MCP 工具（send_image
        不可用），回执明示；CLI 侧修复该竞态后可再装回。"""
        env = os.environ.copy()
        env.update(merge_claude_secrets(self._cfg.secrets, host_claude_env()))
        if getattr(self._cfg, "worker", {}).get("isolate_claude_config", False):
            env["CLAUDE_CONFIG_DIR"] = claude_config_dir(self._cfg.repo_root)
        bin_ = self._cfg.claude_bin
        prefix = bin_ if isinstance(bin_, list) else [bin_]
        # prompt 以 "-" 开头会被 CLI 解析成 flag → 前置空格防误读（单用户自伤
        # 场景，预算闸兜底，一行防御即可）
        prompt = task.prompt if not task.prompt.startswith("-") else " " + task.prompt
        argv = ["--bare",
                "--settings", str(self._cfg.repo_root / "claude" / "settings.json"),
                "--max-turns", str(self._cfg.budget.max_turns),
                "--max-budget-usd", str(self._cfg.budget.max_usd),
                "--permission-mode", POLICY_MODE[session.policy]]
        if session.policy == "bypass":
            argv += ["--disallowedTools", ",".join(BYPASS_DISALLOWED_TOOLS)]
        argv += ["--bg", prompt]
        try:
            proc = await asyncio.create_subprocess_exec(
                *prefix, *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=_STREAM_LIMIT,
                cwd=session.cwd, env=env, **_spawn_kwargs())
        except OSError as e:
            await self._fail(task, session.wechat_user, f"无法启动 claude 后台子进程: {e}")
            return
        # 注册进程句柄（M4）：launch 阶段（bg_id 尚未落盘）CLI 若悬挂，/cancel
        # 才能走既有 kill 路径，而不是"稍后再试"干等、session 槽位被占到重启。
        entry = TrackedProcess(proc)
        self._procs[task.id] = entry
        try:
            stdout, stderr = await proc.communicate()   # 两条管道并发收，无死锁风险
        finally:
            self._procs.pop(task.id, None)
        out = stdout.decode("utf-8", "replace")
        m = _BG_ID_RE.search(out)
        if entry.killed or self._db.get_task(task.id).state == "canceled":
            # launch 被用户取消：canceled 终态已由 cancel 落盘，绝不走 _fail
            # （failed→pending 的重试会把用户刚取消的任务再发一遍）
            return
        if proc.returncode != 0 or m is None:
            err = f"后台启动失败（rc={proc.returncode}）: {out[-_RESULT_TAIL_CHARS:]}"
            tail = stderr.decode("utf-8", "replace").strip()
            if tail:
                err += f": {tail[-_STDERR_TAIL_CHARS:]}"
            await self._fail(task, session.wechat_user, err)
            return
        bg_id = m.group(1)
        self._db.set_bg_id(task.id, bg_id)
        receipt = (f"🚀 已在后台启动（任务 #{task.id}，后台 id {bg_id}）。"
                   f"完成后自动推送结果，/tasks 查进度、/cancel 取消。"
                   f"（后台任务不装载 MCP 工具，需要发图等能力请同步跑）")
        if session.policy == "strict":
            # strict 档 bg 不传审批 MCP（--bg 组合保守集）——必须明示用户，
            # 静默降档违背"选 strict = 要审批"的预期（M5）。deny 清单经
            # --settings 照常生效（I3：与 -p 一致）。
            receipt += ("（注：后台任务不走微信审批；strict 档下需审批的工具"
                        "（Bash/写文件）会被直接拒绝，仅适合只读任务，"
                        "要执行操作请同步跑）")
        self._push(task, session.wechat_user, receipt)

    def _write_daoyu_mcp_config(self, task, session, static_path: Path, tools: str) -> str:
        """四档通用临时 mcp config：静态 mcp.json 的 mcpServers 过滤 disabled、
        按平台展开（Windows npx/uvx 包 cmd /c）后合并 daoyu server 条目
        （tools 按档传 approve,send_image 或 send_image）与 daoyu-ocr 能力面
        条目（恒注入，无 env）。daoyu server 是
        claude 拉起的孙进程，env 经 config 条目注入（claude 子进程 env 无需感知）；
        command 用 sys.executable（runner 与 server 同解释器，Windows 下为 venv
        python 绝对路径，可靠无 PATH 依赖）。返回临时文件路径（NamedTemporaryFile
        前缀 daoyu-mcp-、delete=False，调用方负责删除；kill 残留由 runner 启动时
        按前缀清扫）。静态清单缺文件或坏 JSON/不可读时按空清单合并（仅
        daoyu/daoyu-ocr 系统条目）+ log.warning——与缺文件同策略 fail-open，
        不因 mcp.json 异常拖垮任务主路径（M3 起四档 + bg 全任务都走此装配，
        静默全灭不可接受）。"""
        if static_path.exists():
            try:
                static = json.loads(static_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                log.warning("静态 mcp.json 读取/解析失败，按空清单合并: %s err=%r",
                            static_path, e)
                static = {}
        else:
            log.warning("静态 mcp.json 缺席，按空清单合并（仅 daoyu/daoyu-ocr "
                        "系统条目）: %s", static_path)
            static = {}
        # 余项 A：disabled 条目过滤（不进临时文件 = claude 视为不存在）；
        # disabled 为非 list（坏文件）按空处理，list 内非字符串元素（含不可哈希）
        # 一并忽略——整层与 fail-open 策略一致，不因坏文件拖垮任务。
        disabled = static.get("disabled")
        disabled = {d for d in disabled if isinstance(d, str)} if isinstance(disabled, list) else set()
        servers = {k: v for k, v in static.get("mcpServers", {}).items()
                   if k not in disabled}
        # 平台无关条目 → 实际拉起形态（Windows 白名单命令包 cmd /c；Linux 给
        # chrome-devtools 与 playwright 注入本机 headless Chrome 装配——约定路径
        # 命中才注入，未安装 no-op，详见两 inject 函数）
        servers = expand_platform(servers, sys.platform == "win32")
        if sys.platform != "win32":
            servers = inject_linux_chrome(servers, Path.home())
            servers = inject_linux_playwright(servers, Path.home())
        merged = {"mcpServers": {
            **servers,
            APPROVAL_MCP_SERVER: {
                "type": "stdio",
                "command": sys.executable,
                "args": [str(Path(__file__).resolve().parent / "approval_mcp.py")],
                "env": {
                    "DAOYU_DB": os.path.abspath(self._db.path),
                    "DAOYU_TASK_ID": str(task.id),
                    "DAOYU_TO_USER": session.wechat_user,
                    "DAOYU_TOOLS": tools,
                },
            },
            # 余项 B：daoyu-ocr 能力面系统条目——恒注入（disabled 不管辖）、
            # 无 DB/env 依赖（ocr_mcp.py 自举 repo 根、引擎 lazy）。
            OCR_MCP_SERVER: {
                "type": "stdio",
                "command": sys.executable,
                "args": [str(Path(__file__).resolve().parent / "ocr_mcp.py")],
                "env": {},
            },
        }}
        with tempfile.NamedTemporaryFile(prefix=_MCP_TMP_PREFIX, suffix=".json",
                                         delete=False,
                                         mode="w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=2)
            return f.name

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

    def _alert_all(self, text: str) -> None:
        """监控告警（M2）：复用出站通道推全部白名单用户（outbox 循环 ≤0.5s
        取走）。enqueue 是同步 DB 写，不会抛出破坏任务主路径；cfg 无
        whitelist 属性（测试 FakeConfig）时静默跳过。"""
        for user in sorted(getattr(self._cfg, "whitelist", None) or ()):
            self._db.enqueue(None, user, text)

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
