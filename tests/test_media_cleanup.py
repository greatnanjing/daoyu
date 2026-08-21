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


def test_cleanup_outbound_full_inbound_prefix_root_conservative(tmp_path):
    """M5B 清理规则三分：outbound/ 全量（daoyu 独占——img-* 与 M5B 原名复制）；
    inbound/ 按前缀（img-|file-|voice-|vid-，daoyu 随机名落盘）；media 根目录
    保守规则不变（claude 工作产物混居，只碰 img-*/图片扩展名）。"""
    old_out = _mk(tmp_path, "outbound", "manual-note.txt", age_days=99)
    old_f = _mk(tmp_path, "inbound", "file-old1.pdf", age_days=20)
    old_v = _mk(tmp_path, "inbound", "vid-old1.mp4", age_days=20)
    stray = _mk(tmp_path, "inbound", "claude-draft.txt", age_days=99)   # 非前缀不碰
    fresh = _mk(tmp_path, "inbound", "img-fresh.png", age_days=1)
    n = cleanup_expired_media(tmp_path, 14.0, protected=set())
    assert n == 3
    assert not old_out.exists() and not old_f.exists() and not old_v.exists()
    assert stray.exists() and fresh.exists()


def test_cleanup_covers_media_root_custom_named_images(tmp_path):
    """media 根目录的 claude 工作产物（自定义名截图，真机实证 hermes-pw.png
    堆根目录）：图片扩展名过期即删；非图片工作产物（tmp-*.html）不碰。"""
    root_png = _mk(tmp_path, "", "hermes-pw.png", age_days=30)     # "" = media 根
    root_html = _mk(tmp_path, "", "tmp-8891.html", age_days=30)
    sub_png = _mk(tmp_path, "outbound", "render-8891.PNG", age_days=30)  # 大写扩展名
    n = cleanup_expired_media(tmp_path, 14.0, protected=set())
    assert n == 2
    assert not root_png.exists() and not sub_png.exists()
    assert root_html.exists()


def test_cleanup_protects_active_outbox_refs_any_path_form(tmp_path):
    """未终态 outbox 行引用的文件保留（M5B 起 outbound 全量清理，保护名单
    更关键——原名复制文件也在 outbound）。"""
    gone = _mk(tmp_path, "outbound", "report-old.pdf", age_days=30)
    kept = _mk(tmp_path, "outbound", "report-kept.pdf", age_days=30)
    kept_fwd = _mk(tmp_path, "outbound", "report-kept-fwd.pdf", age_days=30)
    protected = {str(kept),
                 str(kept_fwd).replace("\\", "/")}
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
