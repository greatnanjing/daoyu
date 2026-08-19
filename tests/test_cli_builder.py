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
        assert "--bare" not in argv   # 2026-08-19：--bare 剥离 WebFetch/WebSearch，已移除
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


# ---- expand_platform：静态 mcp.json 平台无关 → 实际拉起形态 ----

def _svc(command="npx", args=None):
    return {"type": "stdio", "command": command, "args": args or [], "env": {}}


def test_expand_platform_windows_wraps_npx():
    from worker.cli_builder import expand_platform
    servers = {"context7": _svc("npx", ["-y", "@upstash/context7-mcp"]),
               "web-reader": _svc("uvx", ["--with", "mcp~=1.0", "mcp-server-fetch"])}
    out = expand_platform(servers, windows=True)
    assert out["context7"]["command"] == "cmd"
    assert out["context7"]["args"] == ["/c", "npx", "-y", "@upstash/context7-mcp"]
    assert out["web-reader"]["command"] == "cmd"
    assert out["web-reader"]["args"][0] == "/c" and out["web-reader"]["args"][1] == "uvx"


def test_expand_platform_linux_passes_through():
    from worker.cli_builder import expand_platform
    servers = {"context7": _svc("npx", ["x"])}
    out = expand_platform(servers, windows=False)
    assert out["context7"]["command"] == "npx"
    assert out["context7"]["args"] == ["x"]


def test_expand_platform_non_whitelist_command_untouched():
    # sys.executable / 自定义二进制等白名单外命令：两平台都不包装（Windows 也不）
    from worker.cli_builder import expand_platform
    servers = {"daoyu": _svc("C:/venv/Scripts/python.exe", ["worker/approval_mcp.py"])}
    for win in (True, False):
        out = expand_platform(servers, windows=win)
        assert out["daoyu"]["command"] == "C:/venv/Scripts/python.exe", win
        assert out["daoyu"]["args"] == ["worker/approval_mcp.py"], win


def test_expand_platform_does_not_mutate_input():
    # 原始 dict 不被就地修改（调用方是读文件所得，但防御拷贝语义要显式）
    from worker.cli_builder import expand_platform
    servers = {"context7": _svc("npx", ["x"])}
    expand_platform(servers, windows=True)
    assert servers["context7"]["command"] == "npx"
    assert servers["context7"]["args"] == ["x"]


# ---- inject_linux_chrome：Linux 侧 headless Chrome 装配注入 ----

def test_inject_linux_chrome_hits_convention_path(tmp_path):
    # 约定安装形态命中：args 追加 headless/isolated/executablePath，
    # env 清空代理；多版本目录取字典序最高（最新版）
    from worker.cli_builder import inject_linux_chrome
    v151 = tmp_path / ".cache/puppeteer/chrome-headless-shell/linux-151.0.1.2/chrome-headless-shell-linux64"
    v152 = tmp_path / ".cache/puppeteer/chrome-headless-shell/linux-152.0.7977.42/chrome-headless-shell-linux64"
    v151.mkdir(parents=True); v152.mkdir(parents=True)
    (v151 / "chrome-headless-shell").write_bytes(b"")
    (v152 / "chrome-headless-shell").write_bytes(b"")
    servers = {"chrome-devtools": _svc("npx", ["chrome-devtools-mcp@latest"])}
    out = inject_linux_chrome(servers, tmp_path)
    args = out["chrome-devtools"]["args"]
    assert args[:1] == ["chrome-devtools-mcp@latest"]          # 静态 args 保留在前
    assert "--headless" in args and "--isolated" in args
    assert args[args.index("--executablePath") + 1] == str(v152 / "chrome-headless-shell")
    env = out["chrome-devtools"]["env"]
    assert env["http_proxy"] == "" and env["https_proxy"] == ""


def test_inject_linux_chrome_alsa_env(tmp_path):
    # ~/chrome-libs 解包的 libasound 存在时注入 LD_LIBRARY_PATH；
    # 静态 env 的已有键保留（注入值只补不删）
    from worker.cli_builder import inject_linux_chrome
    d = tmp_path / ".cache/puppeteer/chrome-headless-shell/linux-152.0.7977.42/chrome-headless-shell-linux64"
    d.mkdir(parents=True)
    (d / "chrome-headless-shell").write_bytes(b"")
    alsa = tmp_path / "chrome-libs/usr/lib64"
    alsa.mkdir(parents=True)
    (alsa / "libasound.so.2").write_bytes(b"")
    servers = {"chrome-devtools": {**_svc("npx"), "env": {"FOO": "bar"}}}
    env = inject_linux_chrome(servers, tmp_path)["chrome-devtools"]["env"]
    assert env["LD_LIBRARY_PATH"] == str(alsa)
    assert env["FOO"] == "bar"


def test_inject_linux_chrome_no_chrome_noop(tmp_path):
    # 未安装（约定路径缺席）：条目原样返回（fail-open）
    from worker.cli_builder import inject_linux_chrome
    servers = {"chrome-devtools": _svc("npx", ["chrome-devtools-mcp@latest"]),
               "context7": _svc("npx", ["-y", "ctx"])}
    out = inject_linux_chrome(servers, tmp_path)
    assert out is servers or out == servers
    assert out["chrome-devtools"]["args"] == ["chrome-devtools-mcp@latest"]


def test_inject_linux_chrome_missing_entry_or_bad_shape(tmp_path):
    # 条目缺席 / 非 dict 形态：不炸、不改其他条目
    from worker.cli_builder import inject_linux_chrome
    d = tmp_path / ".cache/puppeteer/chrome-headless-shell/linux-152.0.0.0/chrome-headless-shell-linux64"
    d.mkdir(parents=True)
    (d / "chrome-headless-shell").write_bytes(b"")
    out = inject_linux_chrome({"context7": _svc("npx")}, tmp_path)
    assert "chrome-devtools" not in out
    out2 = inject_linux_chrome({"chrome-devtools": "not-a-dict"}, tmp_path)
    assert out2["chrome-devtools"] == "not-a-dict"


def test_inject_linux_chrome_does_not_mutate_input(tmp_path):
    from worker.cli_builder import inject_linux_chrome
    d = tmp_path / ".cache/puppeteer/chrome-headless-shell/linux-152.0.0.0/chrome-headless-shell-linux64"
    d.mkdir(parents=True)
    (d / "chrome-headless-shell").write_bytes(b"")
    servers = {"chrome-devtools": _svc("npx", ["chrome-devtools-mcp@latest"])}
    inject_linux_chrome(servers, tmp_path)
    assert servers["chrome-devtools"]["args"] == ["chrome-devtools-mcp@latest"]
    assert servers["chrome-devtools"]["env"] == {}
