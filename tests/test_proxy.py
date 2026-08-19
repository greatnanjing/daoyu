"""配置代理命令文字版（/permissions 读写 /mcp 列表+启停 /config 概览+set）。"""
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


async def test_permissions_add_atomic_no_tmp_leftover(db, tmp_path):
    """M4：写 settings.json 走临时文件 + os.replace——目录里不留 .tmp 残留，
    且目标文件内容完整（无半写窗口）。"""
    _write_settings(tmp_path, {"deny": [], "allow": [], "ask": []})
    reply = await execute_proxy(db, _route("permissions", "deny add Edit(//tmp/**)"),
                                FakeCfg(tmp_path))
    assert "已添加" in reply
    assert list((tmp_path / "claude").glob("*.tmp")) == []       # 无临时残留
    assert _read_settings(tmp_path)["permissions"]["deny"] == ["Edit(//tmp/**)"]


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


# ---- /mcp 列表 ----

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


async def test_mcp_bad_json(db, tmp_path):
    # 语法级坏 JSON（截断）：JSONDecodeError 经 execute_proxy 兜底为
    # 解析失败文案，而非异常逃逸到 poll_loop（审查 Minor-6a）
    (tmp_path / "claude").mkdir()
    (tmp_path / "claude" / "mcp.json").write_text('{"mcpServers": ', encoding="utf-8")
    reply = await execute_proxy(db, _route("mcp"), FakeCfg(tmp_path))
    assert "claude/mcp.json 解析失败" in reply


# ---- /config 概览（脱敏） ----

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
    # 概览标签附短键名：与 /config set 的英文键名可对照（真机验收 UX 修复）
    assert "分页字数(page_char_limit)" in reply
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


# ---- /mcp on/off 启停 ----

