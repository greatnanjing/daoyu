"""config 加载测试：从 tmp 目录读 gateway/config.json + claude/secrets.env，断言字段映射。"""
import json

import pytest

from common.config import load_config
from common.models import Budget


def _write_config(tmp_path, raw):
    (tmp_path / "gateway").mkdir()
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
    assert cfg.worker == {"concurrency": 2, "poll_interval_s": 0.2}
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
    assert cfg.reconnect["session_duration_s"] == 86400
    assert cfg.secrets == {}


def test_load_config_partial_throttle_merged(tmp_path):
    # 实例只覆盖部分节流键：其余键保留默认（合并而非整体替换）
    _write_config(tmp_path, {"throttle": {"page_char_limit": 800}})
    cfg = load_config(tmp_path)
    assert cfg.throttle["page_char_limit"] == 800
    assert cfg.throttle["progress_window_s"] == 2.5


def test_load_config_missing_file_exits(tmp_path):
    with pytest.raises(SystemExit):
        load_config(tmp_path)


def test_load_config_budget_unknown_key_exits_with_hint(tmp_path):
    # M-4：budget 未知键原本是裸 TypeError（启动崩且不知来源），
    # 现在 SystemExit 指明 config.json 来源与合法键名。
    _write_config(tmp_path, {"budget": {"max_turns": 10, "budget_usd": 1.0}})
    with pytest.raises(SystemExit, match="budget"):
        load_config(tmp_path)
