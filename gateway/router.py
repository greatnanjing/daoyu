"""统一命令总线路由（TRD §5）。顺序：桥命令 → iLink 运维 → 转发（headless 可用集）→ 代理 → 未知。"""
import difflib
from dataclasses import dataclass

BRIDGE_COMMANDS = {"cancel", "tasks", "status", "cd", "sessions", "policy", "bg"}
ILINK_COMMANDS = {"time", "重新连接", "help"}
# TUI 交互专属（静态维护，官方 commands 文档），M2 提供完整代理实现
PROXY_COMMANDS = {"permissions", "hooks", "plugins", "login", "config", "mcp",
                  "vim", "terminal-setup"}


@dataclass
class Route:
    kind: str            # chat / bridge / ilink / forward / proxy / unknown
    command: str | None
    args: str
    detail: dict


def _closest(name: str, candidates: set[str]) -> str | None:
    # sorted：平分候选时结果确定（set 迭代序受 PYTHONHASHSEED 影响，跨进程会漂移）
    matches = difflib.get_close_matches(name, sorted(candidates), n=1, cutoff=0.6)
    return matches[0] if matches else None


def route(text: str, slash_commands: set[str]) -> Route:
    stripped = text.strip()
    if not stripped.startswith("/"):
        return Route(kind="chat", command=None, args=stripped, detail={})

    body = stripped[1:]
    parts = body.split(None, 1)
    name = parts[0] if parts else ""
    args = parts[1] if len(parts) > 1 else ""
    if not name:
        return Route(kind="unknown", command=None, args="命令为空",
                     detail={"suggestion": None})

    if name in BRIDGE_COMMANDS:
        return Route(kind="bridge", command=name, args=args, detail={})
    if name in ILINK_COMMANDS:
        return Route(kind="ilink", command=name, args=args, detail={})
    if name in slash_commands:
        return Route(kind="forward", command=name, args=args, detail={})
    if name in PROXY_COMMANDS:
        return Route(kind="proxy", command=name, args=args, detail={})

    pool = BRIDGE_COMMANDS | ILINK_COMMANDS | PROXY_COMMANDS | set(slash_commands)
    suggestion = _closest(name, pool)
    # brief 参考实现仅把建议放 detail，但其测试断言 "review" in r.args —— 测试为准：
    # unknown 时 args 放人类可读提示（与裸斜杠分支 "命令为空" 同一先例），建议仍留 detail。
    msg = f"未知命令 /{name}，最接近：/{suggestion}" if suggestion else f"未知命令 /{name}"
    return Route(kind="unknown", command=name, args=msg,
                 detail={"suggestion": suggestion})