def _write_mcp(root, servers, disabled=None):
    (root / "claude").mkdir(exist_ok=True)
    doc = {"mcpServers": servers}
    if disabled is not None:
        doc["disabled"] = disabled
    (root / "claude" / "mcp.json").write_text(
        json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_mcp(root):
    return json.loads(
        (root / "claude" / "mcp.json").read_text(encoding="utf-8"))


_MCP_SRV = {
    "chrome-devtools": {"type": "stdio", "command": "npx",
                         "args": ["chrome-devtools-mcp@latest"], "env": {}},
    "web-reader": {"type": "stdio", "command": "uvx",
                    "args": ["mcp-server-fetch"], "env": {}},
}


async def test_mcp_off_by_name_then_on_by_index(db, tmp_path):
    _write_mcp(tmp_path, _MCP_SRV)
    reply = await execute_proxy(db, _route("mcp", "off web-reader"), FakeCfg(tmp_path))
    assert "已停用" in reply and "下一任务生效" in reply
    doc = _read_mcp(tmp_path)
    assert doc["disabled"] == ["web-reader"]
    assert doc["mcpServers"]["web-reader"]      # 条目保留不丢
    assert len(_audit_details(db, "config_change")) == 1

    reply = await execute_proxy(db, _route("mcp", "on 2"), FakeCfg(tmp_path))
    assert "已启用" in reply
    assert _read_mcp(tmp_path)["disabled"] == []
    # 审查 Minor-6c：on 路径 audit detail 断言（off 已有）——detail 记解析后
    # 的实际名字（"mcp on web-reader"），而非用户输入原文 "on 2"
    assert _audit_details(db, "config_change") == [
        "mcp off web-reader", "mcp on web-reader"]


async def test_mcp_off_by_index(db, tmp_path):
    _write_mcp(tmp_path, _MCP_SRV)
    reply = await execute_proxy(db, _route("mcp", "off 1"), FakeCfg(tmp_path))
    assert "已停用" in reply and "chrome-devtools" in reply
    assert _read_mcp(tmp_path)["disabled"] == ["chrome-devtools"]


async def test_mcp_off_duplicate_idempotent_hint(db, tmp_path):
    _write_mcp(tmp_path, _MCP_SRV, disabled=["web-reader"])
    reply = await execute_proxy(db, _route("mcp", "off web-reader"), FakeCfg(tmp_path))
    assert "已是停用" in reply
    assert _read_mcp(tmp_path)["disabled"] == ["web-reader"]   # 文件未动


async def test_mcp_on_not_disabled_hint(db, tmp_path):
    _write_mcp(tmp_path, _MCP_SRV)
    reply = await execute_proxy(db, _route("mcp", "on chrome-devtools"), FakeCfg(tmp_path))
    assert "已处于启用" in reply


async def test_mcp_off_unknown_target(db, tmp_path):
    _write_mcp(tmp_path, _MCP_SRV)
    reply = await execute_proxy(db, _route("mcp", "off ghost"), FakeCfg(tmp_path))
    assert "没有这个 server" in reply and "chrome-devtools" in reply   # 提示当前清单


async def test_mcp_off_index_out_of_range(db, tmp_path):
    _write_mcp(tmp_path, _MCP_SRV)
    reply = await execute_proxy(db, _route("mcp", "off 9"), FakeCfg(tmp_path))
    assert "越界" in reply and "共 2" in reply
    assert "disabled" not in _read_mcp(tmp_path)     # 文件未动（无 disabled 键）


async def test_mcp_off_missing_target_shows_usage(db, tmp_path):
    _write_mcp(tmp_path, _MCP_SRV)
    for args in ("off", "on", "toggle web-reader", "off 1 2"):
        reply = await execute_proxy(db, _route("mcp", args), FakeCfg(tmp_path))
        assert "用法" in reply, args


async def test_mcp_list_marks_disabled(db, tmp_path):
    _write_mcp(tmp_path, _MCP_SRV, disabled=["web-reader"])
    reply = await execute_proxy(db, _route("mcp"), FakeCfg(tmp_path))
    assert "⛔" in reply and "✅" in reply
    assert reply.index("chrome-devtools") < reply.index("web-reader")


async def test_mcp_off_atomic_no_tmp_leftover(db, tmp_path):
    _write_mcp(tmp_path, _MCP_SRV)
    await execute_proxy(db, _route("mcp", "off web-reader"), FakeCfg(tmp_path))
    assert list((tmp_path / "claude").glob("*.tmp")) == []


async def test_mcp_list_shows_system_entries(db, tmp_path):
    """余项 B：/mcp 列表呈现系统条目（恒装载，不受 on/off 管辖）。"""
    _write_mcp(tmp_path, _MCP_SRV)
    reply = await execute_proxy(db, _route("mcp"), FakeCfg(tmp_path))
    assert "系统条目（恒装载）：daoyu（审批/发图）· daoyu-ocr（本地 OCR）" in reply


async def test_mcp_list_system_entries_when_no_static_servers(db, tmp_path):
    _write_mcp(tmp_path, {})
    reply = await execute_proxy(db, _route("mcp"), FakeCfg(tmp_path))
    assert "daoyu-ocr（本地 OCR）" in reply


# ---- /config set 白名单写入 ----

def _read_gateway_config(root):
    return json.loads(
        (root / "gateway" / "config.json").read_text(encoding="utf-8"))


async def test_config_set_all_seven_keys(db, tmp_path):
    _write_gateway_config(tmp_path)
    cases = [
        ("throttle.min_send_interval_s", "0.5", 0.5),
        ("throttle.progress_window_s", "3", 3.0),
        ("throttle.page_char_limit", "1500", 1500),
        ("throttle.daily_send_limit", "300", 300),
        ("budget.max_turns", "30", 30),
        ("budget.max_usd", "2.5", 2.5),
        ("worker.concurrency", "2", 2),
    ]
    for key, val, expect in cases:
        reply = await execute_proxy(db, _route("config", f"set {key} {val}"),
                                    FakeCfg(tmp_path))
        assert "已写入" in reply and "重启生效" in reply, key
    doc = _read_gateway_config(tmp_path)
    assert doc["throttle"]["min_send_interval_s"] == 0.5
    assert doc["throttle"]["progress_window_s"] == 3.0
    assert doc["throttle"]["page_char_limit"] == 1500
    assert doc["throttle"]["daily_send_limit"] == 300
    assert doc["budget"]["max_turns"] == 30
    assert doc["budget"]["max_usd"] == 2.5
    assert doc["worker"]["concurrency"] == 2
    # 白名单外原样保留
    assert doc["whitelist"] == ["u@im.wechat"]
    assert doc["default_cwd"] == "/srv/proj"
    assert len(_audit_details(db, "config_change")) == 7


async def test_config_set_creates_missing_section(db, tmp_path):
    # worker 节在原文件缺失 → set 自动建节，其余键保留
    (tmp_path / "gateway").mkdir()
    (tmp_path / "gateway" / "config.json").write_text(
        '{"whitelist": ["u@im.wechat"]}', encoding="utf-8")
    reply = await execute_proxy(
        db, _route("config", "set worker.concurrency 4"), FakeCfg(tmp_path))
    assert "已写入" in reply
    doc = _read_gateway_config(tmp_path)
    assert doc["worker"]["concurrency"] == 4
    assert doc["whitelist"] == ["u@im.wechat"]


async def test_config_set_rejects_non_whitelist_key(db, tmp_path):
    _write_gateway_config(tmp_path)
    for key in ("whitelist", "claude_bin", "reconnect.session_duration_s",
                "default_cwd"):
        reply = await execute_proxy(db, _route("config", f"set {key} 1"),
                                    FakeCfg(tmp_path))
        assert "不开放" in reply and "直接改 gateway/config.json" in reply, key
    assert "whitelist" in _read_gateway_config(tmp_path)   # 文件未动


async def test_config_set_rejects_bad_type(db, tmp_path):
    _write_gateway_config(tmp_path)
    reply = await execute_proxy(
        db, _route("config", "set budget.max_turns 1.5"), FakeCfg(tmp_path))
    assert "整数" in reply
    reply = await execute_proxy(
        db, _route("config", "set throttle.min_send_interval_s fast"), FakeCfg(tmp_path))
    assert "数值" in reply


async def test_config_set_rejects_out_of_range(db, tmp_path):
    _write_gateway_config(tmp_path)
    bad = [("throttle.min_send_interval_s", "0"),        # > 0
           ("throttle.progress_window_s", "-1"),
           ("throttle.page_char_limit", "199"),          # ≥ 200
           ("throttle.daily_send_limit", "0"),           # ≥ 1
           ("budget.max_turns", "0"),                    # ≥ 1
           ("budget.max_usd", "0"),
           ("worker.concurrency", "11"),                 # 1~10
           ("worker.concurrency", "0")]
    for key, val in bad:
        reply = await execute_proxy(db, _route("config", f"set {key} {val}"),
                                    FakeCfg(tmp_path))
        assert "范围" in reply, (key, val)
        assert _read_gateway_config(tmp_path)["budget"]["max_turns"] == 50  # 未动


async def test_config_set_bad_usage(db, tmp_path):
    _write_gateway_config(tmp_path)
    for args in ("set", "set throttle.page_char_limit", "bump x y"):
        reply = await execute_proxy(db, _route("config", args), FakeCfg(tmp_path))
        assert "用法" in reply, args


async def test_config_set_atomic_no_tmp_leftover(db, tmp_path):
    _write_gateway_config(tmp_path)
    await execute_proxy(
        db, _route("config", "set worker.concurrency 2"), FakeCfg(tmp_path))
    assert list((tmp_path / "gateway").glob("*.tmp")) == []


async def test_config_set_rejects_weird_numerics(db, tmp_path):
    """I1/I2：--3/² 在类型层拦（不误报文件损坏）；inf/1e999 拒（预算闸安全底线）。"""
    _write_gateway_config(tmp_path)
    weird_int = [("budget.max_turns", "--3"), ("budget.max_turns", "²"),
                 ("budget.max_turns", "1.5")]
    for key, val in weird_int:
        reply = await execute_proxy(db, _route("config", f"set {key} {val}"),
                                    FakeCfg(tmp_path))
        assert "整数" in reply and "解析失败" not in reply, (key, val)
    for key, val in [("budget.max_usd", "inf"), ("budget.max_usd", "1e999"),
                     ("budget.max_usd", "+infinity")]:
        reply = await execute_proxy(db, _route("config", f"set {key} {val}"),
                                    FakeCfg(tmp_path))
        assert "不是有限数值" in reply, (key, val)
    assert _read_gateway_config(tmp_path)["budget"] == {"max_turns": 50,
                                                        "max_usd": 5.0}


async def test_config_set_rejects_non_object_section(db, tmp_path):
    """M1：目标节存在但不是对象 → 明确报错，不改文件。"""
    (tmp_path / "gateway").mkdir()
    (tmp_path / "gateway" / "config.json").write_text(
        json.dumps({"whitelist": ["u@im.wechat"], "worker": "x"}),
        encoding="utf-8")
    reply = await execute_proxy(
        db, _route("config", "set worker.concurrency 2"), FakeCfg(tmp_path))
    assert "不是对象" in reply and "改 gateway/config.json" in reply
    assert _read_gateway_config(tmp_path)["worker"] == "x"
