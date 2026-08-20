"""/delete 命令测试：桥命令预置确认门 + app.py Y/N 拦截执行。

防误删三闸：序号/任务号合法性、当前话题拒删、pending/running 任务拒删；
真删只发生在回 Y 之后（delete_confirm:<user> state 门）。
"""
import json
from types import SimpleNamespace

from gateway.app import handle_inbound
from gateway.bridge import execute_bridge
from gateway.router import Route


class FakeCfg:
    def __init__(self):
        self.reconnect = {"session_duration_s": 86400}
        self.default_cwd = "/repo"


def _route(args=""):
    return Route(kind="bridge", command="delete", args=args, detail={})


async def test_delete_session_sets_confirm_gate_without_deleting(db):
    db.get_or_create_session("u@im.wechat", "/old")        # 较旧，非当前
    db.get_or_create_session("u@im.wechat", "/repo")
    db.set_active_cwd("u@im.wechat", "/repo")
    sessions = db.list_sessions("u@im.wechat")
    target_idx = next(i for i, s in enumerate(sessions, 1) if s.cwd == "/old")
    reply = await execute_bridge(db, None, _route(f"#{target_idx}"),
                                 "u@im.wechat", FakeCfg())
    assert "Y 确认" in reply and "N 取消" in reply
    # 门已置，行未动
    spec = json.loads(db.get_state("delete_confirm:u@im.wechat"))
    assert spec["type"] == "session"
    assert db.get_session(spec["id"]) is not None


async def test_delete_refuses_active_topic(db):
    db.get_or_create_session("u@im.wechat", "/repo")
    db.set_active_cwd("u@im.wechat", "/repo")
    reply = await execute_bridge(db, None, _route("#1"), "u@im.wechat", FakeCfg())
    assert "当前话题" in reply
    assert db.get_state("delete_confirm:u@im.wechat") is None


async def test_delete_session_out_of_range(db):
    db.get_or_create_session("u@im.wechat", "/repo")
    db.set_active_cwd("u@im.wechat", "/repo")
    reply = await execute_bridge(db, None, _route("#9"), "u@im.wechat", FakeCfg())
    assert "超出范围" in reply


async def test_delete_task_gates(db):
    s = db.get_or_create_session("u@im.wechat", "/repo")
    tid_pending = db.create_task(None, s.id, "待办任务", kind="chat")
    tid_done = db.create_task(None, s.id, "已完成任务", kind="chat")
    db.finish_task(tid_done, "done")
    # pending 拒删
    r1 = await execute_bridge(db, None, _route(f"task {tid_pending}"),
                              "u@im.wechat", FakeCfg())
    assert "pending" in r1 and "cancel" in r1
    # 终态可进确认门
    r2 = await execute_bridge(db, None, _route(f"task {tid_done}"),
                              "u@im.wechat", FakeCfg())
    assert "Y 确认" in r2
    spec = json.loads(db.get_state("delete_confirm:u@im.wechat"))
    assert spec == {"type": "task", "id": tid_done}
    # 不存在的任务
    r3 = await execute_bridge(db, None, _route("task 9999"),
                              "u@im.wechat", FakeCfg())
    assert "没有任务" in r3


async def test_delete_bad_args_usage(db):
    for arg in ("", "session 3", "task abc"):
        reply = await execute_bridge(db, None, _route(arg), "u@im.wechat", FakeCfg())
        assert "用法" in reply, arg
    assert db.get_state("delete_confirm:u@im.wechat") is None


def _inbound(msg_id, text):
    return {"message_id": msg_id, "seq": msg_id, "from_user_id": "u@im.wechat",
            "message_type": 1, "context_token": "CTX",
            "item_list": [{"type": 1, "text_item": {"text": text}}]}


class _InboundCfg:
    default_cwd = "/repo"
    whitelist = {"u@im.wechat"}


async def test_confirm_y_deletes_session_with_relations(db):
    s = db.get_or_create_session("u@im.wechat", "/old")
    tid = db.create_task(None, s.id, "一起清掉", kind="chat")
    db.finish_task(tid, "done")
    db.enqueue(tid, "u@im.wechat", "该任务的历史回执")
    db.set_state("delete_confirm:u@im.wechat",
                 json.dumps({"type": "session", "id": s.id}))
    await handle_inbound(db, _InboundCfg(), None, None, _inbound(1, "Y"))
    assert db.get_session(s.id) is None
    assert db.get_task(tid) is None
    assert db._conn.execute("SELECT COUNT(*) c FROM outbox WHERE task_id=?",
                            (tid,)).fetchone()["c"] == 0
    texts = [r["text"] for r in db._conn.execute("SELECT text FROM outbox")]
    assert any("已删除话题" in t for t in texts)
    assert db.get_state("delete_confirm:u@im.wechat") is None   # 门已消费


async def test_confirm_n_cancels_delete(db):
    s = db.get_or_create_session("u@im.wechat", "/old")
    db.set_state("delete_confirm:u@im.wechat",
                 json.dumps({"type": "session", "id": s.id}))
    await handle_inbound(db, _InboundCfg(), None, None, _inbound(2, "N"))
    assert db.get_session(s.id) is not None
    assert db.get_state("delete_confirm:u@im.wechat") is None
    texts = [r["text"] for r in db._conn.execute("SELECT text FROM outbox")]
    assert any("已取消删除" in t for t in texts)


async def test_confirm_y_deletes_task(db):
    s = db.get_or_create_session("u@im.wechat", "/repo")
    tid = db.create_task(None, s.id, "单删这条", kind="chat")
    db.finish_task(tid, "failed")
    db.set_state("delete_confirm:u@im.wechat",
                 json.dumps({"type": "task", "id": tid}))
    await handle_inbound(db, _InboundCfg(), None, None, _inbound(3, "Y"))
    assert db.get_task(tid) is None
    assert db.get_session(s.id) is not None                      # 话题不受影响
    texts = [r["text"] for r in db._conn.execute("SELECT text FROM outbox")]
    assert any("已删除任务" in t for t in texts)


async def test_non_yn_text_does_not_consume_delete_gate(db):
    s = db.get_or_create_session("u@im.wechat", "/old")
    db.set_state("delete_confirm:u@im.wechat",
                 json.dumps({"type": "session", "id": s.id}))
    # 非 Y/N 文本不消费门（会被当普通消息路由——pool=None 下路由前即返回不崩即可，
    # 此处仅断言门仍在、行未删）
    msg = _inbound(4, "先别删，我再想想")
    try:
        await handle_inbound(db, _InboundCfg(), None, None, msg)
    except Exception:
        pass   # 路由分支无 pool 替身可能抛——不在本用例关注面
    assert db.get_state("delete_confirm:u@im.wechat") is not None
    assert db.get_session(s.id) is not None
