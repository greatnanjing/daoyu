"""claude CLI 版本探测（TRD §11 版本漂移对策的机制化落地）。

仓库实现对特定 claude CLI 版本的实测行为敏感——flag 语义/输出形态的版本锚点
散落 worker 各处注释（POLICY_MODE 触发条件、bg 三终态、backgrounded 首行、
审批 behavior JSON 契约），版本漂移 = 行为假设失真。启动时探测实际版本与
EXPECTED_CLAUDE_VERSION 比对：匹配记 audit 留痕；漂移/探测失败 audit +
warning 告警。绝不阻断启动（fail-open：探测故障不能 brick bot）。

实测输出形态（2026-08-20 双平台）：`2.1.233 (Claude Code)`（rc=0）。
"""
import asyncio
import logging
import re
import subprocess

from worker.cli_builder import wrap_windows_command

log = logging.getLogger(__name__)

# 实测验收基线：生产服务器 2026-08-19 真机采样 2.1.233（本机 2026-08-20
# 实测已 2.1.234 = 漂移实例，启动即告警提醒跑回归）。
# 升级流程：改此常量 → python -m pytest 全量回归 → 生产服务器 npm i -g 升级。
EXPECTED_CLAUDE_VERSION = "2.1.233"

_VERSION_RE = re.compile(r"\d+\.\d+\.\d+")


def parse_claude_version(output: str) -> str | None:
    """从 `claude --version` 输出提取版本号；无匹配返回 None。"""
    m = _VERSION_RE.search(output)
    return m.group(0) if m else None


def probe_claude_version(prefix: list[str], timeout_s: float = 10.0) -> str | None:
    """同步跑 <prefix> --version 返回版本号；任何失败返回 None（fail-open）。

    prefix 为 claude_bin 的 argv 前缀（str/list 双形态展开后）；
    Windows .cmd 形态须先经 wrap_windows_command（shell=False 直 exec 会 OSError）。"""
    try:
        cp = subprocess.run([*prefix, "--version"],
                            capture_output=True, timeout=timeout_s)
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("claude --version 探测失败（fail-open）: %r", e)
        return None
    out = (cp.stdout + b"\n" + cp.stderr).decode("utf-8", "replace")
    if cp.returncode != 0:
        log.warning("claude --version rc=%s（fail-open）: %s",
                    cp.returncode, out.strip()[:200])
        return None
    return parse_claude_version(out)


async def check_claude_version(db, config) -> None:
    """启动探测入口（app.py main_async 崩溃恢复块后调用一次）。

    三分支：匹配 audit("claude_version") / 漂移 audit("claude_version_drift")
    +warning / 探测失败 audit("claude_version_probe_failed")+warning。
    subprocess 走 to_thread 不阻塞事件循环。"""
    bin_ = config.claude_bin
    prefix = bin_ if isinstance(bin_, list) else [bin_]
    ver = await asyncio.to_thread(
        probe_claude_version, wrap_windows_command(prefix))
    if ver is None:
        db.audit("claude_version_probe_failed",
                 f"expected={EXPECTED_CLAUDE_VERSION}")
        log.warning("claude 版本探测失败（不阻断启动）；预期基线 %s",
                    EXPECTED_CLAUDE_VERSION)
    elif ver == EXPECTED_CLAUDE_VERSION:
        db.audit("claude_version", f"ok {ver}")
    else:
        db.audit("claude_version_drift",
                 f"expected={EXPECTED_CLAUDE_VERSION} got={ver}")
        log.warning("claude CLI 版本漂移：预期 %s 实测 %s ——flag/输出形态的"
                    "实测假设可能失真，升级前跑全量 E2E 回归",
                    EXPECTED_CLAUDE_VERSION, ver)
