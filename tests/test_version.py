"""claude CLI 版本探测（TRD §11 版本漂移对策机制化）测试。

fake_claude.py 的 --version 分支输出真实 CLI 形态（实测 "2.1.233 (Claude Code)"），
FAKE_CLAUDE_VERSION env 注入可模拟匹配/漂移两分支。
"""
import sys
from pathlib import Path

import pytest

from worker.cli_builder import wrap_windows_command
from worker.version import (EXPECTED_CLAUDE_VERSION, check_claude_version,
                            parse_claude_version, probe_claude_version)

FAKE = str(Path(__file__).parent / "fixtures" / "fake_claude.py")


class FakeCfg:
    def __init__(self, claude_bin):
        self.claude_bin = claude_bin


def _audits(db):
    return [(r["kind"], r["detail"])
            for r in db._conn.execute("SELECT kind, detail FROM audit_log")]


# ---- 解析与包装（纯函数）----

def test_parse_version_forms():
    assert parse_claude_version("2.1.233 (Claude Code)\n") == "2.1.233"
    assert parse_claude_version("1.2.3") == "1.2.3"
    assert parse_claude_version("no version here") is None
    assert parse_claude_version("") is None


def test_wrap_windows_command():
    assert wrap_windows_command(["C:/x/claude.cmd"]) == ["cmd", "/c", "C:/x/claude.cmd"]
    assert wrap_windows_command(["C:/x/claude.CMD", "--version"]) == \
        ["cmd", "/c", "C:/x/claude.CMD", "--version"]
    assert wrap_windows_command(["C:/x/claude.bat"]) == ["cmd", "/c", "C:/x/claude.bat"]
    # Linux 路径 / 裸名 / 空前缀原样（后缀判断天然平台无关，Linux 上 .cmd 不出现）
    assert wrap_windows_command(["/usr/bin/claude"]) == ["/usr/bin/claude"]
    assert wrap_windows_command(["claude"]) == ["claude"]
    assert wrap_windows_command([]) == []


@pytest.mark.skipif(sys.platform != "win32",
                    reason="npm shim 解析是 Windows 专属行为（.cmd 内 %dp0% 反斜杠"
                           "路径仅 Windows 解析直达 exe；Linux 走 cmd /c 回退）")
def test_wrap_windows_command_resolves_npm_shim_exe(tmp_path):
    """npm shim 解析：.cmd 内 "%dp0%\\...\\x.exe" 指向真实 exe 时直达
    （cmd /c 对含空格引号路径有剥引号切分的静默失败坑，exe 直达是首选）。"""
    exe = tmp_path / "node_modules" / "pkg" / "bin" / "claude.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    shim = tmp_path / "claude.cmd"
    shim.write_text('@ECHO off\n"%dp0%\\node_modules\\pkg\\bin\\claude.exe"   %*\n',
                    encoding="utf-8")
    assert wrap_windows_command([str(shim), "--version"]) == [str(exe), "--version"]
    # shim 指向的 exe 不存在（坏 shim）→ 回退 cmd /c
    exe.unlink()
    assert wrap_windows_command([str(shim)]) == ["cmd", "/c", str(shim)]


# ---- probe（真子进程）----

def test_probe_with_fake_claude_default_version():
    assert probe_claude_version([sys.executable, FAKE]) == EXPECTED_CLAUDE_VERSION


def test_probe_failure_fail_open():
    # 脚本不存在：rc≠0 → None（fail-open，不抛）
    assert probe_claude_version([sys.executable, "no-such-file-xyz.py"]) is None


# ---- check 三分支（audit 留痕）----

async def test_check_version_match_audits_ok(db):
    await check_claude_version(db, FakeCfg([sys.executable, FAKE]))
    assert _audits(db) == [("claude_version", f"ok {EXPECTED_CLAUDE_VERSION}")]


async def test_check_version_drift_audits(db, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_VERSION", "9.9.9")
    await check_claude_version(db, FakeCfg([sys.executable, FAKE]))
    rows = _audits(db)
    assert rows[0][0] == "claude_version_drift"
    assert "expected=" in rows[0][1] and "9.9.9" in rows[0][1]


async def test_check_version_probe_failure_fail_open(db):
    await check_claude_version(db, FakeCfg([sys.executable, "no-such-file-xyz.py"]))
    assert _audits(db) == [("claude_version_probe_failed",
                            f"expected={EXPECTED_CLAUDE_VERSION}")]
