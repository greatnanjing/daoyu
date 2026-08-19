"""reconnect 守护与 poll_loop 401 清 token 测试。

覆盖审查修复与静默续期：
- I-1 空 token 死窗：poll 清 token 后 timer 自动重走扫码流程（不等 24h 计时）。
- M-2 扫码超时清 reconnect_warned，下轮 warning 可重发。
- M-3 poll_loop 精确匹配 "HTTP 401/403" 才清 token，且清一次后幂等。
- 静默续期：local_token_list 命中免扫码（grace 窗内不推二维码）、超窗回退
  推码、bot_token 清空后从 bot_token_last 副本续、预警窗直接自动尝试。
"""
import asyncio
import time
from types import SimpleNamespace

from gateway.reconnect import ReconnectTimer

_orig_sleep = asyncio.sleep


class FakeILink:
    def __init__(self, status=None):
        self._status = status or {}
        self.qr_calls = 0
        self.local_tokens_history = []   # 每次 get_bot_qrcode 收到的 local_token_list

    async def get_bot_qrcode(self, local_tokens, base_url=None):
        self.qr_calls += 1
        self.local_tokens_history.append(list(local_tokens))
        return {"qrcode": f"https://qr/{self.qr_calls}",
                "qrcode_img_content": f"https://qr/{self.qr_calls}"}

    async def poll_login_status(self, qrcode, verify_code=None):
        return dict(self._status)


class FakeOutbound:
    def __init__(self):
        self.notified = 0

    def notify(self):
        self.notified += 1


def _cfg(**rc):
    return SimpleNamespace(
        whitelist={"u@im.wechat"},
        reconnect={"session_duration_s": 86400, "warning_before_s": 7200,
                   "force_before_s": 1800, "qrcode_scan_timeout_s": 600, **rc})


def _timer(db, cfg, ilink, token_ref, outbound=None):
    return ReconnectTimer(db, cfg, ilink, token_ref, {}, outbound or FakeOutbound())


def _fast_sleep(monkeypatch):
    # poll_loop 失败重试固定睡 5s：换成即时让步，测试毫秒级跑完多次重试。
    async def _sleep(_t):
        await _orig_sleep(0)
    monkeypatch.setattr("gateway.app.asyncio.sleep", _sleep)


async def test_empty_token_triggers_reconnect_automatically(db):
    # I-1：login_at 全新（deadline 路径不会触发），token 被 poll 清空 →
    # timer 首轮即自动置 reconnect_now 并完成扫码换 token，死窗变自动重试。
    db.set_state("login_at", str(time.time()))
    db.set_state("bot_token", "")
    token_ref = {"token": "", "base_url": ""}
    ilink = FakeILink({"bot_token": "NEW", "baseurl": "https://b"})
    task = asyncio.create_task(_timer(db, _cfg(), ilink, token_ref).run_forever())

    async def swapped():
        while token_ref["token"] != "NEW":
            await _orig_sleep(0.05)
    await asyncio.wait_for(swapped(), 5)
    assert ilink.qr_calls >= 1
    assert db.get_state("bot_token") == "NEW"
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def test_warning_triggers_auto_reconnect_attempt(db):
    # 预警窗行为变更（后台自动续期）：不再挂 Y/N 确认，直接置 reconnect_now；
    # 静默/推码分流由 _do_reconnect 处理（另有专门用例）。
    cfg = _cfg()
    db.set_state("login_at", str(time.time() - (86400 - 3600)))   # remain=3600s ∈ 预警窗
    _timer(db, cfg, FakeILink(), {"token": "T", "base_url": ""})._check_deadline()
    assert db.get_state("reconnect_now") == "1"
    assert db.get_state("reconnect_warned") == "1"
    assert not db.get_state("reconnect_confirm")


async def test_scan_timeout_clears_warned_for_next_round(db):
    # M-2：扫码超时只重置 login_at 不清 warned → 下轮 warning 永不重发的 bug。
    db.set_state("reconnect_warned", "1")
    token_ref = {"token": "OLD", "base_url": ""}
    t = _timer(db, _cfg(qrcode_scan_timeout_s=0.05), FakeILink({}), token_ref)
    await t._do_reconnect()
    assert db.get_state("reconnect_warned") == ""
    assert float(db.get_state("login_at")) > time.time() - 10   # 计时已重置


