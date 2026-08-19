"""代理命令（TUI 交互专属命令的微信文字版，M2 Task 5）。

/permissions 读写 claude/settings.json（刀鱼专属配置——宿主 ~/.claude 已由
CLAUDE_CONFIG_DIR 机制隔离，改的就是刀鱼这份），/mcp 列表 + on/off 启停（顶层
disabled 标记，下一任务生效），/config 概览 + set 白名单写入（脱敏）；其余 proxy 命令提示暂未提供。
全部 gateway 本地秒回，不经 Claude。写回统一
ensure_ascii=False, indent=2 + 临时文件原子替换，效果等价 TUI、天然可版本化。
"""
import json
import math
import os
import tempfile

PERMISSIONS_USAGE = ("用法：/permissions deny add <规则> | "
                     "/permissions deny del <序号> | /permissions allow add <规则>")
MCP_USAGE = "用法：/mcp — 列表；/mcp off <序号|名字> 停用；/mcp on <序号|名字> 启用"
CONFIG_USAGE = ("用法：/config — 概览；/config set <键> <值>（可改键："
                "throttle.min_send_interval_s/progress_window_s/"
                "page_char_limit/daily_send_limit、budget.max_turns/max_usd、"
                "worker.concurrency；重启生效）")
_SCOPES = ("deny", "allow")


class NotJsonObjectError(ValueError):
    """文件是合法 JSON 但顶层不是对象（数组/字符串等）。str(e) 为文件路径。"""


async def execute_proxy(db, route, config) -> str:
    cmd = route.command
    if cmd == "permissions":
        try:
            return _permissions(db, config, route.args.strip())
        except NotJsonObjectError as e:
            return f"配置文件格式异常（顶层不是对象）：{e}"
        except ValueError as e:
            return f"claude/settings.json 解析失败：{e}"
    if cmd == "mcp":
        try:
            return _mcp(db, config, route.args.strip())
        except NotJsonObjectError as e:
            return f"配置文件格式异常（顶层不是对象）：{e}"
        except ValueError as e:
            return f"claude/mcp.json 解析失败：{e}"
    if cmd == "config":
        try:
            reply = _config(config, route.args.strip())
            if reply.startswith("已写入"):
                db.audit("config_change",
                         f"config set {'='.join(route.args.split()[1:3])}")
            return reply
        except NotJsonObjectError as e:
            return f"配置文件格式异常（顶层不是对象）：{e}"
        except ValueError as e:
            return f"gateway/config.json 解析失败：{e}"
    return f"/{cmd} 的微信代理版暂未提供。"


# ---- /permissions：读写 claude/settings.json ----

def _settings_path(config):
    return config.repo_root / "claude" / "settings.json"


def _load_settings(config):
    """读 settings.json 整体（保留其他顶层键）；文件缺失返回 None。"""
    path = _settings_path(config)
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise NotJsonObjectError(path)
    return data


def _perm_lists(data) -> dict:
    """就地补齐 allow/deny/ask 三列表（缺失或类型不对按空表处理）。"""
    perms = data.setdefault("permissions", {})
    if not isinstance(perms, dict):
        raise ValueError("permissions 不是对象")
    for k in ("allow", "deny", "ask"):
        if not isinstance(perms.get(k), list):
            perms[k] = []
    return perms


