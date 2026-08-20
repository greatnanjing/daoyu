"""进程组/整树 kill（/cancel 技术债清偿，M1 移交项）。

_spawn_kwargs 平台分支 + kill_process_tree 孙进程一并杀死的真子进程断言：
父进程 spawn 孙进程写心跳文件 → kill_process_tree → 心跳停更 = 树死。
跨 Windows（taskkill /T 按父子链）与 Linux（start_new_session + killpg）。
"""
import asyncio
import sys
import time

from worker.runner import _spawn_kwargs, kill_process_tree

# 父脚本：spawn 孙进程 → 打印孙 pid → 挂 60s（等被杀）
_PARENT = (
    "import subprocess,sys,time\n"
    "p=subprocess.Popen([sys.executable,'-c',sys.argv[1]])\n"
    "print(p.pid,flush=True)\n"
    "time.sleep(60)\n"
)


def _grandchild_script(heartbeat: str) -> str:
    # 孙脚本：每 0.1s touch 心跳文件（心跳停更 = 孙进程死亡）
    return (f"import time\n"
            f"while True:\n"
            f"    open({heartbeat!r},'a').close()\n"
            f"    time.sleep(0.1)\n")


def test_spawn_kwargs_platform_branches(monkeypatch):
    monkeypatch.setattr("worker.runner.sys.platform", "win32")
    assert _spawn_kwargs() == {}
    monkeypatch.setattr("worker.runner.sys.platform", "linux")
    assert _spawn_kwargs() == {"start_new_session": True}


async def test_kill_process_tree_already_exited_no_raise(tmp_path):
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "pass",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        **_spawn_kwargs())
    await proc.wait()
    kill_process_tree(proc)   # 已退出：直接返回，不 raise


async def test_kill_process_tree_kills_grandchildren(tmp_path):
    heartbeat = tmp_path / "beat"
    heartbeat.touch()
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", _PARENT, _grandchild_script(str(heartbeat)),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        **_spawn_kwargs())
    gpid = (await proc.stdout.readline()).decode().strip()
    assert gpid.isdigit(), f"父进程未打印孙 pid: {gpid!r}"

    def _beat_age() -> float:
        return time.time() - heartbeat.stat().st_mtime

    # 等孙进程心跳活跃（证明孙进程真的在跑，防"孙进程未起就杀"的假阳性）
    for _ in range(50):
        if _beat_age() < 0.5:
            break
        await asyncio.sleep(0.1)
    assert _beat_age() < 0.5, "孙进程心跳未启动"

    kill_process_tree(proc)
    await proc.wait()
    # 祖先被杀后孙进程也死：等 0.6s（心跳周期 0.1s 的 6 倍），心跳停更即树死。
    # 若孙进程残留，mtime 会持续刷新，age 停在 ~0.1s
    await asyncio.sleep(0.6)
    assert _beat_age() > 0.5, (
        f"孙进程(pid={gpid})仍存活：心跳 {heartbeat.stat().st_mtime:.0f} "
        f"仍在更新（age={_beat_age():.2f}s）")
