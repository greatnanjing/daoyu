from pathlib import Path

from common.models import Budget
from worker.cli_builder import build_argv


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
    assert mode_for("strict") == "acceptEdits"
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
