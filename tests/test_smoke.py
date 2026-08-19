def test_packages_importable():
    import common
    import gateway
    import worker


def test_claude_config_files_exist():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    assert (root / "claude" / "settings.json").is_file()
    assert (root / "claude" / "mcp.json").is_file()
    assert (root / "gateway" / "config.example.json").is_file()


def test_claude_deny_rules_absolute_anchored():
    # I-2 回归：deny 清单必须 // 绝对锚定——官方 permissions 文档规定 Read/Edit
    # 单前导 / 锚定 settings 来源目录（--settings <file> → 该文件所在目录）而非
    # 文件系统根；`Edit(/)` 是四种合法形态之外的未定义模式；//**/x 匹配文件
    # 系统任意位置（文档记载形态）。json.loads 同时校验配置文件合法性。
    import json
    from pathlib import Path
    from worker.cli_builder import BYPASS_DISALLOWED_TOOLS
    root = Path(__file__).resolve().parents[1]
    deny = json.loads((root / "claude" / "settings.json").read_text(
        encoding="utf-8"))["permissions"]["deny"]
    assert "Read(//etc/**)" in deny and "Edit(//etc/**)" in deny
    assert "Edit(//**/data/daoyu.db)" in deny
    assert "Read(~/.ssh/**)" in deny and "Edit(~/.claude/**)" in deny
    assert "Bash(rm -rf /*)" in deny and "Bash(rm -rf ~)" in deny
    assert "Edit(/)" not in deny                       # 未定义形态，不得回退
    assert "Read(/etc/**)" not in deny and "Edit(/etc/**)" not in deny
    assert "Edit(./data/daoyu.db)" not in deny         # cwd 相对锚定不随 /cd 走
    # bypass 档工具级兜底与 deny 清单是同一防线，两清单不得漂移
    assert set(BYPASS_DISALLOWED_TOOLS) == set(deny)


def test_claude_allow_rules_cover_daoyu_send_image():
    # M3 真机验收（2026-08-19）回归：send_image 是 MCP 工具，acceptEdits 只放行
    # 文件编辑、不放行 MCP 调用——headless 无确认通道时直接 deny。不在 allow
    # 清单 = auto/bypass/plan 档发图全挂。approve 不进 allow：它是 prompt-tool
    # 走审批通道，Claude 自主调用 approve 应被拒（fail-safe）。
    import json
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    perms = json.loads((root / "claude" / "settings.json").read_text(
        encoding="utf-8"))["permissions"]
    assert "mcp__daoyu__send_image" in perms.get("allow", [])
    assert not any("approve" in r for r in perms.get("allow", []))
