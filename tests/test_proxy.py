"""M2 Task 5: 配置代理命令文字版（/permissions 读写 /mcp /config 只读）。"""
import json

from gateway.app import handle_inbound
from gateway.proxy import execute_proxy
from gateway.router import Route


class FakeCfg:
    """execute_proxy 只依赖 repo_root；handle_inbound 另需 whitelist。"""

    def __init__(self, repo_root, whitelist=None):
        self.repo_root = repo_root
        self.whitelist = whitelist if whitelist is not None else {"u@im.wechat"}


def _route(cmd, args=""):
    return Route(kind="proxy", command=cmd, args=args, detail={})


def _write_settings(root, perms):
    (root / "claude").mkdir(exist_ok=True)
    (root / "claude" / "settings.json").write_text(
        json.dumps({"permissions": perms}, ensure_ascii=False, indent=2),
        encoding="utf-8")


def _read_settings(root):
    return json.loads(
        (root / "claude" / "settings.json").read_text(encoding="utf-8"))


def _audit_details(db, kind):
    return [r["detail"] for r in db._conn.execute(
        "SELECT detail FROM audit_log WHERE kind=?", (kind,))]


def _outbox_texts(db):
    return [r["text"] for r in db._conn.execute("SELECT text FROM outbox")]


PERMS = {
    "deny": ["Read(//etc/**)", "Read(~/.ssh/**)"],
    "allow": ["Bash(git:*)"],
    "ask": [],
}


# ---- /permissions 只读列表 ----

async def test_permissions_lists_three_sections(db, tmp_path):
    _write_settings(tmp_path, PERMS)
    reply = await execute_proxy(db, _route("permissions"), FakeCfg(tmp_path))
    assert "⚙️ permissions" in reply
    assert "deny:" in reply
    assert "  1. Read(//etc/**)" in reply
    assert "  2. Read(~/.ssh/**)" in reply
    assert "  1. Bash(git:*)" in reply     # allow 非空也带序号
    assert "ask:（空）" in reply
    assert "用法：/permissions" in reply


async def test_permissions_missing_settings_file(db, tmp_path):
    reply = await execute_proxy(db, _route("permissions"), FakeCfg(tmp_path))
    assert "未找到 claude/settings.json" in reply


async def test_permissions_bad_json(db, tmp_path):
    (tmp_path / "claude").mkdir()
    (tmp_path / "claude" / "settings.json").write_text("{oops", encoding="utf-8")
    reply = await execute_proxy(db, _route("permissions"), FakeCfg(tmp_path))
    assert "解析失败" in reply


async def test_settings_top_level_not_object(db, tmp_path):
    # 顶层为合法非对象 JSON（数组）：回复格式异常提示而非 AttributeError 逃逸
    (tmp_path / "claude").mkdir()
    (tmp_path / "claude" / "settings.json").write_text("[]", encoding="utf-8")
    reply = await execute_proxy(db, _route("permissions"), FakeCfg(tmp_path))
    assert "配置文件格式异常" in reply and "顶层不是对象" in reply
    assert "settings.json" in reply


# ---- /permissions deny add / allow add ----

async def test_permissions_deny_add_roundtrip(db, tmp_path):
    _write_settings(tmp_path, {"deny": ["Read(//etc/**)"], "allow": [], "ask": []})
    reply = await execute_proxy(db, _route("permissions", "deny add Edit(//tmp/**)"),
                                FakeCfg(tmp_path))
    assert "已添加" in reply and "下次调用生效" in reply
    data = _read_settings(tmp_path)
    assert data["permissions"]["deny"] == ["Read(//etc/**)", "Edit(//tmp/**)"]
    assert data["permissions"]["allow"] == []          # 其他节不受影响
    details = _audit_details(db, "config_change")
    assert len(details) == 1 and "deny add" in details[0] \
        and "Edit(//tmp/**)" in details[0]


