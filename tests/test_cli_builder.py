from pathlib import Path

from common.models import Budget
from worker.cli_builder import BYPASS_DISALLOWED_TOOLS, build_argv


def test_new_session_uses_session_id():
    argv = build_argv(session_uuid="U", resume=False, policy="auto",
                      budget=Budget(), mcp_config=None, settings=None)
    assert argv[0] == "-p"
    assert "--session-id" in argv and argv[argv.index("--session-id") + 1] == "U"
    assert "--resume" not in argv


def test_resume_session():
    argv = build_argv(session_uuid="U", resume=True, policy="auto",
                      budget=Budget(), mcp_config=None, settings=None)
    assert "--resume" in argv and argv[argv.index("--resume") + 1] == "U"
    assert "--session-id" not in argv


def test_policy_mode_mapping():
    def mode_for(policy):
        argv = build_argv(session_uuid="U", resume=True, policy=policy,
                          budget=Budget(), mcp_config=None, settings=None)
        return argv[argv.index("--permission-mode") + 1]
    assert mode_for("auto") == "acceptEdits"
    # C2：实测（m2-final-review 探针 6）acceptEdits 下不触发 permission-prompt-tool
    # （Bash 直接放行），default 档才触发——strict 必须用 default。
    assert mode_for("strict") == "default"
    assert mode_for("bypass") == "bypassPermissions"
    assert mode_for("plan") == "plan"


def test_bypass_adds_disallowed_tools_fallback():
    argv = build_argv(session_uuid="U", resume=True, policy="bypass",
                      budget=Budget(), mcp_config=None, settings=None)
    assert "--disallowedTools" in argv   # deny 清单在 bypass 下是否生效未实测，工具级兜底恒加
    argv2 = build_argv(session_uuid="U", resume=True, policy="auto",
                       budget=Budget(), mcp_config=None, settings=None)
    assert "--disallowedTools" not in argv2


def test_budget_and_stream_flags_always_present():
    for policy in ("auto", "strict", "bypass", "plan"):
        argv = build_argv(session_uuid="U", resume=True, policy=policy,
                          budget=Budget(max_turns=30, max_usd=1.5),
                          mcp_config=Path("/mcp.json"), settings=Path("/settings.json"))
        assert argv[argv.index("--max-turns") + 1] == "30"
        assert argv[argv.index("--max-budget-usd") + 1] == "1.5"
        assert "--bare" in argv
        assert argv[argv.index("--output-format") + 1] == "stream-json"
        assert "--verbose" in argv
        assert "--include-partial-messages" in argv
        assert argv[argv.index("--mcp-config") + 1] == str(Path("/mcp.json"))
        assert argv[argv.index("--settings") + 1] == str(Path("/settings.json"))
        assert "--strict-mcp-config" in argv


def test_strict_adds_approval_prompt_tool():
    # 三方向：strict+approval_mcp=True 加 flag；非 strict 不加；strict 但
    # approval_mcp=False（无审批 server 场景）不加。
    argv = build_argv(session_uuid="U", resume=True, policy="strict",
                      budget=Budget(), mcp_config=None, settings=None,
                      approval_mcp=True)
    i = argv.index("--permission-prompt-tool")
    assert argv[i + 1] == "mcp__daoyu__approve"
    argv2 = build_argv(session_uuid="U", resume=True, policy="auto",
                       budget=Budget(), mcp_config=None, settings=None,
                       approval_mcp=True)
    assert "--permission-prompt-tool" not in argv2
    argv3 = build_argv(session_uuid="U", resume=True, policy="strict",
                       budget=Budget(), mcp_config=None, settings=None,
                       approval_mcp=False)
    assert "--permission-prompt-tool" not in argv3


def test_bypass_disallowed_tools_use_absolute_anchor():
    # I-2 回归：--disallowedTools 是 CLI flag，官方 permissions 文档规定 Read/Edit
    # 单前导 / 锚定 original cwd（会话目录可被 /cd 切走）→ 系统路径必须 // 绝对
    # 锚定；Bash 兜底两项与 claude/settings.json deny 清单对齐，防回退。
    assert "Read(//etc/**)" in BYPASS_DISALLOWED_TOOLS
    assert "Edit(//etc/**)" in BYPASS_DISALLOWED_TOOLS
    assert "Edit(//**/data/daoyu.db)" in BYPASS_DISALLOWED_TOOLS
    assert "Read(~/.ssh/**)" in BYPASS_DISALLOWED_TOOLS
    assert "Edit(~/.claude/**)" in BYPASS_DISALLOWED_TOOLS
    assert "Bash(rm -rf /*)" in BYPASS_DISALLOWED_TOOLS
    assert "Bash(rm -rf ~)" in BYPASS_DISALLOWED_TOOLS
    # 旧形态（单 / 锚定 settings 来源目录、cwd 相对、未定义形态）不得复活
    assert "Edit(/)" not in BYPASS_DISALLOWED_TOOLS
    assert "Read(/etc/**)" not in BYPASS_DISALLOWED_TOOLS
    assert "Edit(/etc/**)" not in BYPASS_DISALLOWED_TOOLS
    assert "Edit(./data/**)" not in BYPASS_DISALLOWED_TOOLS
