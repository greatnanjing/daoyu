"""M5C1 连发消息合并窗口：纯文本聚合为一个 prompt；flush-first；启动恢复。"""
import asyncio
import json
import time

from common.db import Database
from common.models import InboundMessage
from gateway.app import (_flush_merge_pending, _append_merge_pending,
                         handle_inbound)

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
    return [r["text"] for r in db._conn.execute("SELECT text FROM outbox ORDER BY id")]


async def test_merge_two_text_messages_single_task(tmp_path):
    db = Database(tmp_path / "t.db"); db.ensure_schema()
    cfg = Cfg(tmp_path, window=0.05)
    await handle_inbound(db, cfg, None, None, _msg(1, "第一步"), ilink=None)
    await handle_inbound(db, cfg, None, None, _msg(2, "第二步"), ilink=None)
    assert _task_prompts(db) == []                      # 窗口内未建任务
    assert any("正在合并" in t for t in _outbox_texts(db))   # 首条 ACK
    await asyncio.sleep(0.15)                           # 过窗口
    prompts = _task_prompts(db)
    assert len(prompts) == 1 and prompts[0] == "第一步\n第二步"
    assert any("已合并 2 条" in t for t in _outbox_texts(db))


async def test_single_message_flushes_on_window(tmp_path):
    db = Database(tmp_path / "t.db"); db.ensure_schema()
    cfg = Cfg(tmp_path, window=0.05)
    await handle_inbound(db, cfg, None, None, _msg(1, "单条"), ilink=None)
    await asyncio.sleep(0.15)
    assert _task_prompts(db) == ["单条"]
    assert any("已合并 1 条" in t for t in _outbox_texts(db))


async def test_queue_position_in_ack(tmp_path):
    """B：已有 pending 任务时 flush ACK 追加队列位次。"""
    db = Database(tmp_path / "t.db"); db.ensure_schema()
    s = db.get_or_create_session(USER, str(tmp_path))
    db.create_task(None, s.id, "前序任务", kind="chat")    # 已有 1 pending
    cfg = Cfg(tmp_path, window=0.05)
    await handle_inbound(db, cfg, None, None, _msg(1, "新消息"), ilink=None)
    await asyncio.sleep(0.15)
    assert any("排在第 2 位" in t for t in _outbox_texts(db))


async def test_slash_command_flushes_pending_first(tmp_path):
    """forward（slash 转发）先 flush 暂存文本任务再建自身任务——序不倒。"""
    db = Database(tmp_path / "t.db"); db.ensure_schema()
    db.set_state("slash_commands", json.dumps(["review"]))
    cfg = Cfg(tmp_path, window=0.05)
    await handle_inbound(db, cfg, None, None, _msg(1, "上下文"), ilink=None)
    await handle_inbound(db, cfg, None, None, _msg(2, "/review"), ilink=None)
    prompts = _task_prompts(db)
    assert prompts[0] == "上下文"          # flush 在前
    assert prompts[1] == "/review"         # slash 任务在后
    await asyncio.sleep(0.15)              # 计时器已无悬挂 flush（slash 先 flush 清了 KV）


async def test_startup_recovery_flushes_pending(tmp_path):
    """崩溃恢复：残留 merge_pending KV → 启动 create_task + ACK + audit + 清 KV。"""
    db = Database(tmp_path / "t.db"); db.ensure_schema()
    s = db.get_or_create_session(USER, str(tmp_path))
    db.set_state(f"merge_pending:{USER}", json.dumps(
        {"texts": ["遗留1", "遗留2"], "session_id": s.id,
         "first_msg_id": "x", "started_at": int(time.time())}))
    # 直接调 flush（recover=True 等价于 main_async 启动恢复的逐条 flush 调用）
    await _flush_merge_pending(db, Cfg(tmp_path), None, None, USER, recover=True)
    assert _task_prompts(db) == ["遗留1\n遗留2"]
    assert any("已恢复 2 条" in t for t in _outbox_texts(db))
    assert db.get_state(f"merge_pending:{USER}") is None
    assert any(r["kind"] == "merge_recover" for r in db._conn.execute(
        "SELECT kind FROM audit_log"))


async def test_slash_flush_cancels_zombie_timer(tmp_path):
    """文本→slash→文本 交错：slash 的 flush-first 弹出计时器必须 cancel，
    否则僵尸在原定 fire 时刻过早冲刷新合并窗口（合并特性系统性失效）。
    窗口=0.3s；"文本2"在 T1 原 fire 之前进新窗口；中间断言卡在 T1 已 fire、
    T2 未 fire 的间隙——无僵尸时应仍只有"文本1"+"/review"。"""
    db = Database(tmp_path / "t.db"); db.ensure_schema()
    db.set_state("slash_commands", json.dumps(["review"]))
    cfg = Cfg(tmp_path, window=0.3)
    await handle_inbound(db, cfg, None, None, _msg(1, "文本1"), ilink=None)
    await handle_inbound(db, cfg, None, None, _msg(2, "/review"), ilink=None)
    await asyncio.sleep(0.15)                  # "文本2"进新窗口（早于 T1 fire=0.30s）
    await handle_inbound(db, cfg, None, None, _msg(3, "文本2"), ilink=None)
    # T1（slash flush 弹出但未 cancel 的僵尸）原定 fire ≈ t=0.30s；T2 窗口 ≈ t=0.45s。
    # t≈0.35s：T1 已 fire、T2 未 fire——僵尸不应过早把"文本2"冲刷成单 prompt。
    await asyncio.sleep(0.20)                  # → t≈0.35s
    prompts = _task_prompts(db)
    assert prompts == ["文本1", "/review"], f"僵尸计时器过早冲刷新窗口: {prompts}"
    # 过窗口后"文本2"作为单 prompt flush（未被僵尸过早冲刷/丢失/拆分）
    await asyncio.sleep(0.20)                  # → t≈0.55s，T2 已 fire
    assert _task_prompts(db) == ["文本1", "/review", "文本2"]


async def test_voice_transcript_merges_in_window(tmp_path):
    """语音转写并入 text_parts 走合并窗口（生产路径，merge_window_s>0）：
    两条语音转写消息窗口内不建任务，过窗口后合并为单 prompt 含两段转写。
    对照 test_inbound_media.test_voice_transcript_treated_as_text（禁用即时路径）。"""
    db = Database(tmp_path / "t.db"); db.ensure_schema()
    cfg = Cfg(tmp_path, window=0.05)

    def _voice_msg(msg_id, transcript):
        return {"message_id": msg_id, "seq": msg_id, "from_user_id": USER,
                "message_type": 1, "context_token": "CTX",
                "item_list": [{"type": 3, "voice_item": {"text": transcript}}]}

    await handle_inbound(db, cfg, None, None,
                        _voice_msg(1, "第一段转写"), ilink=None)
    await handle_inbound(db, cfg, None, None,
                        _voice_msg(2, "第二段转写"), ilink=None)
    assert _task_prompts(db) == []                      # 窗口内未建任务
    assert any("正在合并" in t for t in _outbox_texts(db))   # 首条 ACK
    await asyncio.sleep(0.15)                           # 过窗口
    prompts = _task_prompts(db)
    assert len(prompts) == 1 and prompts[0] == "第一段转写\n第二段转写"
    assert any("已合并 2 条" in t for t in _outbox_texts(db))
