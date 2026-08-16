"""claude/mcp.json 静态 MCP 清单 schema 测试：三个 server 键存在、stdio 传输、
command/args 非空。仅校验结构（命令能否真拉起属真机实测，见 task-4-report）。
清单的 command 是 Windows 形态（cmd /c npx …）；Linux 部署时改为 npx/uvx 直连、
去掉 /c 前缀（README 部署节注明），schema 断言两种形态都兼容。"""
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
