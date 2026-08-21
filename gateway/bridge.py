"""桥命令（gateway 本地秒回）与 /help 生成。"""
import json
import os
import time
from pathlib import Path

from gateway.router import (BUILTIN_ALIASES, BRIDGE_COMMANDS, ILINK_COMMANDS,
                            PROXY_COMMANDS)

BRIDGE_HELP = {
    "cancel": "/cancel <任务号> — 取消任务",
    "tasks": "/tasks — 查看 running/pending 任务",
    "status": "/status — 队列深度、死信数、当日费用、连接剩余",
    "new": "/new — 在当前目录开新话题（新 Claude 会话，上下文从零开始）",
    "adopt": "/adopt [uuid前缀] — 收养终端里创建的 Claude 会话为当前话题（无参数=最新一个）",
    "delete": "/delete #<序号> — 删话题；/delete task <任务号> — 删任务记录（均需回 Y 确认）",
    "cd": "/cd <目录|#序号> — 切目录或切话题（#序号见 /sessions）",
    "sessions": "/sessions — 按目录列出全部话题（/cd #n 切换、/new 开新）",
    "policy": "/policy <auto|strict|bypass|plan> — 当前话题的权限档位",
    "bg": "/bg <任务描述> — 转入后台长任务（claude --bg，完成自动回报结果）",
    "cron": "/cron — 定时任务（日报/巡检）：on|off、time daily <HH:MM>、interval patrol <分钟>",
    "alias": "/alias add <名> <内容> — 自定义快捷命令（del <名>、list 查看；"
             "内置：/t=/tasks /s=/status /c=/cancel /cs=/sessions）",
}
ILINK_HELP = {
    "time": "/time — 连接剩余时间",
    "重新连接": "/重新连接 — 立即重新扫码连接",
    "help": "/help — 本帮助",
}
# 配置代理层（gateway/proxy.py 已实现的三个；hooks/plugins/login 等未提供，
# 不列——/help 只列当前实际可用命令）
PROXY_HELP = {
    "permissions": "/permissions — 查看权限规则；deny add/del、allow add 读写",
    "mcp": "/mcp — 列出 MCP server；off/on <序号|名字> 启停（下一任务生效）",
    "config": "/config — gateway 配置概览；set <键> <值> 改常用键（重启生效）",
}
POLICIES = ("auto", "strict", "bypass", "plan")


def _active_session(db, from_user: str, default_cwd: str):
    """当前话题绑定（chat /policy /bg /cancel 共用；get_active_binding 语义）。"""
    return db.get_active_binding(from_user, default_cwd)


def _scan_external_transcripts(db, repo_root) -> list[dict]:
    """/adopt 候选：claude-home projects 下未被刀鱼管理的会话 transcript，
    按 mtime 降序（uuid 字典序决胜，同秒确定）。目录不存在（未启用隔离配置/
    从未跑过任务）返回空。跨项目目录 glob——slug 格式平台有异（Linux 连字符、
    Windows 盘符形态），不猜测、直接全扫。"""
    projects = Path(repo_root) / "data" / "claude-home" / "projects"
    if not projects.is_dir():
        return []
    out = []
    for f in projects.glob("*/*.jsonl"):
        if db.get_session_by_uuid(f.stem) is None:
            out.append({"uuid": f.stem, "path": f, "mtime": f.stat().st_mtime})
    out.sort(key=lambda c: (-c["mtime"], c["uuid"]))
    return out


def _transcript_meta(path) -> tuple[str | None, str]:
    """transcript 首段提取 (cwd, 首条用户消息预览≤30字)。cwd 取首个带 cwd 字段
    的行；预览取首条 type==user 且 content 为纯字符串的行（数组形态是
    tool_result 等结构，非人类 prompt）。坏行跳过；首段全无则 cwd=None、
    预览兜底。只读前 200 行，外部长会话不整文件解析。"""
    cwd = None
    preview = None
    with open(path, encoding="utf-8", errors="replace") as fh:
        for i, line in enumerate(fh):
            if i >= 200:
                break
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if cwd is None and isinstance(ev.get("cwd"), str):
                cwd = ev["cwd"]
            if preview is None and ev.get("type") == "user":
                content = (ev.get("message") or {}).get("content")
                if isinstance(content, str) and content.strip():
                    preview = " ".join(content.split())[:30]
            if cwd is not None and preview is not None:
                break
    return cwd, preview or "（外部会话）"


