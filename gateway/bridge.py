"""桥命令（gateway 本地秒回）与 /help 生成。"""
import json
import os
import time

BRIDGE_HELP = {
    "cancel": "/cancel <任务号> — 取消任务",
    "tasks": "/tasks — 查看 running/pending 任务",
    "status": "/status — 队列深度、死信数、当日费用、连接剩余",
    "cd": "/cd <目录|#序号> — 切换工作目录（=切换 Claude 会话）",
    "sessions": "/sessions — 列出会话（/cd #n 快速切换）",
    "policy": "/policy <auto|strict|bypass|plan> — 权限档位",
    "bg": "/bg <任务描述> — 转入后台长任务（claude --bg，完成自动回报结果）",
}
ILINK_HELP = {
    "time": "/time — 连接剩余时间",
    "重新连接": "/重新连接 — 立即重新扫码连接",
    "help": "/help — 本帮助",
}
POLICIES = ("auto", "strict", "bypass", "plan")


def _active_session(db, from_user: str, default_cwd: str):
    cwd = db.get_active_cwd(from_user, default_cwd)
    return db.get_or_create_session(from_user, cwd)


async def execute_bridge(db, pool, route, from_user: str, config) -> str:
    cmd = route.command
    if cmd == "cancel":
        arg = route.args.strip()
        if arg.isdigit():
            return await pool.cancel(int(arg))
        if not arg:
            # Ctrl+C 语义（PRD FR-2）：无参数 = 取消当前会话最新运行中任务
            cwd = db.get_active_cwd(from_user, config.default_cwd)
            sid = db.get_or_create_session(from_user, cwd).id
            running = [t for t in pool.snapshot()
                       if t.session_id == sid and t.state == "running"]
            if running:
                return await pool.cancel(running[-1].id)
            return "当前会话没有运行中的任务。用法：/cancel <任务号>（/tasks 查看）"
        return "用法：/cancel <任务号>（/tasks 查看）"
    if cmd == "tasks":
        rows = pool.snapshot()
        if not rows:
            return "当前没有运行中或排队的任务。"
        lines = []
        for t in rows:
            mark = "▶️" if t.state == "running" else "⏳"
            lines.append(f"{mark} #{t.id} [{t.kind}] {t.prompt[:40]}")
        return "\n".join(lines)
    if cmd == "status":
        cost = db.today_cost_usd()
        remain = _remain_text(db, config)
        return (f"队列：{db.queue_depth()} 排队 / {len(pool.running_session_ids())} 会话运行中\n"
                f"死信：{db.dead_letter_count()}\n"
                f"当日费用：${cost:.2f}\n"
                f"连接剩余：{remain}")
    if cmd == "cd":
        path = route.args.strip()
        if not path:
            cwd = db.get_active_cwd(from_user, config.default_cwd)
            sessions = db.list_sessions(from_user)
            lines = [f"当前目录：{cwd}", "历史会话："]
            lines += [f"  · {s.cwd}" for s in sessions[:10]]
            lines.append("提示：/sessions 查看全部会话，/cd #n 快速切换")
            return "\n".join(lines)
        if path.startswith("#") and path[1:].isdigit():
            # /sessions 序号切换：列表按 last_active_at DESC 排，#n 即第 n 个
            sessions = db.list_sessions(from_user)
            n = int(path[1:])
            if not 1 <= n <= len(sessions):
                return f"序号超出范围（共 {len(sessions)} 个会话）"
            target = sessions[n - 1]
            db.set_active_cwd(from_user, target.cwd)
            return f"已切换到 {target.cwd}（/sessions 序号 #{n}）"
        if not os.path.isdir(path):
            return f"目录不存在：{path}"
        db.set_active_cwd(from_user, path)
        db.get_or_create_session(from_user, path)
        return f"已切换到 {path}（新目录 = 新 Claude 会话）"
    if cmd == "sessions":
        cwd = db.get_active_cwd(from_user, config.default_cwd)
        sessions = db.list_sessions(from_user)
        if not sessions:
            return "当前没有会话。"
        lines = []
        for i, s in enumerate(sessions, 1):
            mark = "▶" if s.cwd == cwd else "  "
            summary = db.last_task_summary(s.id) or "（无任务）"
            lines.append(f"{mark} #{i} {s.cwd}（{_rel_time(s.last_active_at)}）{summary}")
        return "\n".join(lines)
    if cmd == "policy":
        arg = route.args.strip().lower()
        if not arg:
            s = _active_session(db, from_user, config.default_cwd)
            return f"当前档位：{s.policy}\n可切换：{'/'.join(POLICIES)}"
        if arg not in POLICIES:
            return f"无效档位 {arg}。可切换：auto/strict/bypass/plan"
        s = _active_session(db, from_user, config.default_cwd)
        db.set_policy(s.id, arg)
        db.audit("policy", f"user={from_user} session={s.id} → {arg}")
        return f"权限档位已切换为 {arg}（下一条消息生效）。"
    if cmd == "bg":
        prompt = route.args.strip()
        if not prompt:
            return "用法：/bg <任务描述> — 转入后台执行长任务（/tasks 查进度、/cancel 取消）"
        cwd = db.get_active_cwd(from_user, config.default_cwd)
        s = db.get_or_create_session(from_user, cwd)
        tid = db.create_task(None, s.id, prompt, kind="bg")
        await pool.submit_check()
        return f"已转后台（任务 #{tid}），/tasks 查进度、/cancel 取消。"
    return f"未知桥命令 {cmd}"


def _rel_time(ts: int) -> str:
    """/sessions 相对活跃时间：<1分钟 / 约N分钟 / 约N小时 / N天前。"""
    delta = max(0, time.time() - ts)
    if delta < 60:
        return "<1分钟"
    if delta < 3600:
        return f"约{int(delta // 60)}分钟"
    if delta < 86400:
        return f"约{int(delta // 3600)}小时"
    return f"{int(delta // 86400)}天前"


def _remain_text(db, config) -> str:
    login_at = float(db.get_state("login_at") or 0)
    dur = config.reconnect.get("session_duration_s", 86400)
    remain = max(0.0, login_at + dur - time.time())
    h, m = int(remain // 3600), int(remain % 3600 // 60)
    return f"{h} 小时 {m} 分钟" if h else f"{m} 分钟"


def build_help(db) -> str:
    try:
        forwarded = json.loads(db.get_state("slash_commands") or "[]")
    except ValueError:
        forwarded = []
    lines = ["刀鱼可用命令（与 Claude Code CLI 同一套语法）：", ""]
    lines += [f"  {d}" for d in BRIDGE_HELP.values()]
    lines += [f"  {d}" for d in ILINK_HELP.values()]
    if forwarded:
        lines.append(f"  可转发给 Claude：{' '.join('/' + c for c in forwarded)}")
    lines.append("")
    lines.append("其余文本直接作为对话发给 Claude。")
    return "\n".join(lines)


async def execute_ilink_op(db, route, from_user: str, config, reconnect_fn) -> str:
    if route.command == "help":
        return build_help(db)
    if route.command == "time":
        return f"当前连接剩余时间：{_remain_text(db, config)}"
    if route.command == "重新连接":
        db.set_state("reconnect_confirm", from_user)
        return "确认立即重新连接？回复 Y 确认 / N 取消"
    return "未知运维命令"