async def test_poll_loop_clears_token_on_exact_http_401(db, monkeypatch):
    # M-3：连续失败且文案含 "HTTP 401" → 清 state 与运行时引用各一次，循环不崩。
    from gateway import app as app_mod
    from gateway.ilink import ILinkError

    class Unauthorized:
        def __init__(self):
            self.n = 0

        async def getupdates(self, buf, token, base_url=None):
            self.n += 1
            raise ILinkError("POST ilink/bot/getupdates HTTP 401: token invalid")

    ilink = Unauthorized()
    db.set_state("bot_token", "VALID")
    token_ref = {"token": "VALID", "base_url": ""}
    _fast_sleep(monkeypatch)
    task = asyncio.create_task(
        app_mod.poll_loop(db, None, ilink, None, None, token_ref))

    async def cleared():
        while token_ref["token"]:
            await _orig_sleep(0.01)
    await asyncio.wait_for(cleared(), 5)
    n_at_clear = ilink.n
    assert db.get_state("bot_token") == ""

    async def still_alive():   # 清空后幂等门放行重试、循环继续活着
        while ilink.n < n_at_clear + 3:
            await _orig_sleep(0.01)
    await asyncio.wait_for(still_alive(), 5)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_poll_loop_keeps_token_on_bare_401_substring(db, monkeypatch):
    # M-3：错误体里恰含 "401" 子串（订单号等）的普通 5xx 不得误清仍有效的 token。
    from gateway import app as app_mod
    from gateway.ilink import ILinkError

    class ServerError:
        def __init__(self):
            self.n = 0

        async def getupdates(self, buf, token, base_url=None):
            self.n += 1
            raise ILinkError("POST ilink/bot/getupdates HTTP 500: id 40123 not found")

    ilink = ServerError()
    db.set_state("bot_token", "VALID")
    token_ref = {"token": "VALID", "base_url": ""}
    _fast_sleep(monkeypatch)
    task = asyncio.create_task(
        app_mod.poll_loop(db, None, ilink, None, None, token_ref))

    async def ran_awhile():   # 连续 8 次失败（远超阈值 5）
        while ilink.n < 8:
            await _orig_sleep(0.01)
    await asyncio.wait_for(ran_awhile(), 5)
    assert token_ref["token"] == "VALID"
    assert db.get_state("bot_token") == "VALID"
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def _outbox_texts(db):
    cols = [r[1] for r in db._conn.execute("PRAGMA table_info(outbox)")]
    return [dict(zip(cols, r))["text"] for r in db._conn.execute("SELECT * FROM outbox")]


async def test_silent_renew_no_qr_pushed(db):
    # 静默续期命中：服务端秒回 bot_token，grace 窗内不推二维码、
    # 回执是"免扫码"文案；bot_token_last 副本同步落盘。
    db.set_state("bot_token", "OLD")
    token_ref = {"token": "OLD", "base_url": ""}
    t = _timer(db, _cfg(silent_grace_s=10), FakeILink({"bot_token": "NEW",
                                                       "baseurl": "https://b"}), token_ref)
    await t._do_reconnect()
    assert token_ref["token"] == "NEW"
    texts = _outbox_texts(db)
    assert any("免扫码" in x for x in texts)
    assert not any("扫码（或打开链接）" in x for x in texts)
    assert db.get_state("bot_token_last") == "NEW"


async def test_silent_already_connected_no_qr_pushed(db):
    # 服务端确认仍连接（binded_redirect）：静默刷新计时，不推二维码。
    db.set_state("bot_token", "OLD")
    token_ref = {"token": "OLD", "base_url": ""}
    t = _timer(db, _cfg(silent_grace_s=10), FakeILink({"already_connected": True}),
               token_ref)
    await t._do_reconnect()
    assert token_ref["token"] == "OLD"                     # token 未换
    assert float(db.get_state("login_at")) > time.time() - 10
    texts = _outbox_texts(db)
    assert any("连接仍有效" in x for x in texts)
    assert not any("扫码（或打开链接）" in x for x in texts)