def _adopt(db, arg: str, from_user: str, config) -> str:
    """收养外部会话：候选扫描 → 命中建话题行并切为当前话题。参数支持完整 uuid
    或 ≥8 位唯一前缀（手机输入完整 36 位不现实）；无参数取最新。收养前提是
    transcript 在刀鱼隔离配置目录（CLAUDE_CONFIG_DIR 同源），宿主 ~/.claude 下
    的会话对 runner 不可见，提示里给创建命令。"""
    cands = _scan_external_transcripts(db, getattr(config, "repo_root", "."))
    if arg:
        arg = arg.lower()
        hit = next((c for c in cands if c["uuid"] == arg), None)
        if hit is None and len(arg) >= 8:
            prefixed = [c for c in cands if c["uuid"].startswith(arg)]
            if len(prefixed) == 1:
                hit = prefixed[0]
            elif len(prefixed) > 1:
                return (f"前缀 {arg} 匹配到 {len(prefixed)} 个会话，请加长：\n"
                        + "\n".join(f"  ·{c['uuid'][:8]}" for c in prefixed[:5]))
        if hit is None:
            if db.get_session_by_uuid(arg):
                return "该会话已是刀鱼话题（/sessions 查看）。"
            return f"未找到 uuid 为 {arg} 的可收养会话（须在刀鱼配置目录下创建）。"
    else:
        if not cands:
            return ("未找到可收养的外部会话。终端里须用刀鱼隔离配置目录创建：\n"
                    "CLAUDE_CONFIG_DIR=<repo>/data/claude-home claude\n"
                    "（宿主 ~/.claude 下建的会话刀鱼不可见）")
        hit = cands[0]
    cwd, preview = _transcript_meta(hit["path"])
    if cwd is None:
        return f"会话 {hit['uuid'][:8]} 的 transcript 无法解析出工作目录，收养中止。"
    db.adopt_session(from_user, cwd, hit["uuid"])
    db.set_active_cwd(from_user, cwd)
    db.audit("adopt", f"user={from_user} uuid={hit['uuid']} cwd={cwd}")
    return (f"已收养外部会话为当前话题（目录 {cwd}）：{preview}\n"
            f"（uuid ·{hit['uuid'][:8]}；该会话若仍在终端中打开，先退出再发消息）")


