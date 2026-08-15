"""reconnect 守护与 poll_loop 401 清 token 测试。

覆盖三处审查修复：
- I-1 空 token 死窗：poll 清 token 后 timer 自动重走扫码流程（不等 24h 计时）。
- M-1 warning 确认挂首个白名单用户（单用户产品，不再循环覆盖 state 键）。
- M-2 扫码超时清 reconnect_warned，下轮 warning 可重发。
- M-3 poll_loop 精确匹配 "HTTP 401/403" 才清 token，且清一次后幂等。
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

    async def get_bot_qrcode(self, local_tokens, base_url=None):
        self.qr_calls += 1
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


def test_warning_confirm_pins_first_whitelist_user(db):
    # M-1：Y/N 确认挂 sorted 首个白名单用户，不再被 for 循环覆盖成最后一人。
    cfg = _cfg()
    cfg.whitelist = {"b@im.wechat", "a@im.wechat"}
    db.set_state("login_at", str(time.time() - (86400 - 3600)))   # remain=3600s ∈ 预警窗
    _timer(db, cfg, FakeILink(), {"token": "T", "base_url": ""})._check_deadline()
    assert db.get_state("reconnect_confirm") == "a@im.wechat"
    assert db.get_state("reconnect_warned") == "1"


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
