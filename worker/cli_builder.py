"""claude CLI 命令行组装（TRD §4.1）。每次调用全量传 flag（--resume 不恢复权限/MCP 配置）。
prompt 一律走 stdin，不进 argv（避免 shell 转义问题）。"""
from pathlib import Path

from common.models import Budget

POLICY_MODE = {
    "auto": "acceptEdits",
    "strict": "acceptEdits",        # 审批 MCP 为 M2 项；M1 strict 与 auto 同基线
    "bypass": "bypassPermissions",
    "plan": "plan",
}

# bypass 档工具级兜底（bypass 下 permissions.deny 生效性未实测，TRD §8 要求叠加）。
# 与 claude/settings.json 的 deny 清单逐项对齐。路径用 // 绝对锚定：官方 permissions
# 文档规定 Read/Edit 单前导 / 锚定到规则来源目录（--settings <file> → 该文件所在目录，
# CLI flag → original cwd，且会话 cwd 可被 /cd 切走），不锚定文件系统根；
# //**/x 匹配文件系统任意位置的同名路径（文档明确记载的形态）。
BYPASS_DISALLOWED_TOOLS = [
    "Read(//etc/**)", "Read(~/.ssh/**)", "Read(~/.claude/**)",
    "Edit(//etc/**)", "Edit(~/.ssh/**)", "Edit(~/.claude/**)",
    "Edit(//**/data/daoyu.db)", "Bash(rm -rf /*)", "Bash(rm -rf ~)",
]


def build_argv(*, session_uuid: str, resume: bool, policy: str, budget: Budget,
               mcp_config: Path | None, settings: Path | None) -> list[str]:
    argv = ["-p"]
    argv += ["--resume", session_uuid] if resume else ["--session-id", session_uuid]
    argv += ["--permission-mode", POLICY_MODE[policy]]
    if policy == "bypass":
        argv += ["--disallowedTools", ",".join(BYPASS_DISALLOWED_TOOLS)]
    argv += ["--bare"]
    argv += ["--max-turns", str(budget.max_turns)]
    argv += ["--max-budget-usd", str(budget.max_usd)]
    if mcp_config is not None:
        argv += ["--mcp-config", str(mcp_config), "--strict-mcp-config"]
    if settings is not None:
        argv += ["--settings", str(settings)]
    argv += ["--output-format", "stream-json", "--verbose",
             "--include-partial-messages"]
    return argv