async def execute_bridge(db, pool, route, from_user: str, config) -> str:
    cmd = route.command
    if cmd == "cancel":
        arg = route.args.strip()
        if arg.isdigit():
            return await pool.cancel(int(arg))
        if not arg:
            # Ctrl+C 语义（PRD FR-2）：无参数 = 取消当前会话最新运行中任务
            sid = _active_session(db, from_user, config.default_cwd).id
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
        sent = db.sent_pages_today(int(config.throttle["page_char_limit"]),
                                   md_clean_enabled=bool(
                                       config.throttle.get("md_clean", True)))
        return (f"队列：{db.queue_depth()} 排队 / {len(pool.running_session_ids())} 会话运行中\n"
                f"死信：{db.dead_letter_count()}\n"
                f"当日费用：${cost:.2f}\n"
                f"今日已发送：{sent} 条\n"
                f"连接剩余：{remain}")
    if cmd == "new":
        cwd = _active_session(db, from_user, config.default_cwd).cwd
        db.create_topic(from_user, cwd)
        # 回复不带序号：#N 在本产品专指 /sessions 全局序号口径（/cd #n 按此解析），
        # 目录内序数会诱发错切旧话题
        return f"已开启新话题（目录 {cwd}），上下文从零开始"
    if cmd == "cd":
        path = route.args.strip()
        if not path:
            sessions = db.list_sessions(from_user)
            active = db.get_active_binding(from_user, config.default_cwd, touch=False)
            gidx = {s.id: i for i, s in enumerate(sessions, 1)}
            lines = [f"当前目录：{active.cwd}（话题 #{gidx.get(active.id, '?')}）",
                     "该目录话题："]
            for s in (x for x in sessions if x.cwd == active.cwd):
                mark = "▶" if s.id == active.id else "  "
                summary = db.last_task_summary(s.id) or "（无任务）"
                lines.append(f"{mark} #{gidx[s.id]}  {summary}（{_rel_time(s.last_active_at)}）")
            lines.append("提示：/sessions 查看全部话题，/cd #n 快速切换，/new 开新话题")
            return "\n".join(lines)
        if path.startswith("#") and path[1:].isdigit():
            # 全局序号切话题：序号即 /sessions 显示（last_active_at DESC 全局排序）
            sessions = db.list_sessions(from_user)
            n = int(path[1:])
            if not 1 <= n <= len(sessions):
                return f"序号超出范围（共 {len(sessions)} 个话题）"
            target = sessions[n - 1]
            db.set_active_cwd(from_user, target.cwd)   # 旧 cwd 指针同步保持一致
            db.set_active_session(from_user, target.id)
            return (f"已切换到话题 #{n}（目录 {target.cwd}）："
                    f"{db.last_task_summary(target.id) or '（无任务）'}")
        if not os.path.isdir(path):
            return f"目录不存在：{path}"
        db.set_active_cwd(from_user, path)
        latest = db.latest_topic_in(from_user, path)
        if latest is not None:
            db.set_active_session(from_user, latest.id)
            return f"已切换到 {path}（该目录最新话题）"
        s = db.get_or_create_session(from_user, path)   # 目录无话题 → 自动建
        db.set_active_session(from_user, s.id)
        return f"已切换到 {path}（新话题，上下文从零开始）"
    if cmd == "sessions":
        sessions = db.list_sessions(from_user)
        if not sessions:
            return "当前没有话题。"
        active = db.get_active_binding(from_user, config.default_cwd, touch=False)
        # 两级展示：按目录分组（组按组内最新活跃排序），组内各话题带全局序号
        groups: dict[str, list] = {}
        for idx, s in enumerate(sessions, 1):
            groups.setdefault(s.cwd, []).append((idx, s))
        lines = []
        for cwd, items in groups.items():
            lines.append(f"📂 {cwd}")
            for idx, s in items:
                mark = "▶" if s.id == active.id else "  "
                summary = db.last_task_summary(s.id) or "（无任务）"
                lines.append(f"{mark} #{idx}  {summary}  {_rel_time(s.last_active_at)}"
                             f"  ·{s.claude_uuid[:8]}")
        lines.append("切换：/cd #序号（话题）或 /cd <目录>；/new 开新；/adopt 收养终端会话")
        return "\n".join(lines)
    if cmd == "adopt":
        return _adopt(db, route.args.strip(), from_user, config)
    if cmd == "delete":
        return _delete(db, route.args.strip(), from_user, config)
    if cmd == "policy":
        arg = route.args.strip().lower()
        s = _active_session(db, from_user, config.default_cwd)
        if not arg:
            return f"当前话题档位：{s.policy}\n可切换：{'/'.join(POLICIES)}"
        if arg not in POLICIES:
            return f"无效档位 {arg}。可切换：auto/strict/bypass/plan"
        db.set_policy(s.id, arg)
        db.audit("policy", f"user={from_user} session={s.id} → {arg}")
        return f"权限档位已切换为 {arg}（下一条消息生效）。"
    if cmd == "bg":
        prompt = route.args.strip()
        if not prompt:
            return "用法：/bg <任务描述> — 转入后台执行长任务（/tasks 查进度、/cancel 取消）"
        s = _active_session(db, from_user, config.default_cwd)
        tid = db.create_task(None, s.id, prompt, kind="bg")
        await pool.submit_check()
        return f"已转后台（任务 #{tid}），/tasks 查进度、/cancel 取消。"
    if cmd == "cron":
        return _cron(db, route.args.strip())
    if cmd == "alias":
        return _alias(db, route.args.strip(), from_user)
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
    lines += [f"  {d}" for d in PROXY_HELP.values()]
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