async def test_permissions_allow_add(db, tmp_path):
    _write_settings(tmp_path, {"deny": [], "allow": [], "ask": []})
    reply = await execute_proxy(db, _route("permissions", "allow add WebSearch"),
                                FakeCfg(tmp_path))
    assert "已添加" in reply
    assert _read_settings(tmp_path)["permissions"]["allow"] == ["WebSearch"]


async def test_permissions_deny_add_duplicate_rejected(db, tmp_path):
    _write_settings(tmp_path, {"deny": ["Read(//etc/**)"], "allow": [], "ask": []})
    reply = await execute_proxy(db, _route("permissions", "deny add Read(//etc/**)"),
                                FakeCfg(tmp_path))
    assert "已存在" in reply
    assert _read_settings(tmp_path)["permissions"]["deny"] == ["Read(//etc/**)"]


async def test_permissions_add_creates_missing_file(db, tmp_path):
    # 服务器上经微信无法手建文件：add 应自动创建 claude/settings.json
    reply = await execute_proxy(db, _route("permissions", "deny add Bash(curl:*)"),
                                FakeCfg(tmp_path))
    assert "已添加" in reply
    assert _read_settings(tmp_path)["permissions"]["deny"] == ["Bash(curl:*)"]


async def test_permissions_add_empty_rule_shows_usage(db, tmp_path):
    _write_settings(tmp_path, PERMS)
    for args in ("deny add", "deny add   ", "allow add"):
        reply = await execute_proxy(db, _route("permissions", args), FakeCfg(tmp_path))
        assert "用法" in reply, args


async def test_permissions_unknown_subcommand(db, tmp_path):
    _write_settings(tmp_path, PERMS)
    reply = await execute_proxy(db, _route("permissions", "deny remove 1"),
                                FakeCfg(tmp_path))
    assert "用法" in reply


# ---- /permissions deny del ----

async def test_permissions_deny_del_by_index(db, tmp_path):
    _write_settings(tmp_path, {"deny": ["Read(//etc/**)", "Bash(rm:*)"],
                               "allow": [], "ask": []})
    reply = await execute_proxy(db, _route("permissions", "deny del 1"),
                                FakeCfg(tmp_path))
    assert "已删除" in reply and "Read(//etc/**)" in reply
    assert _read_settings(tmp_path)["permissions"]["deny"] == ["Bash(rm:*)"]
    assert len(_audit_details(db, "config_change")) == 1


async def test_permissions_deny_del_out_of_range(db, tmp_path):
    _write_settings(tmp_path, PERMS)   # deny 2 条
    reply = await execute_proxy(db, _route("permissions", "deny del 5"),
                                FakeCfg(tmp_path))
    assert "越界" in reply and "共 2 条" in reply
    assert len(_read_settings(tmp_path)["permissions"]["deny"]) == 2   # 文件未动


async def test_permissions_deny_del_non_numeric(db, tmp_path):
    _write_settings(tmp_path, PERMS)
    reply = await execute_proxy(db, _route("permissions", "deny del abc"),
                                FakeCfg(tmp_path))
    assert "用法" in reply


# ---- /mcp 只读 ----

async def test_mcp_lists_servers(db, tmp_path):
    (tmp_path / "claude").mkdir()
    (tmp_path / "claude" / "mcp.json").write_text(json.dumps({
        "mcpServers": {
            "chrome-devtools": {"type": "stdio", "command": "cmd",
                                "args": ["/c", "npx"], "env": {}},
            "fetch": {"type": "stdio", "command": "uvx", "args": ["mcp-server-fetch"]},
        }}, ensure_ascii=False), encoding="utf-8")
    reply = await execute_proxy(db, _route("mcp"), FakeCfg(tmp_path))
    assert "chrome-devtools" in reply and "fetch" in reply
    assert "cmd /c" in reply          # server 名 + command 首参数
    assert "uvx mcp-server-fetch" in reply


