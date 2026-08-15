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