def _delete(db, arg: str, from_user: str, config) -> str:
    """/delete 预置确认门（回 Y 才真删，app.py 拦截执行）。两种形态：
    /delete #<全局序号> 删话题（连同其任务/outbox/approvals）；
    /delete task <任务号> 删单个任务记录。防误删三闸：序号/任务号合法性、
    当前话题拒删（先切走）、pending/running 任务拒删（先 /cancel）。"""
    if not arg:
        return ("用法：/delete #<序号> 删话题（序号见 /sessions）；"
                "/delete task <任务号> 删任务记录（/tasks 查看）")
    if arg.startswith("#") and arg[1:].isdigit():
        sessions = db.list_sessions(from_user)
        n = int(arg[1:])
        if not 1 <= n <= len(sessions):
            return f"序号超出范围（共 {len(sessions)} 个话题）"
        target = sessions[n - 1]
        active = db.get_active_binding(from_user, config.default_cwd, touch=False)
        if target.id == active.id:
            return ("这是当前话题，不能直接删。先 /cd #<其他序号> 或 /new 切走，"
                    "再 /delete。")
        cnt = db.session_task_count(target.id)
        summary = db.last_task_summary(target.id) or "（无任务）"
        db.set_state(f"delete_confirm:{from_user}",
                     json.dumps({"type": "session", "id": target.id}))
        return (f"⚠️ 将删除话题 #{n}（{target.cwd}，含 {cnt} 个任务记录）：{summary}\n"
                f"不可恢复。回复 Y 确认 / N 取消")
    parts = arg.split(None, 1)
    if len(parts) == 2 and parts[0] == "task" and parts[1].isdigit():
        tid = int(parts[1])
        task = db.get_task(tid)
        if task is None:
            return f"没有任务 #{tid}。"
        if task.state in ("pending", "running"):
            return f"任务 #{tid} 仍在 {task.state}，先 /cancel {tid} 再删。"
        db.set_state(f"delete_confirm:{from_user}",
                     json.dumps({"type": "task", "id": tid}))
        return (f"⚠️ 将删除任务 #{tid}（{task.kind}/{task.state}）：{task.prompt[:30]}\n"
                f"不可恢复。回复 Y 确认 / N 取消")
    return ("用法：/delete #<序号> 删话题；/delete task <任务号> 删任务记录"
            "（删除前会请你回 Y 确认）")


def _cron(db, arg: str) -> str:
    """/cron 主动服务管理（M4）：写 cron_jobs 表，scheduler 每轮现读即时生效。
    daily=定时日报 / patrol=周期巡检（详见 scheduler 模块）。"""
    from gateway.scheduler import next_run_time   # 局部导入：scheduler lazy psutil
    parts = arg.split()
    jobs = {j.name: j for j in db.cron_jobs()}
    usage = ("用法：/cron — 列表；/cron on|off <daily|patrol>；"
             "/cron time daily <HH:MM>；/cron interval patrol <分钟>")
    if not parts or parts[0] == "list":
        now = int(time.time())
        lines = []
        for name, icon in (("daily", "📅"), ("patrol", "🔍")):
            j = jobs.get(name)
            if j is None:
                continue
            mark = "✅" if j.enabled else "⏸"
            sched = (f"每天 {j.time_of_day}" if name == "daily"
                     else f"每 {j.interval_min} 分钟")
            nxt = next_run_time(j, now)
            nxt_s = time.strftime("%m-%d %H:%M", time.localtime(nxt)) if nxt else "—"
            lines.append(f"{icon} {name} {mark} {sched}（下次：{nxt_s}）")
            if j.last_run_at:
                lr = time.strftime("%m-%d %H:%M", time.localtime(j.last_run_at))
                lines.append(f"   └ 上次 {lr} · {j.last_result or '—'}")
            else:
                lines.append("   └ 尚未运行")
        lines.append(usage)
        return "\n".join(lines)
    op = parts[0].lower()
    if op in ("on", "off") and len(parts) == 2 and parts[1] in ("daily", "patrol"):
        db.update_cron(parts[1], enabled=1 if op == "on" else 0,
                       touch_last_run=int(time.time()) if op == "on" else None)
        db.audit("cron", f"{op} {parts[1]} user")
        if op == "on":
            return (f"{parts[1]} 已开启（从当前时刻起算：daily 到点即跑、"
                    f"patrol 满一个间隔后跑首轮）。")
        return f"{parts[1]} 已暂停。"
    if op == "time" and len(parts) == 3 and parts[1] == "daily":
        hhmm = parts[2]
        ok = (len(hhmm) == 5 and hhmm[2] == ":" and hhmm[:2].isdigit()
              and hhmm[3:].isdigit() and int(hhmm[:2]) < 24 and int(hhmm[3:]) < 60)
        if not ok:
            return "时间格式应为 HH:MM（如 08:30）。"
        db.update_cron("daily", time_of_day=hhmm)
        db.audit("cron", f"time daily {hhmm}")
        return f"日报时间已改为每天 {hhmm}。"
    if op == "interval" and len(parts) == 3 and parts[1] == "patrol":
        if not parts[2].isdigit() or int(parts[2]) < 1:
            return "间隔应为 ≥1 的分钟数。"
        db.update_cron("patrol", interval_min=int(parts[2]))
        db.audit("cron", f"interval patrol {parts[2]}")
        return f"巡检间隔已改为 {parts[2]} 分钟。"
    return usage


