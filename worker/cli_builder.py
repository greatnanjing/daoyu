"""claude CLI 命令行组装（TRD §4.1）。每次调用全量传 flag（--resume 不恢复权限/MCP 配置）。
prompt 一律走 stdin，不进 argv（避免 shell 转义问题）。"""
from pathlib import Path

from common.models import Budget

POLICY_MODE = {
    "auto": "acceptEdits",
    # strict = default + 审批 MCP（--permission-prompt-tool）。实测（claude 2.1.233，
    # 干净 CLAUDE_CONFIG_DIR 环境）：acceptEdits 下 Bash 等直接放行、不触发
    # prompt-tool，default 档才触发审批——TRD §4.1 "strict=acceptEdits" 假设已被推翻。
    "strict": "default",
    "bypass": "bypassPermissions",
    "plan": "plan",
}


def claude_config_dir(repo_root) -> str:
    """刀鱼 Claude 实例的 CLAUDE_CONFIG_DIR（自建即 mkdir，幂等）。

    实测（m2-final-review 探针 1-5）：--bare 与 --settings 均不能隔离宿主 ~/.claude
    （宿主 defaultMode/allow/trustAllFiles/插件全部穿透生效，直接架空 strict 审批与
    硬 deny 清单）；只有重定向 config 目录才是机制化隔离。凭据不受影响：仍经
    secrets env 注入（ANTHROPIC_API_KEY 等）；MCP 清单经 --mcp-config 显式传。"""
    d = Path(repo_root) / "data" / "claude-home"
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


# strict 档审批 server 键（runner 临时 mcp config 的 mcpServers 键）与工具引用。
# 引用格式 mcp__<server 键>__<工具名>，键名原样透传：键与引用不一致时 Claude 找不到
# 该工具 → 无审批通道 → 该次工具调用被 deny（fail-safe，TRD §4.4）。
APPROVAL_MCP_SERVER = "daoyu"
APPROVAL_PROMPT_TOOL = f"mcp__{APPROVAL_MCP_SERVER}__approve"

# 能力面 server 键（runner 临时 mcp config 恒注入，不受 /mcp on/off 管辖——
# 同 APPROVAL_MCP_SERVER 一样是系统条目，但无 DB/env 依赖）。
OCR_MCP_SERVER = "daoyu-ocr"
OCR_TOOL = f"mcp__{OCR_MCP_SERVER}__ocr"   # claude/settings.json allow 引用同一形态

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
               mcp_config: Path | None, settings: Path | None,
               approval_mcp: bool = False,
               fork_session: bool = False) -> list[str]:
    argv = ["-p"]
    if resume:
        argv += ["--resume", session_uuid]
        if fork_session:
            # 会话仍被 bg daemon 持有时 --resume 会报错（实测 2.1.233："Session
            # ... is currently running as a background agent"）→ fork 副本接续，
            # 原条目不受影响。pool 对 blocked 条目取结果即用此形态。
            argv += ["--fork-session"]
    else:
        argv += ["--session-id", session_uuid]
    argv += ["--permission-mode", POLICY_MODE[policy]]
    if policy == "strict" and approval_mcp:
        argv += ["--permission-prompt-tool", APPROVAL_PROMPT_TOOL]
    if policy == "bypass":
        argv += ["--disallowedTools", ",".join(BYPASS_DISALLOWED_TOOLS)]
    # 不带 --bare（2026-08-19 实测：--bare 会剥离 WebFetch/WebSearch/Write/Glob/
    # Grep 等全部扩展工具，只留 Bash/Edit/Read+MCP——智谱端点下模型彻底丧失
    # 联网搜索能力。去掉后 WebSearch 经智谱服务端适配（web_search_prime）实测
    # 可用；WebFetch 因抓取前的 claude.ai 域名验证在国内服务器不可达而失败，
    # 模型会自行 fallback 到 web-reader MCP。代价仅系统提示词变长（减载收益
    # 让位于能力面），配置隔离仍由 CLAUDE_CONFIG_DIR 机制承担（与 --bare 无关）。
    argv += ["--max-turns", str(budget.max_turns)]
    argv += ["--max-budget-usd", str(budget.max_usd)]
    if mcp_config is not None:
        argv += ["--mcp-config", str(mcp_config), "--strict-mcp-config"]
    if settings is not None:
        argv += ["--settings", str(settings)]
    argv += ["--output-format", "stream-json", "--verbose",
             "--include-partial-messages"]
    return argv