async def test_qr_fallback_after_silent_grace(db, monkeypatch):
    # 静默窗口耗尽仍未确认 → 回退推二维码（现行扫码路径不变）。
    # poll 间隔固定睡 2s 会越过缩小的 deadline：换即时让步（同 _fast_sleep 模式）。
    async def _sleep(_t):
        await _orig_sleep(0)
    monkeypatch.setattr("gateway.reconnect.asyncio.sleep", _sleep)
    db.set_state("bot_token", "OLD")
    token_ref = {"token": "OLD", "base_url": ""}
    t = _timer(db, _cfg(silent_grace_s=0.05, qrcode_scan_timeout_s=0.3),
               FakeILink({}), token_ref)
    await t._do_reconnect()
    assert any("扫码（或打开链接）" in x for x in _outbox_texts(db))


async def test_cleared_token_uses_last_known_copy(db):
    # bot_token 被 401 清空 → local_token_list 从 bot_token_last 副本取，
    # 静默续期通道不因清空丢失。
    db.set_state("bot_token", "")
    db.set_state("bot_token_last", "LAST")
    token_ref = {"token": "", "base_url": ""}
    ilink = FakeILink({"bot_token": "NEW", "baseurl": "https://b"})
    t = _timer(db, _cfg(silent_grace_s=10), ilink, token_ref)
    await t._do_reconnect()
    assert ilink.local_tokens_history and ilink.local_tokens_history[0] == ["LAST"]
    assert token_ref["token"] == "NEW"


class _StaleTokenILink:
    """getupdates 恒返回 200 + errcode -14（session timeout）的替身。"""

    def __init__(self, n_max=100):
        self.n = 0
        self._n_max = n_max

    async def getupdates(self, buf, token, base_url=None):
        self.n += 1
        if self.n > self._n_max:
            raise asyncio.CancelledError   # 足够轮数后结束测试循环
        return {"ret": -14, "errcode": -14, "errmsg": "session timeout"}


async def test_poll_loop_clears_token_on_repeated_errcode_14(db, monkeypatch):
    # 官方语义（openclaw-weixin README/monitor.js）：token 失效 = 200 响应体
    # errcode/ret = -14（session timeout），HTTP 仍 200。连续 5 次防抖后清
    # token 走重连；只盯 HTTP 401 的旧实现会静默空转。
    from gateway import app as app_mod
    _fast_sleep(monkeypatch)
    ilink = _StaleTokenILink()
    db.set_state("bot_token", "VALID")
    token_ref = {"token": "VALID", "base_url": ""}
    task = asyncio.create_task(
        app_mod.poll_loop(db, None, ilink, None, None, token_ref))

    async def cleared():
        while token_ref["token"]:
            await _orig_sleep(0.01)
    await asyncio.wait_for(cleared(), 5)
    assert db.get_state("bot_token") == ""
    assert ilink.n >= 5   # 第 5 次防抖阈值即触发（清空后循环续跑到取消，n 可略多）
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_poll_loop_errcode_14_debounce_and_other_codes(db, monkeypatch):
    # 防抖：非连续 5 次 -14 不清；非 -14 的错误码（ret/errcode 其他值）不清。
    from gateway import app as app_mod
    _fast_sleep(monkeypatch)

    class Flaky:
        def __init__(self):
            self.n = 0

        async def getupdates(self, buf, token, base_url=None):
            self.n += 1
            if self.n > 12:
                raise asyncio.CancelledError
            # 4 次 -14 + 1 次正常 + 1 次 -14 + …：永远凑不满连续 5 次
            seq = [{"errcode": -14}, {"ret": 0, "msgs": []}, {"ret": -14},
                   {"errcode": -99}]
            return seq[self.n % 4]

    ilink = Flaky()
    token_ref = {"token": "VALID", "base_url": ""}
    task = asyncio.create_task(
        app_mod.poll_loop(db, None, ilink, None, None, token_ref))
    await _orig_sleep(0.3)
    assert token_ref["token"] == "VALID"   # 防抖生效未误清
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