def _alias(db, arg: str, from_user: str) -> str:
    """/alias：用户自定义快捷命令（M5C3，spec §3.6）。存 KV alias:<user> 单键
    JSON dict（merge_pending 同构先例）。撞名规则：系统命令（桥/运维/代理/
    alias 自身）禁止；内置别名（t/s/c/cs）可覆盖（app 层用户展开先于内置映射，
    天然生效）；撞 Claude 动态命令允许但回执提示（用户显式意图优先）。"""
    parts = arg.split(None, 1)
    op = parts[0] if parts else "list"
    rest = parts[1] if len(parts) > 1 else ""
    key = f"alias:{from_user}"

    def _load() -> dict:
        try:
            return json.loads(db.get_state(key) or "{}")
        except ValueError:
            return {}

    if op == "list":
        aliases = _load()
        if not aliases:
            return ("暂无自定义别名。内置：/t=/tasks /s=/status "
                    "/c=/cancel /cs=/sessions")
        lines = [f"快捷命令（{len(aliases)} 条）："]
        for name, value in sorted(aliases.items()):
            lines.append(f"· /{name} → {' '.join(value.split())[:30]}")
        return "\n".join(lines)

    if op == "add":
        sub = rest.split(None, 1)
        if len(sub) != 2:
            return "用法：/alias add <名> <内容>（内容可含空格；del/list 见 /help）"
        name, value = sub[0], sub[1].strip()
        if not name or len(name) > 16:
            return "别名名须为 1~16 个字符（不含空格）。"
        if not value or len(value) > 2000:
            return "别名内容须为 1~2000 字符。"
        reserved = BRIDGE_COMMANDS | ILINK_COMMANDS | PROXY_COMMANDS | {"alias"}
        if name in reserved:
            return f"/{name} 是系统命令，不能用作别名。"
        aliases = _load()
        if name not in aliases and len(aliases) >= 50:
            return "别名已达上限（50 条），请先 /alias del 清理。"
        try:
            slash = set(json.loads(db.get_state("slash_commands") or "[]"))
        except ValueError:
            slash = set()
        aliases[name] = value
        db.set_state(key, json.dumps(aliases, ensure_ascii=False))
        db.audit("alias_add", f"user={from_user} name={name}")
        note = ""
        if name in BUILTIN_ALIASES:
            note = "（已覆盖内置同名别名）"
        elif name in slash:
            note = f"（注意：与 Claude 命令 /{name} 重名，别名优先）"
        return f"✅ 已定义 /{name} → {' '.join(value.split())[:30]}{note}"

    if op == "del":
        if not rest:
            return "用法：/alias del <名>"
        name = rest.split()[0]
        aliases = _load()
        if name not in aliases:
            return f"没有别名 /{name}（/alias list 查看）。"
        del aliases[name]
        db.set_state(key, json.dumps(aliases, ensure_ascii=False))
        db.audit("alias_del", f"user={from_user} name={name}")
        return f"已删除别名 /{name}。"

    return "用法：/alias add <名> <内容> | /alias del <名> | /alias list"