async def test_mcp_missing_file(db, tmp_path):
    reply = await execute_proxy(db, _route("mcp"), FakeCfg(tmp_path))
    assert "未找到 claude/mcp.json" in reply


async def test_mcp_top_level_not_object(db, tmp_path):
    (tmp_path / "claude").mkdir()
    (tmp_path / "claude" / "mcp.json").write_text("[]", encoding="utf-8")
    reply = await execute_proxy(db, _route("mcp"), FakeCfg(tmp_path))
    assert "配置文件格式异常" in reply and "mcp.json" in reply


# ---- /config 只读脱敏 ----

def _write_gateway_config(root):
    (root / "gateway").mkdir(exist_ok=True)
    (root / "gateway" / "config.json").write_text(json.dumps({
        "whitelist": ["u@im.wechat"],
        "default_cwd": "/srv/proj",
        "budget": {"max_turns": 50, "max_usd": 5.0},
        "throttle": {"min_send_interval_s": 1.0, "progress_window_s": 2.5,
                     "page_char_limit": 2000, "daily_send_limit": 500},
    }, ensure_ascii=False), encoding="utf-8")


async def test_config_overview_redacts_secrets(db, tmp_path):
    _write_gateway_config(tmp_path)
    (tmp_path / "claude").mkdir(exist_ok=True)
    (tmp_path / "claude" / "secrets.env").write_text(
        "ANTHROPIC_API_KEY=sk-supersecret\nTENCENT_KEY=tct-hidden\n",
        encoding="utf-8")
    reply = await execute_proxy(db, _route("config"), FakeCfg(tmp_path))
    assert "白名单：1 个" in reply
    assert "/srv/proj" in reply
    assert "max_turns=50" in reply and "max_usd=$5.0" in reply
    assert "2000" in reply and "500" in reply
    assert "已配置 2 项" in reply
    # 脱敏红线：任何 secret 值不得出现在输出
    assert "sk-" not in reply
    assert "supersecret" not in reply and "tct-hidden" not in reply


async def test_config_missing_file(db, tmp_path):
    reply = await execute_proxy(db, _route("config"), FakeCfg(tmp_path))
    assert "未找到 gateway/config.json" in reply


async def test_config_top_level_not_object(db, tmp_path):
    (tmp_path / "gateway").mkdir()
    (tmp_path / "gateway" / "config.json").write_text("[]", encoding="utf-8")
    reply = await execute_proxy(db, _route("config"), FakeCfg(tmp_path))
    assert "配置文件格式异常" in reply and "config.json" in reply


async def test_config_default_cwd_null_falls_back(db, tmp_path):
    # default_cwd 显式为 null 时回退 repo_root，而不是打印 "None"
    (tmp_path / "gateway").mkdir()
    (tmp_path / "gateway" / "config.json").write_text(
        '{"default_cwd": null}', encoding="utf-8")
    reply = await execute_proxy(db, _route("config"), FakeCfg(tmp_path))
    assert "None" not in reply
    assert str(tmp_path) in reply


# ---- 其余 proxy 命令 ----

async def test_unimplemented_proxy_commands(db, tmp_path):
    cfg = FakeCfg(tmp_path)
    for cmd in ("hooks", "plugins", "login", "vim", "terminal-setup"):
        reply = await execute_proxy(db, _route(cmd), cfg)
        assert f"/{cmd} 的微信代理版暂未提供" in reply, cmd


# ---- handle_inbound 接线 ----

async def test_handle_inbound_proxy_branch_replies(db, tmp_path):
    msg = {"message_type": 1, "from_user_id": "u@im.wechat", "message_id": "m1",
           "context_token": "tok",
           "item_list": [{"text_item": {"text": "/permissions"}}]}
    await handle_inbound(db, FakeCfg(tmp_path), None, None, msg)
    texts = _outbox_texts(db)
    assert len(texts) == 1
    assert "未找到 claude/settings.json" in texts[0]   # 真执行了 execute_proxy
