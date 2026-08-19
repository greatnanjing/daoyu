"""claude/mcp.json 静态 MCP 清单 schema 测试：三个 server 键存在、stdio 传输、
command/args 非空。清单为平台无关形态（command 直写 npx/uvx），实际拉起形态
由 runner 合并层按平台展开（Windows 包 cmd /c，见 worker/cli_builder.py）。"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MCP_JSON = ROOT / "claude" / "mcp.json"

EXPECTED_SERVERS = ("chrome-devtools", "context7", "web-reader")


def _servers() -> dict:
    return json.loads(MCP_JSON.read_text(encoding="utf-8"))["mcpServers"]


def test_mcp_json_has_three_expected_servers():
    servers = _servers()
    for name in EXPECTED_SERVERS:
        assert name in servers, f"mcp.json 缺 server: {name}"


def test_mcp_servers_are_stdio_with_nonempty_command_and_args():
    servers = _servers()
    assert servers, "mcpServers 不应为空清单"
    for name, entry in servers.items():
        assert isinstance(entry, dict), f"{name} 条目应为对象"
        assert entry.get("type") == "stdio", f"{name} type 应为 stdio"
        cmd = entry.get("command")
        assert isinstance(cmd, str) and cmd, f"{name} command 非空字符串"
        args = entry.get("args")
        assert isinstance(args, list) and args, f"{name} args 非空列表"
        assert all(isinstance(a, str) and a for a in args), f"{name} args 元素均非空字符串"
        env = entry.get("env", {})
        assert isinstance(env, dict), f"{name} env 应为对象（可为空）"


def test_mcp_disabled_key_is_optional_list_of_names():
    raw = json.loads(MCP_JSON.read_text(encoding="utf-8"))
    disabled = raw.get("disabled", [])
    assert isinstance(disabled, list), "disabled 应为 list（缺省视为空）"
    assert all(isinstance(d, str) and d for d in disabled)
