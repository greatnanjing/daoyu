from gateway.router import route


def test_plain_text_is_chat():
    r = route("帮我看下这个 bug", set())
    assert r.kind == "chat"


def test_bridge_commands():
    for cmd, args in [("cancel", ""), ("tasks", ""), ("/status", ""), ("cd", "/repo"),
                      ("policy", "strict"), ("sessions", ""), ("/new", ""),
                      ("adopt", "abc12345"), ("delete", "#2"), ("delete", "task 3")]:
        text = cmd if cmd.startswith("/") else f"/{cmd} {args}".strip()
        r = route(text, set())
        assert r.kind == "bridge", text
    r = route("/policy strict", set())
    assert r.command == "policy" and r.args == "strict"


def test_ilink_ops():
    assert route("/time", set()).kind == "ilink"
    assert route("/重新连接", set()).kind == "ilink"
    assert route("/help", set()).kind == "ilink"


def test_known_slash_command_forwards():
    r = route("/review", {"review", "model", "compact"})
    assert r.kind == "forward" and r.command == "review"


def test_proxy_tui_commands():
    for c in ("permissions", "hooks", "plugins", "login", "config", "mcp"):
        r = route(f"/{c}", set())
        assert r.kind == "proxy", c


def test_proxy_beats_forward_when_in_slash_commands():
    # I1：实测 claude 2.1.233 的 init slash_commands 含 config/mcp——代理必须
    # 先于转发判定，否则 /mcp /config 被截走原样发给 headless claude，proxy
    # 实现生产不可达。
    for c in ("config", "mcp"):
        r = route(f"/{c}", {"config", "mcp", "review"})
        assert r.kind == "proxy", c


def test_unknown_gets_suggestion():
    r = route("/revie w", {"review"})
    assert r.kind == "unknown"
    assert "review" in r.args


def test_forward_with_args_kept_verbatim():
    r = route("/model sonnet", {"model"})
    assert r.kind == "forward" and r.args == "sonnet"


def test_bare_slash_treated_as_unknown():
    r = route("/", set())
    assert r.kind == "unknown"


# ---------------- M5C3：内置短别名 ----------------

def test_builtin_aliases():
    for short, full in [("t", "tasks"), ("s", "status"),
                        ("c", "cancel"), ("cs", "sessions")]:
        r = route(f"/{short}", set())
        assert r.kind == "bridge" and r.command == full, short


def test_builtin_alias_args_follow():
    r = route("/c 5", set())
    assert r.kind == "bridge" and r.command == "cancel" and r.args == "5"


def test_builtin_alias_beats_slash_commands():
    # 内置映射先于动态 slash 清单：claude 若也暴露 /t 命令不遮蔽内置别名
    r = route("/t", {"t", "tasks"})
    assert r.kind == "bridge" and r.command == "tasks"


def test_builtin_alias_target_not_overridden():
    # 映射目标必须仍是合法桥命令（防常量改错后掉进 unknown）
    from gateway.router import BUILTIN_ALIASES, BRIDGE_COMMANDS
    assert set(BUILTIN_ALIASES.values()) <= BRIDGE_COMMANDS
