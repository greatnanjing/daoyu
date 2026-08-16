from gateway.router import route


def test_plain_text_is_chat():
    r = route("帮我看下这个 bug", set())
    assert r.kind == "chat"


def test_bridge_commands():
    for cmd, args in [("cancel", ""), ("tasks", ""), ("/status", ""), ("cd", "/repo"),
                      ("policy", "strict"), ("sessions", "")]:
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
