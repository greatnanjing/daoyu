"""data/media 定期清理（M3 审查追加项）测试。

os.utime 造文件龄；cleanup_expired_media 纯函数直测 + outbound 挂载层
（_media_cleanup_once）与 db.active_media_paths 保护名单。
"""
import os
import time
from pathlib import Path

from common.db import Database
from gateway.media import cleanup_expired_media
from gateway.outbound import OutboundLoop


def _mk(root: Path, sub: str, name: str, age_days: float = 0.0) -> Path:
    d = root / "data" / "media" / sub
    d.mkdir(parents=True, exist_ok=True)
    f = d / name
    f.write_bytes(b"\x89PNG\r\n\x1a\nxx")
    t = time.time() - age_days * 86400
    os.utime(f, (t, t))
    return f


def test_cleanup_removes_expired_keeps_fresh_and_non_media(tmp_path):
    old = _mk(tmp_path, "inbound", "img-old1.png", age_days=20)
    fresh = _mk(tmp_path, "inbound", "img-fresh.png", age_days=1)
    note = _mk(tmp_path, "outbound", "manual-note.txt", age_days=99)   # 非 img-* 不碰
    n = cleanup_expired_media(tmp_path, 14.0, protected=set())
    assert n == 1
    assert not old.exists()
    assert fresh.exists()
    assert note.exists()


def test_cleanup_protects_active_outbox_refs_any_path_form(tmp_path):
    """未终态 outbox 行引用的文件保留（approval_mcp 写绝对路径；斜杠形态
    差异经 abspath 归一化命中——真实数据流无相对路径形态）。"""
    gone = _mk(tmp_path, "outbound", "img-free.png", age_days=30)
    kept = _mk(tmp_path, "outbound", "img-kept.png", age_days=30)
    kept_fwd = _mk(tmp_path, "outbound", "img-kept-fwd.png", age_days=30)
    protected = {str(kept),                            # 原生形态（Windows 反斜杠）
                 str(kept_fwd).replace("\\", "/")}     # 正斜杠形态（env/JSON 传递常见）
    n = cleanup_expired_media(tmp_path, 14.0, protected)
    assert n == 1
    assert not gone.exists()
    assert kept.exists() and kept_fwd.exists()


def test_cleanup_missing_dirs_is_noop(tmp_path):
    assert cleanup_expired_media(tmp_path, 14.0, protected=set()) == 0


def test_active_media_paths_excludes_sent(tmp_path):
    db = Database(tmp_path / "t.db")
    db.ensure_schema()
    db.enqueue(None, "u", "文本行")                                   # 无 media_path
    db.enqueue_media(None, "u", "/m/pending.png", "c")                # pending → 保护
    db.enqueue_media(None, "u", "/m/sent.png", "c")
    db.enqueue_media(None, "u", "/m/dead.png", "c")
    oid = db._conn.execute("SELECT id FROM outbox WHERE media_path='/m/sent.png'"
                           ).fetchone()["id"]
    db.mark_sent(oid)
    dead_id = db._conn.execute("SELECT id FROM outbox WHERE media_path='/m/dead.png'"
                               ).fetchone()["id"]
    db._conn.execute("UPDATE outbox SET state='dead' WHERE id=?", (dead_id,))
    db._conn.commit()
    assert db.active_media_paths() == {"/m/pending.png", "/m/dead.png"}


class _Cfg:
    """outbound 挂载层测试配置：带 repo_root/media_retention_days（FakeCfg 简版）。"""

    def __init__(self, root, days):
        self.repo_root = root
        self.media_retention_days = days
        self.throttle = {"min_send_interval_s": 0.0, "page_char_limit": 2000,
                         "daily_send_limit": 500, "progress_window_s": 0.0}


async def test_outbound_media_cleanup_once_end_to_end(db, tmp_path):
    old = _mk(tmp_path, "outbound", "img-old.png", age_days=20)
    db.enqueue_media(None, "u", str(old), "")      # 未终态 → 受保护，本轮不删
    loop = OutboundLoop(db, _FakeILink(), _Cfg(tmp_path, 14.0),
                        token_ref={"token": "T", "base_url": ""}, typing_state={})
    await loop._media_cleanup_once()
    assert old.exists()                            # protected 命中
    audits = [r["detail"] for r in db._conn.execute(
        "SELECT detail FROM audit_log WHERE kind='media_cleanup'")]
    assert not audits                              # 删 0 个不刷 audit
    # 终态化（sent）后不再受保护 → 再清即删
    db._conn.execute("UPDATE outbox SET state='sent', sent_at=? WHERE media_path=?",
                     (int(time.time()), str(old)))
    db._conn.commit()
    await loop._media_cleanup_once()
    assert not old.exists()
    audits = [r["detail"] for r in db._conn.execute(
        "SELECT detail FROM audit_log WHERE kind='media_cleanup'")]
    assert audits == ["removed=1"]


async def test_outbound_media_cleanup_disabled_when_zero(db, tmp_path):
    _mk(tmp_path, "inbound", "img-old.png", age_days=99)
    loop = OutboundLoop(db, _FakeILink(), _Cfg(tmp_path, 0),
                        token_ref={"token": "T", "base_url": ""}, typing_state={})
    await loop._media_cleanup_once()
    assert (tmp_path / "data" / "media" / "inbound" / "img-old.png").exists()


async def test_outbound_rolls_day_before_token_guard(db, tmp_path):
    """日界滚动在 token 守卫之前：token 空窗（已清空）时滚动照常发生
    （T6 修正的回归锁——计数重算 + media 清理不因 token 失效停摆）。"""
    old = _mk(tmp_path, "inbound", "img-old.png", age_days=30)
    db.enqueue(None, "u", "x" * 5000)              # 3 页
    oid = db._conn.execute("SELECT id FROM outbox").fetchone()["id"]
    db.mark_sent(oid)                              # 今日 sent → 折算 3
    db.enqueue(None, "u", "待投的 pending 行")       # I-1 观察行
    pending_id = oid + 1
    loop = OutboundLoop(db, _FakeILink(), _Cfg(tmp_path, 14.0),
                        token_ref={"token": "T", "base_url": ""}, typing_state={})
    assert loop._sent_today == 3
    loop._day = -1                                 # 强制日界
    loop._token_ref["token"] = ""                  # 模拟 token 空窗
    await loop._drain_once()
    assert loop._day == time.localtime().tm_yday   # 滚动发生了（守卫没挡住它）
    assert not old.exists()                        # 清理也发生了
    assert db.get_outbox(pending_id).attempts == 0  # 但 outbox 未被 claim（I-1 仍守）


class _FakeILink:
    async def sendmessage(self, *a, **kw):
        return True
