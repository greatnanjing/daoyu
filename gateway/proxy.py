"""代理命令（TUI 交互专属命令的微信文字版，M2 Task 5）。

/permissions 读写 claude/settings.json（刀鱼专属配置——宿主 ~/.claude 已由
CLAUDE_CONFIG_DIR 机制隔离，改的就是刀鱼这份），/mcp 与 /config 只读脱敏；
其余 proxy 命令提示暂未提供。全部 gateway 本地秒回，不经 Claude。写回统一
ensure_ascii=False, indent=2 + 临时文件原子替换，效果等价 TUI、天然可版本化。
"""
import json
import os
import tempfile

PERMISSIONS_USAGE = ("用法：/permissions deny add <规则> | "
                     "/permissions deny del <序号> | /permissions allow add <规则>")
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
            return _mcp(config)
        except NotJsonObjectError as e:
            return f"配置文件格式异常（顶层不是对象）：{e}"
    if cmd == "config":
        try:
            return _config(config)
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


def _save_settings(config, data) -> None:
    """原子写：先写同目录临时文件再 os.replace。截断式 write_text 中途崩溃
    会留半写文件，下次 claude 调用读 settings 失败。"""
    path = _settings_path(config)
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


# ---- /mcp：只读列 claude/mcp.json ----

def _mcp(config) -> str:
    path = config.repo_root / "claude" / "mcp.json"
    if not path.is_file():
        return "未找到 claude/mcp.json。"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as e:
        return f"claude/mcp.json 解析失败：{e}"
    if not isinstance(raw, dict):
        raise NotJsonObjectError(path)
    servers = raw.get("mcpServers") or {}
    if not servers:
        return "claude/mcp.json 中没有配置 MCP server。"
    lines = ["🔌 mcpServers（claude/mcp.json，只读；启停 M3 提供）："]
    for i, (name, svc) in enumerate(servers.items(), 1):
        cmd = svc.get("command", "?") if isinstance(svc, dict) else "?"
        first_arg = f" {svc['args'][0]}" if isinstance(svc, dict) and svc.get("args") else ""
        lines.append(f"  {i}. {name} — {cmd}{first_arg}")
    return "\n".join(lines)


# ---- /config：只读 gateway/config.json（脱敏，不回显任何 secret 值） ----

_THROTTLE_LABELS = (
    ("min_send_interval_s", "发送间隔"),
    ("progress_window_s", "进度窗口"),
    ("page_char_limit", "分页字数"),
    ("daily_send_limit", "日发送上限"),
)


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


def _config(config) -> str:
    path = config.repo_root / "gateway" / "config.json"
    if not path.is_file():
        return "未找到 gateway/config.json。"
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise NotJsonObjectError(path)
    throttle = raw.get("throttle") or {}
    thr = " · ".join(f"{label} {throttle.get(key, '默认')}"
                     for key, label in _THROTTLE_LABELS)
    budget = raw.get("budget") or {}
    n = _secrets_count(config)
    secrets_line = (f"secrets：已配置 {n} 项（claude/secrets.env，值不回显）" if n
                    else "secrets：未配置（claude/secrets.env）")
    return "\n".join([
        "🛠 gateway/config.json（只读，改文件后重启生效）：",
        f"白名单：{len(raw.get('whitelist') or [])} 个账号",
        f"默认目录：{raw.get('default_cwd') or str(config.repo_root)}",
        f"预算：max_turns={budget.get('max_turns', '未设置')} / "
        f"max_usd=${budget.get('max_usd', '未设置')}",
        f"节流：{thr}",
        secrets_line,
        "Claude 实例配置：claude/settings.json · claude/mcp.json",
    ])
