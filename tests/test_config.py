"""config 加载测试：从 tmp 目录读 gateway/config.json + claude/secrets.env，断言字段映射。"""
import json

import pytest

from common.config import load_config
from common.models import Budget


def _write_config(tmp_path, raw):
    # exist_ok：同一测试内可多次覆写 config（notify 默认+合并测试写两次）
    (tmp_path / "gateway").mkdir(exist_ok=True)
    (tmp_path / "gateway" / "config.json").write_text(
        json.dumps(raw, ensure_ascii=False), encoding="utf-8")


def test_load_config_maps_fields(tmp_path):
    _write_config(tmp_path, {
        "whitelist": ["u@im.wechat"],
        "default_cwd": "/repo",
        "claude_bin": ["node", "/opt/claude.js"],
        "throttle": {"min_send_interval_s": 2.0, "progress_window_s": 3.0,
                     "page_char_limit": 1500, "daily_send_limit": 300},
        "budget": {"max_turns": 30, "max_usd": 2.5},
        "worker": {"concurrency": 2, "poll_interval_s": 0.2},
        "reconnect": {"session_duration_s": 43200, "warning_before_s": 3600},
    })
    (tmp_path / "claude").mkdir()
    (tmp_path / "claude" / "secrets.env").write_text(
        "# 注释行\n"
        "ANTHROPIC_API_KEY=sk-test\n"
        "EMPTY_VALUE=\n"          # 值为空也要保留键
        "NO_EQUALS_LINE\n"        # 无 = 的行跳过
        "  SPACED = padded  \n",  # 键值去空白
        encoding="utf-8")

    cfg = load_config(tmp_path)

    assert cfg.repo_root == tmp_path
    assert cfg.db_path == tmp_path / "data" / "daoyu.db"
    assert cfg.whitelist == {"u@im.wechat"}
    assert cfg.default_cwd == "/repo"
    assert cfg.claude_bin == ["node", "/opt/claude.js"]
    assert cfg.throttle == {"min_send_interval_s": 2.0, "progress_window_s": 3.0,
                            "page_char_limit": 1500, "daily_send_limit": 300}
    assert cfg.budget == Budget(max_turns=30, max_usd=2.5)
    assert cfg.worker == {"concurrency": 2, "poll_interval_s": 0.2,
                          "bg_poll_s": 10, "bg_blocked_timeout_s": 1800}
    assert cfg.reconnect["session_duration_s"] == 43200
    assert cfg.reconnect["warning_before_s"] == 3600
    assert cfg.secrets == {"ANTHROPIC_API_KEY": "sk-test", "EMPTY_VALUE": "",
                           "SPACED": "padded"}


def test_load_config_defaults_fill_contract_keys(tmp_path):
    # 空 config：全部走默认；secrets.env 缺失 → 空 dict。
    # throttle 四键是 TaskRunner/OutboundLoop 的运行期契约，缺键会在循环里 KeyError。
    _write_config(tmp_path, {})
    cfg = load_config(tmp_path)

    assert cfg.whitelist == set()
    assert cfg.default_cwd == str(tmp_path)
    assert cfg.claude_bin == "claude"
    assert cfg.budget == Budget()
    for k in ("min_send_interval_s", "progress_window_s",
              "page_char_limit", "daily_send_limit"):
        assert k in cfg.throttle, k
    assert cfg.worker["concurrency"] == 3
    assert cfg.reconnect["session_duration_s"] == 2592000   # 30 天（token 长效实证）
    assert cfg.secrets == {}


def test_load_config_partial_throttle_merged(tmp_path):
    # 实例只覆盖部分节流键：其余键保留默认（合并而非整体替换）
    _write_config(tmp_path, {"throttle": {"page_char_limit": 800}})
    cfg = load_config(tmp_path)
    assert cfg.throttle["page_char_limit"] == 800
    assert cfg.throttle["progress_window_s"] == 2.5


def test_load_config_notify_defaults_and_merge(tmp_path):
    # M5A：notify 节默认 + 部分覆盖合并（不整体替换）
    _write_config(tmp_path, {})
    cfg = load_config(tmp_path)
    assert cfg.notify == {"listen": "127.0.0.1:8417", "http_enabled": True}
    _write_config(tmp_path, {"notify": {"listen": "127.0.0.1:9000"}})
    cfg = load_config(tmp_path)
    assert cfg.notify["listen"] == "127.0.0.1:9000"
    assert cfg.notify["http_enabled"] is True


def test_load_config_missing_file_exits(tmp_path):
    with pytest.raises(SystemExit):
        load_config(tmp_path)


def test_load_config_budget_unknown_key_exits_with_hint(tmp_path):
    # M-4：budget 未知键原本是裸 TypeError（启动崩且不知来源），
    # 现在 SystemExit 指明 config.json 来源与合法键名。
    _write_config(tmp_path, {"budget": {"max_turns": 10, "budget_usd": 1.0}})
    with pytest.raises(SystemExit, match="budget"):
        load_config(tmp_path)


# ---- host_claude_env / merge_claude_secrets：宿主 settings.json 动态凭据层 ----

def test_host_claude_env_whitelist(tmp_path):
    from common.config import host_claude_env
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude/settings.json").write_text(json.dumps({
        "env": {"ANTHROPIC_AUTH_TOKEN": "t1", "ANTHROPIC_BASE_URL": "https://x",
                "ANTHROPIC_DEFAULT_OPUS_MODEL": "m1", "API_TIMEOUT_MS": "9000",
                "DISABLE_TELEMETRY": "1"},          # 非白名单：不取
        "permissions": {"defaultMode": "auto"},     # 非 env：不取
    }), encoding="utf-8")
    env = host_claude_env(tmp_path)
    assert env == {"ANTHROPIC_AUTH_TOKEN": "t1", "ANTHROPIC_BASE_URL": "https://x",
                   "ANTHROPIC_DEFAULT_OPUS_MODEL": "m1", "API_TIMEOUT_MS": "9000"}


def test_host_claude_env_missing_or_bad(tmp_path):
    from common.config import host_claude_env
    assert host_claude_env(tmp_path) == {}                       # 无文件
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude/settings.json").write_text("{bad json", encoding="utf-8")
    assert host_claude_env(tmp_path) == {}                       # 坏 JSON
    (tmp_path / ".claude/settings.json").write_text("{}", encoding="utf-8")
    assert host_claude_env(tmp_path) == {}                       # 无 env 块


def test_merge_claude_secrets_dedup_and_priority():
    from common.config import merge_claude_secrets
    fb = {"ANTHROPIC_API_KEY": "old", "ANTHROPIC_BASE_URL": "old-url",
          "OTHER": "keep"}
    # 动态层出 AUTH_TOKEN → 兜底层 API_KEY 剔除（防双 key 双头）
    out = merge_claude_secrets(fb, {"ANTHROPIC_AUTH_TOKEN": "new"})
    assert "ANTHROPIC_API_KEY" not in out and out["ANTHROPIC_AUTH_TOKEN"] == "new"
    # 反向同理；动态层逐键覆盖（BASE_URL 换新）
    out2 = merge_claude_secrets(fb, {"ANTHROPIC_API_KEY": "k2",
                                     "ANTHROPIC_BASE_URL": "new-url"})
    assert "ANTHROPIC_AUTH_TOKEN" not in out2 and out2["ANTHROPIC_API_KEY"] == "k2"
    assert out2["ANTHROPIC_BASE_URL"] == "new-url" and out2["OTHER"] == "keep"
    # 动态层为空 → 兜底层原样
    assert merge_claude_secrets(fb, {}) == fb