def _atomic_write_json(path, data) -> None:
    """原子写 JSON：同目录临时文件 + os.replace。截断式 write_text 中途崩溃
    会留半写文件，下次读取方（claude / gateway 启动）读失败。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _save_settings(config, data) -> None:
    """原子写 claude/settings.json（见 _atomic_write_json）。"""
    _atomic_write_json(_settings_path(config), data)


def _fmt_rules(name: str, rules: list) -> str:
    if not rules:
        return f"{name}:（空）"
    return "\n".join([f"{name}:"] +
                     [f"  {i}. {r}" for i, r in enumerate(rules, 1)])


def _permissions(db, config, args: str) -> str:
    data = _load_settings(config)
    if not args:   # 只读列表
        if data is None:
            return "未找到 claude/settings.json。"
        perms = _perm_lists(data)
        return "\n".join([
            "⚙️ permissions（claude/settings.json，下次调用生效）：",
            _fmt_rules("deny", perms["deny"]),
            _fmt_rules("allow", perms["allow"]),
            _fmt_rules("ask", perms["ask"]),
            PERMISSIONS_USAGE,
        ])

    parts = args.split(None, 2)
    if len(parts) < 2 or parts[0] not in _SCOPES or parts[1] not in ("add", "del"):
        return PERMISSIONS_USAGE
    scope, op = parts[0], parts[1]
    rest = parts[2].strip() if len(parts) > 2 else ""

    if op == "add":
        if not rest:
            return PERMISSIONS_USAGE
        data = data if data is not None else {}
        rules = _perm_lists(data)[scope]
        if rest in rules:
            return f"已存在 {scope} 规则：{rest}（无需重复添加）"
        rules.append(rest)
        _save_settings(config, data)
        db.audit("config_change", f"permissions {scope} add: {rest}")
        return f"已添加 {scope}：{rest}，下次调用生效。"

    # del <序号>（1-based，与列表显示一致）
    if data is None:
        return "未找到 claude/settings.json。"
    if not (rest.isascii() and rest.isdigit()):   # isdigit 会放行 int() 拒绝的字符（如 ²）
        return PERMISSIONS_USAGE
    n = int(rest)
    rules = _perm_lists(data)[scope]
    if not 1 <= n <= len(rules):
        return f"序号越界：{scope} 当前共 {len(rules)} 条。"
    removed = rules.pop(n - 1)
    _save_settings(config, data)
    db.audit("config_change", f"permissions {scope} del #{n}: {removed}")
    return f"已删除 {scope} 第 {n} 条（{removed}），下次调用生效。"


# ---- /mcp：列 claude/mcp.json + on/off 启停（顶层 disabled 标记）----

def _load_mcp(config):
    """读 mcp.json；返回 (path, raw dict)。文件缺失返回 (path, None)。"""
    path = config.repo_root / "claude" / "mcp.json"
    if not path.is_file():
        return path, None
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise NotJsonObjectError(path)
    return path, raw


def _mcp(db, config, args: str) -> str:
    path, raw = _load_mcp(config)
    if raw is None:
        return "未找到 claude/mcp.json。"
    servers = raw.get("mcpServers") or {}
    disabled = raw.get("disabled")
    disabled = disabled if isinstance(disabled, list) else []

    parts = args.split()
    if parts and parts[0] in ("on", "off"):
        if len(parts) != 2:
            return MCP_USAGE
        return _mcp_toggle(db, path, raw, servers, disabled,
                           parts[0], parts[1])

    if not servers:
        return "claude/mcp.json 中没有配置 MCP server。"
    lines = ["🔌 mcpServers（claude/mcp.json；启停下一任务生效）："]
    for i, (name, svc) in enumerate(servers.items(), 1):
        cmd = svc.get("command", "?") if isinstance(svc, dict) else "?"
        first_arg = f" {svc['args'][0]}" if isinstance(svc, dict) and svc.get("args") else ""
        mark = "⛔" if name in disabled else "✅"
        lines.append(f"  {i}. {name} {mark} — {cmd}{first_arg}")
    lines.append(MCP_USAGE)
    return "\n".join(lines)


def _mcp_toggle(db, path, raw, servers, disabled, op, target) -> str:
    """on/off 单个 server：名字精确匹配优先，否则 1-based 序号（与列表一致）。"""
    name = None
    if target in servers:
        name = target
    elif target.isascii() and target.isdigit():
        n = int(target)
        keys = list(servers)
        if 1 <= n <= len(keys):
            name = keys[n - 1]
        else:
            return f"序号越界：共 {len(keys)} 个 server。"
    if name is None:
        return (f"没有这个 server：{target}（当前：{', '.join(servers) or '（空）'}）")

    if op == "off":
        if name in disabled:
            return f"{name} 已是停用状态。"
        disabled = [*disabled, name]
        raw["disabled"] = disabled
        _atomic_write_json(path, raw)
        db.audit("config_change", f"mcp off {name}")
        return f"已停用 {name}，下一任务生效（配置保留，/mcp on {name} 可再启）。"
    # on
    if name not in disabled:
        return f"{name} 已处于启用状态。"
    disabled = [d for d in disabled if d != name]
    raw["disabled"] = disabled      # 空数组也留键（与静态 mcp.json 初始形态一致）
    _atomic_write_json(path, raw)
    db.audit("config_change", f"mcp on {name}")
    return f"已启用 {name}，下一任务生效。"


# ---- /config：概览 + set 白名单写入 gateway/config.json（脱敏，不回显任何 secret 值） ----

_THROTTLE_LABELS = (
    ("min_send_interval_s", "发送间隔"),
    ("progress_window_s", "进度窗口"),
    ("page_char_limit", "分页字数"),
    ("daily_send_limit", "日发送上限"),
)

# /config set 白名单：key -> (解析器, 校验器, 类型名)。范围外的键拒绝（whitelist
# 从微信改 = 放别人进服务器，安全不开放——其余提示改文件）。
def _is_int(s: str) -> bool:
    t = s[1:] if s[:1] == "-" else s
    return s.isascii() and t.isdigit()


def _is_float(s: str) -> bool:
    try:
        float(s)
        return True
    except ValueError:
        return False


CONFIG_KEYS = {
    "throttle.min_send_interval_s": (float, lambda v: v > 0, "数值"),
    "throttle.progress_window_s": (float, lambda v: v > 0, "数值"),
    "throttle.page_char_limit": (int, lambda v: v >= 200, "整数"),
    "throttle.daily_send_limit": (int, lambda v: v >= 1, "整数"),
    "budget.max_turns": (int, lambda v: v >= 1, "整数"),
    "budget.max_usd": (float, lambda v: v > 0, "数值"),
    "worker.concurrency": (int, lambda v: 1 <= v <= 10, "整数"),
}


def _secrets_count(config) -> int:
    path = config.repo_root / "claude" / "secrets.env"
    if not path.is_file():
        return 0
    n = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line \
                and line.split("=", 1)[1].strip():
            n += 1   # 只数有值的键，空值不算已配置
    return n


def _config(config, args: str) -> str:
    path = config.repo_root / "gateway" / "config.json"
    if not path.is_file():
        return "未找到 gateway/config.json。"
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise NotJsonObjectError(path)

    parts = args.split()
    if parts:
        if parts[0] != "set":
            return f"未知子命令：{parts[0]}\n{CONFIG_USAGE}"
        return _config_set(path, raw, parts[1:])

    # 概览（现状不变，仅标题改写 + 尾行加用法）
    throttle = raw.get("throttle") or {}
    thr = " · ".join(f"{label} {throttle.get(key, '默认')}"
                     for key, label in _THROTTLE_LABELS)
    budget = raw.get("budget") or {}
    n = _secrets_count(config)
    secrets_line = (f"secrets：已配置 {n} 项（claude/secrets.env，值不回显）" if n
                    else "secrets：未配置（claude/secrets.env）")
    return "\n".join([
        "🛠 gateway/config.json（概览；set 可改常用键，重启生效）：",
        f"白名单：{len(raw.get('whitelist') or [])} 个账号",
        f"默认目录：{raw.get('default_cwd') or str(config.repo_root)}",
        f"预算：max_turns={budget.get('max_turns', '未设置')} / "
        f"max_usd=${budget.get('max_usd', '未设置')}",
        f"节流：{thr}",
        secrets_line,
        "Claude 实例配置：claude/settings.json · claude/mcp.json",
        CONFIG_USAGE,
    ])


def _config_set(path, raw, rest) -> str:
    """set <键> <值>：白名单 + 类型 + 范围校验，读原文改键整体原子写回。
    成功时回执以「已写入」开头——execute_proxy 据此记 audit。"""
    if len(rest) != 2:
        return CONFIG_USAGE
    key, val = rest
    spec = CONFIG_KEYS.get(key)
    if spec is None:
        return (f"键 {key} 不开放微信修改，请直接改 gateway/config.json"
                f"（可改键见 /config 用法行）")
    parser, check, type_name = spec
    if not (_is_int(val) if parser is int else _is_float(val)):
        return f"值 {val} 不是合法{type_name}。"
    v = parser(val)
    if parser is float and not math.isfinite(v):
        return f"值 {val} 不是有限数值。"
    if not check(v):
        return f"值 {v} 超出允许范围（{key} 的合法范围见 /config 用法行与文档）。"

    section, _, leaf = key.partition(".")
    raw.setdefault(section, {})
    if not isinstance(raw[section], dict):
        return f"配置节 {section} 不是对象，请直接改 gateway/config.json。"
    raw[section][leaf] = v
    _atomic_write_json(path, raw)
    return (f"已写入 {key}={v}，重启生效（systemctl restart daoyu）。"
            f"当前运行中的旧值继续使用。")