# 静态 mcp.json 平台无关命令的 Windows 包装白名单：npm 系命令在 Windows 是
# .cmd shim，asyncio create_subprocess_exec 直启会 FileNotFoundError → 包
# cmd /c。Linux 直传。白名单外（sys.executable 等绝对路径）两平台都直传。
_WINDOWS_WRAP = {"npx", "uvx"}


def expand_platform(servers: dict, windows: bool) -> dict:
    """静态 mcpServers → 实际拉起形态（纯函数，平台由参数传入可测）。
    windows=False 时原样返回传入对象（调用方当只读）；仅 windows=True 分支
    对白名单条目浅拷贝改写、其余条目原引用。非 dict 条目原样透传（防御坏文件）。"""
    if not windows:
        return servers
    out = {}
    for name, svc in servers.items():
        if isinstance(svc, dict) and svc.get("command") in _WINDOWS_WRAP:
            svc = {**svc, "command": "cmd",
                   "args": ["/c", svc["command"], *svc.get("args", [])]}
        out[name] = svc
    return out


# Linux headless Chrome 自动发现的约定安装形态（@puppeteer/browsers npmmirror
# 安装产物）：~/.cache/puppeteer/chrome-headless-shell/linux-<ver>/chrome-headless-
# shell-linux64/chrome-headless-shell。服务器无桌面环境，chrome-devtools-mcp
# 必须显式 --headless + --executablePath 才能拉起该二进制。
_LINUX_CHROME_GLOB = ("chrome-headless-shell", "linux-*", "chrome-headless-shell-linux64",
                      "chrome-headless-shell")
# Chrome 152 在 OpenCloudOS 9 上唯一缺失的系统库（headless 壳仍链接 ALSA）；
# 免 sudo 方案 = rpm 解包到 ~/chrome-libs/usr/lib64，注入 LD_LIBRARY_PATH。
_LINUX_ALSA_REL = Path("chrome-libs") / "usr" / "lib64" / "libasound.so.2"


def inject_linux_chrome(servers: dict, home: Path) -> dict:
    """Linux 侧 chrome-devtools 条目的本机装配注入（纯函数，home 参数化可测）。

    命中约定路径的 headless Chrome 时给 chrome-devtools 条目追加
    --headless --isolated --executablePath，并按需注入 LD_LIBRARY_PATH
    （~/chrome-libs 解包的 libasound 存在时）；同时显式清空条目 env 的
    http(s)_proxy——服务器 shell 的死代理配置会穿透给 Chrome 导致所有导航
    失败（systemd 生产环境本就干净，防御手跑排障场景）。条目 env 与静态条目
    env 合并（注入值优先），args 追加在静态 args 之后（CLI 后值覆盖前值）。
    未安装（约定路径缺失）或条目缺席/坏形态时原样返回——fail-open，与
    expand_platform 的防御口径一致。Windows 由调用方分支排除，不会走到。"""
    chrome = None
    base = home / ".cache" / "puppeteer"
    if base.is_dir():
        cands = sorted(base.glob(str(Path(*_LINUX_CHROME_GLOB))))
        if cands:
            chrome = cands[-1]   # 版本目录字典序 = 最高版本
    svc = servers.get("chrome-devtools")
    if chrome is None or not isinstance(svc, dict):
        return servers
    env = dict(svc.get("env") or {})
    alsa = home / _LINUX_ALSA_REL
    if alsa.exists():
        env["LD_LIBRARY_PATH"] = str(alsa.parent) + (
            ":" + env["LD_LIBRARY_PATH"] if env.get("LD_LIBRARY_PATH") else "")
    env.setdefault("http_proxy", "")
    env.setdefault("https_proxy", "")
    servers = {**servers, "chrome-devtools": {
        **svc,
        "args": [*svc.get("args", []), "--headless", "--isolated",
                 "--executablePath", str(chrome)],
        "env": env,
    }}
    return servers
