"""出站发送器：outbox → iLink。最小发送间隔节流、失败重试、死信、每日上限熔断、typing 状态。"""
import asyncio
import logging
import random
import time

from common.text import split_text

log = logging.getLogger(__name__)

_IDLE_POLL_S = 0.5   # 无唤醒信号时的兜底轮询周期（notify() 可即时唤醒）
_BATCH = 10          # 每轮领取的 outbox 条数


class OutboundLoop:
    def __init__(self, db, ilink, config, token_ref: dict, typing_state: dict):
        self._db = db
        self._ilink = ilink
        self._cfg = config
        self._token_ref = token_ref          # {"token","base_url"}，重连方原子替换
        self._typing = typing_state          # from_user → typing_ticket
        self._wake = asyncio.Event()
        self._sent_today = 0
        self._day = time.localtime().tm_yday
        self._last_send = 0.0

    async def run_forever(self) -> None:
        while True:
            try:
                await self._drain_once()
            except Exception as e:   # 循环自身不许死（保姆代码），记日志后继续
                log.exception("outbound loop error: %s", e)
                await asyncio.sleep(1)
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=_IDLE_POLL_S)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

    def notify(self) -> None:
        """有新消息入 outbox / 重连成功时唤醒（入站管道调用入口）。"""
        self._wake.set()

    async def _drain_once(self) -> None:
        today = time.localtime().tm_yday
        if today != self._day:
            self._day, self._sent_today = today, 0
        if self._sent_today >= self._cfg.throttle["daily_send_limit"]:
            return  # 熔断：超每日上限暂停出站（/status 可见，明日自动恢复）

        for item in self._db.next_outbox_batch(limit=_BATCH):
            pages = split_text(item.text, self._cfg.throttle["page_char_limit"])
            ok = True
            for page in pages:
                await self._respect_interval()
                ok = await self._send(item.to_user, page)
                if not ok:
                    break   # 任一页失败即止：整条留待重试（item 级重试语义）
            if ok:
                self._db.mark_sent(item.id)
                self._sent_today += 1
            else:
                self._db.mark_send_failed(item.id, "sendmessage 未确认")
                if self._db.get_outbox(item.id).state == "dead":
                    self._db.audit("dead_letter",
                                   f"count={self._db.dead_letter_count()} id={item.id}")

    async def _send(self, to_user: str, text: str) -> bool:
        # TRD "token 陷阱"对策：context_token 只用该用户最新入站消息的。无入站
        # 历史 → 拿不到有效 token，空 token 会 HTTP 200 但静默不投递——绝不发送，
        # return False 交由 outbox 重试/死信路径（用户下次来信后即有 token 可投）。
        ctx = self._db.latest_context_token(to_user)
        if not ctx:
            return False
        token = self._token_ref["token"]
        base = self._token_ref["base_url"] or None
        try:
            await self._typing_on(to_user, ctx)
            ok = await self._ilink.sendmessage(to_user, ctx, text, token, base)
        except Exception:
            return False
        finally:
            try:
                await self._typing_off(to_user)
            except Exception:
                pass   # typing 收尾失败不影响发送结果判定
        return ok

    async def _typing_on(self, user: str, ctx: str) -> None:
        if not ctx:
            return   # 拿不到 ticket 就不发 typing（不影响发送主路径）
        if user not in self._typing:
            self._typing[user] = await self._ilink.getconfig(
                user, ctx, self._token_ref["token"], self._token_ref["base_url"] or None)
        ticket = self._typing.get(user)
        if ticket:
            await self._ilink.sendtyping(user, ticket, 1,
                                         self._token_ref["token"],
                                         self._token_ref["base_url"] or None)

    async def _typing_off(self, user: str) -> None:
        ticket = self._typing.get(user)
        if ticket:
            await self._ilink.sendtyping(user, ticket, 2,
                                         self._token_ref["token"],
                                         self._token_ref["base_url"] or None)

    async def _respect_interval(self) -> None:
        interval = self._cfg.throttle["min_send_interval_s"]
        wait = self._last_send + interval + random.uniform(0, 0.3) - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_send = time.monotonic()
