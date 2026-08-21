"""M5C3 用户别名展开：route 前一层展开；展开后行为与直接发该文本一致。"""
import asyncio
import json

from common.db import Database
from gateway.app import _expand_alias, handle_inbound

USER = "u@im.wechat"


class Cfg:
    def __init__(self, tmp_path, window=2.0):
        self.repo_root = tmp_path
        self.whitelist = {USER}
        self.default_cwd = str(tmp_path)
        self.throttle = {"min_send_interval_s": 0.0, "progress_window_s": 0.0,
                         "page_char_limit": 2000, "daily_send_limit": 500,
                         "merge_window_s": window}


def _msg(msg_id, text):
    return {"message_id": msg_id, "seq": msg_id, "from_user_id": USER,
            "message_type": 1, "context_token": "CTX",
            "item_list": [{"type": 1, "text_item": {"text": text}}]}


def _task_prompts(db):
    return [r["prompt"] for r in db._conn.execute(
        "SELECT prompt FROM tasks ORDER BY id")]


def _outbox_texts(db):
    return [r["text"] for r in db._conn.execute(
        "SELECT text FROM outbox ORDER BY id")]


def _set_alias(db, name, value):
    cur = json.loads(db.get_state(f"alias:{USER}") or "{}")
    cur[name] = value
    db.set_state(f"alias:{USER}", json.dumps(cur, ensure_ascii=False))


class _EmptyPool:
    """brief harness 修正（断言不变）：execute_bridge /tasks 分支需
    pool.snapshot()，pool=None 直接 AttributeError（本仓库既有约定见
    test_delete.py「路由分支无 pool 替身可能抛」注记；test_bridge.py 用
    FakePool）。bridge 路径用例传入此最小空池替身，与 FakePool([]) 同构。"""

    def snapshot(self):
        return []

    def running_session_ids(self):
        return []


# ---- _expand_alias 纯逻辑 ----

def test_expand_alias_hit_with_args(tmp_path):
    db = Database(tmp_path / "t.db"); db.ensure_schema()
    _set_alias(db, "go", "跑全量测试")
    assert _expand_alias(db, USER, "/go") == "跑全量测试"
    assert _expand_alias(db, USER, "/go 只跑单元") == "跑全量测试 只跑单元"


def test_expand_alias_miss_cases(tmp_path):
    db = Database(tmp_path / "t.db"); db.ensure_schema()
    assert _expand_alias(db, USER, "普通文本") is None      # 非斜杠
    assert _expand_alias(db, USER, "/") is None             # 裸斜杠
    assert _expand_alias(db, USER, "/nosuch") is None       # 未命中
    db.set_state(f"alias:{USER}", "{oops")                  # 坏 JSON 容错
    assert _expand_alias(db, USER, "/go") is None


def test_expand_alias_empty_value(tmp_path):
    db = Database(tmp_path / "t.db"); db.ensure_schema()
    _set_alias(db, "bad", "")
    assert _expand_alias(db, USER, "/bad") is None


# ---- handle_inbound 集成 ----

async def test_alias_expands_to_chat_enters_merge_window(tmp_path):
    """/go 展开为 chat 文本 → 与直接发该文本一致：进合并窗口。"""
    db = Database(tmp_path / "t.db"); db.ensure_schema()
    _set_alias(db, "go", "跑全量测试")
    cfg = Cfg(tmp_path, window=0.05)
    await handle_inbound(db, cfg, None, None, _msg(1, "/go"), ilink=None)
    assert _task_prompts(db) == []                       # 窗口内未建任务
    await asyncio.sleep(0.15)
    assert _task_prompts(db) == ["跑全量测试"]            # flush 后 prompt=展开文本
    # 入站落盘存原始 /go（审计看用户发了什么）
    assert [r["text"] for r in db._conn.execute(
        "SELECT text FROM messages")][0] == "/go"


async def test_alias_expands_to_bridge_command(tmp_path):
    """/自定义 t 映射 /tasks：展开为斜杠 → bridge 秒回，不建任务。"""
    db = Database(tmp_path / "t.db"); db.ensure_schema()
    _set_alias(db, "t", "/tasks")
    cfg = Cfg(tmp_path, window=0.05)
    await handle_inbound(db, cfg, _EmptyPool(), None, _msg(1, "/t"), ilink=None)
    assert _task_prompts(db) == []                       # bridge 不建任务
    assert any("没有运行中或排队的任务" in t for t in _outbox_texts(db))


async def test_alias_expansion_single_layer(tmp_path):
    """/a 展开 /b 后不再展开 /b（一层防循环）——/b 未定义故落 unknown 提示。"""
    db = Database(tmp_path / "t.db"); db.ensure_schema()
    _set_alias(db, "a", "/b")
    cfg = Cfg(tmp_path, window=0.05)
    await handle_inbound(db, cfg, None, None, _msg(1, "/a"), ilink=None)
    await asyncio.sleep(0.15)
    assert any("未知命令 /b" in t for t in _outbox_texts(db))


async def test_alias_expansion_single_layer_builtin_fallback(tmp_path):
    """一层语义的另一面：展开出 /t 后不再走用户 KV（也无覆盖）→ 落 router
    内置别名 t→tasks（bridge），而非被二次展开或 unknown。"""
    db = Database(tmp_path / "t.db"); db.ensure_schema()
    _set_alias(db, "x", "/t")
    cfg = Cfg(tmp_path, window=0.05)
    await handle_inbound(db, cfg, _EmptyPool(), None, _msg(1, "/x"), ilink=None)
    assert _task_prompts(db) == []
    assert any("没有运行中或排队的任务" in t for t in _outbox_texts(db))


async def test_builtin_alias_without_user_override(tmp_path):
    """无用户覆盖时 /t 走 router 内置映射（Task 4）→ bridge tasks。"""
    db = Database(tmp_path / "t.db"); db.ensure_schema()
    cfg = Cfg(tmp_path, window=0.05)
    await handle_inbound(db, cfg, _EmptyPool(), None, _msg(1, "/t"), ilink=None)
    assert _task_prompts(db) == []
    assert any("没有运行中或排队的任务" in t for t in _outbox_texts(db))
