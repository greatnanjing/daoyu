"""桥命令执行器与 /help 生成测试。"""
import json

from gateway.bridge import build_help, execute_bridge, execute_ilink_op
from gateway.router import Route


class FakeCfg:
    def __init__(self):
        self.reconnect = {"session_duration_s": 86400}
        self.default_cwd = "/repo"


class FakePool:
    """WorkerPool 最小替身。running_session_ids 为 brief 夹具遗漏、
    真实 WorkerPool 公开接口（execute_bridge /status 分支依赖），此处补齐。"""

    def __init__(self, tasks):
        self._tasks = tasks

    def snapshot(self):
        return self._tasks

    def running_session_ids(self):
        return set()

    async def cancel(self, task_id):
        return f"已取消任务 #{task_id}。"


def _route(cmd, args="", kind="bridge"):
    return Route(kind=kind, command=cmd, args=args, detail={})


async def test_cd_switches_session(db, tmp_path):
    # brief 原用 "/new" 字面量；/cd 有 os.path.isdir 校验，Windows 测试环境下
    # "/new" 解析为当前盘根目录不存在 → 改用 tmp_path 真实目录，断言语义不变。
    new = tmp_path / "new"
    new.mkdir()
    db.get_or_create_session("u@im.wechat", "/old")
    db.set_active_cwd("u@im.wechat", "/old")
    reply = await execute_bridge(db, FakePool([]), _route("cd", str(new)),
                                 "u@im.wechat", FakeCfg())
    assert str(new) in reply
    assert db.get_active_cwd("u@im.wechat", "/d") == str(new)
    assert db.get_or_create_session("u@im.wechat", str(new)).id != \
           db.get_or_create_session("u@im.wechat", "/old").id


async def test_cd_rejects_missing_dir(db):
    db.get_or_create_session("u@im.wechat", "/repo")
    reply = await execute_bridge(db, FakePool([]), _route("cd", "/no/such/dir"),
                                 "u@im.wechat", FakeCfg())
    assert "目录不存在" in reply
    assert db.get_active_cwd("u@im.wechat", "/d") == "/d"  # 未切换


async def test_policy_switch(db):
    s = db.get_or_create_session("u@im.wechat", "/repo")
    db.set_active_cwd("u@im.wechat", "/repo")
    reply = await execute_bridge(db, FakePool([]), _route("policy", "strict"),
                                 "u@im.wechat", FakeCfg())
    assert "strict" in reply
    assert db.get_session(s.id).policy == "strict"


async def test_policy_invalid(db):
    db.get_or_create_session("u@im.wechat", "/repo")
    reply = await execute_bridge(db, FakePool([]), _route("policy", "yolo"),
                                 "u@im.wechat", FakeCfg())
    assert "auto/strict/bypass/plan" in reply


async def test_tasks_listing(db):
    s = db.get_or_create_session("u@im.wechat", "/repo")
    db.create_task(None, s.id, "/review", kind="command")
    reply = await execute_bridge(db, FakePool(db.active_tasks()), _route("tasks"),
                                 "u@im.wechat", FakeCfg())
    assert "#1" in reply and "review" in reply


async def test_status(db):
    # 真造一个 pending task（此前版本插 outbox 行——对 queue_depth 显示毫无影响，
    # "排队" 字样恒在回复模板里，断言恒真）
    s = db.get_or_create_session("u@im.wechat", "/repo")
    db.create_task(None, s.id, "/review", kind="command")
    reply = await execute_bridge(db, FakePool([]), _route("status"),
                                 "u@im.wechat", FakeCfg())
    assert "队列：1 排队" in reply and "死信：0" in reply


async def test_cancel(db):
    reply = await execute_bridge(db, FakePool([]), _route("cancel", "3"),
                                 "u@im.wechat", FakeCfg())
    assert "3" in reply


async def test_cancel_non_numeric(db):
    reply = await execute_bridge(db, FakePool([]), _route("cancel", "abc"),
                                 "u@im.wechat", FakeCfg())
    assert "用法" in reply


def test_help_merges_three_layers(db):
    db.set_state("slash_commands", json.dumps(["review", "model"]))
    text = build_help(db)
    assert "/cancel" in text          # 桥命令层
    assert "/time" in text            # iLink 运维层
    assert "/help" in text            # /help 自身也在列（Minor #8：曾缺失）
    assert "/review" in text          # headless 转发层


async def test_ilink_ops(db):
    cfg = FakeCfg()
    help_text = await execute_ilink_op(db, _route("help", kind="ilink"),
                                       "u@im.wechat", cfg, None)
    assert "/cancel" in help_text
    time_text = await execute_ilink_op(db, _route("time", kind="ilink"),
                                       "u@im.wechat", cfg, None)
    assert "剩余" in time_text
    reconn = await execute_ilink_op(db, _route("重新连接", kind="ilink"),
                                    "u@im.wechat", cfg, None)
    assert "确认" in reconn
    assert db.get_state("reconnect_confirm") == "u@im.wechat"
