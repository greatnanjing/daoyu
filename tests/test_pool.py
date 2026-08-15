import asyncio

import pytest

from worker.pool import WorkerPool


class FakeRunner:
    def __init__(self):
        self.started = asyncio.Event()
        self.finish = asyncio.Event()
        self.ran = []
        self.procs = {}

    async def run(self, task, session):
        self.ran.append(task.id)
        self.started.set()
        await self.finish.wait()


@pytest.fixture
def fake_runner():
    return FakeRunner()


def make_pool(db, runner, **kw):
    return WorkerPool(db, config=None, runner=runner, concurrency=2,
                      poll_interval_s=0.01, **kw)


async def test_same_session_serial(db, fake_runner):
    s = db.get_or_create_session("u@im.wechat", "/repo")
    db.create_task(None, s.id, "a")
    db.create_task(None, s.id, "b")
    pool = make_pool(db, fake_runner)
    loop_task = asyncio.create_task(pool.run_forever())
    await asyncio.wait_for(fake_runner.started.wait(), 3)
    await asyncio.sleep(0.2)
    assert fake_runner.ran == [1]                     # 同 session 第二个不启动
    assert pool.running_session_ids() == {s.id}
    fake_runner.finish.set()
    for _ in range(100):                      # 等第二个任务被领取
        if fake_runner.ran == [1, 2]:
            break
        await asyncio.sleep(0.05)
    assert fake_runner.ran == [1, 2]
    loop_task.cancel()


async def test_different_sessions_parallel(db, fake_runner):
    s1 = db.get_or_create_session("u@im.wechat", "/a")
    s2 = db.get_or_create_session("u@im.wechat", "/b")
    db.create_task(None, s1.id, "a")
    db.create_task(None, s2.id, "b")
    pool = make_pool(db, fake_runner)
    loop_task = asyncio.create_task(pool.run_forever())
    await asyncio.wait_for(fake_runner.started.wait(), 3)
    await asyncio.sleep(0.2)
    assert sorted(fake_runner.ran) == [1, 2]          # 跨 session 并行
    fake_runner.finish.set()
    loop_task.cancel()


async def test_concurrency_cap(db, fake_runner):
    sessions = [db.get_or_create_session("u@im.wechat", f"/p{i}") for i in range(4)]
    for s in sessions:
        db.create_task(None, s.id, "x")
    pool = make_pool(db, fake_runner)                  # concurrency=2
    loop_task = asyncio.create_task(pool.run_forever())
    await asyncio.wait_for(fake_runner.started.wait(), 3)
    await asyncio.sleep(0.3)
    assert len(fake_runner.ran) == 2                   # 上限 2
    fake_runner.finish.set()
    loop_task.cancel()


async def test_cancel_pending_and_running(db, fake_runner):
    s = db.get_or_create_session("u@im.wechat", "/repo")
    db.create_task(None, s.id, "a")
    db.create_task(None, s.id, "b")
    pool = make_pool(db, fake_runner)
    loop_task = asyncio.create_task(pool.run_forever())
    await asyncio.wait_for(fake_runner.started.wait(), 3)
    await asyncio.sleep(0.1)
    assert "已取消" in await pool.cancel(2)             # pending → canceled
    assert db.get_task(2).state == "canceled"

    class FakeProc:
        def kill(self):
            fake_runner.finish.set()                    # kill 后 runner 返回
    fake_runner.procs[1] = FakeProc()
    reply = await pool.cancel(1)                        # running → kill
    assert "取消" in reply
    await asyncio.sleep(0.2)
    loop_task.cancel()


async def test_submit_check_snapshot_cancel_edges(db, fake_runner):
    s = db.get_or_create_session("u@im.wechat", "/repo")
    pool = WorkerPool(db, config=None, runner=fake_runner, concurrency=1,
                      poll_interval_s=30)               # 长轮询间隔：凸显 submit_check 的唤醒作用
    loop_task = asyncio.create_task(pool.run_forever())
    await asyncio.sleep(0.05)                           # 空队列 → 池已进入长眠
    assert fake_runner.ran == []
    assert pool.snapshot() == []
    assert "没有" in await pool.cancel(999)             # 不存在的任务

    t = db.create_task(None, s.id, "a")
    assert [x.id for x in pool.snapshot()] == [t]       # pending 即入 /tasks 快照
    await pool.submit_check()                           # 立即唤醒，不等 30s 轮询
    await asyncio.wait_for(fake_runner.started.wait(), 1)
    assert pool.running_session_ids() == {s.id}

    db.finish_task(t, "done")                           # 模拟任务已完结
    assert "无需取消" in await pool.cancel(t)           # 终态 → 不动进程
    fake_runner.finish.set()
    loop_task.cancel()


async def test_runner_crash_does_not_kill_pool(db):
    class CrashRunner:
        procs = {}

        async def run(self, task, session):
            raise RuntimeError("boom")

    s = db.get_or_create_session("u@im.wechat", "/repo")
    db.create_task(None, s.id, "a")
    pool = make_pool(db, CrashRunner())
    loop_task = asyncio.create_task(pool.run_forever())
    for _ in range(100):                        # 等重试耗尽进死信
        if db.get_task(1).state == "dead":
            break
        await asyncio.sleep(0.05)
    await asyncio.sleep(0.1)
    assert db.get_task(1).state == "dead"       # 崩溃 → failed → 重试耗尽 → dead
    assert not loop_task.done()                 # 调度循环仍活着
    kinds = [r["kind"] for r in db._conn.execute(
        "SELECT kind FROM audit_log").fetchall()]
    assert "runner_crash" in kinds              # 崩溃已审计
    loop_task.cancel()
