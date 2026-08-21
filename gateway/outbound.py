"""出站发送器：outbox → iLink。最小发送间隔节流、失败重试、死信、每日上限熔断、typing 状态。"""
import asyncio
import logging
import random
import time

from common.text import split_text
from gateway.media import upload_media

log = logging.getLogger(__name__)

_IDLE_POLL_S = 0.5   # 无唤醒信号时的兜底轮询周期（notify() 可即时唤醒）
_BATCH = 10          # 每轮领取的 outbox 条数

_NO_TOKEN_ERR = "无有效 context_token（该用户无入站历史）"
_UNCONFIRMED_ERR = "sendmessage 未确认"


class OutboundLoop:
    def __init__(self, db, ilink, config, token_ref: dict, typing_state: dict):
        self._db = db
        self._ilink = ilink
        self._cfg = config
        self._token_ref = token_ref          # {"token","base_url"}，重连方原子替换
        self._typing = typing_state          # from_user → typing_ticket
        self._wake = asyncio.Event()
        # 重启恢复：当日已发送量按已送达 outbox 行折算（文本分页 + 图片 caption+图），
        # 熔断日计数不再重启清零（M1 Task 11 Minor 清偿）。折算口径与运行时
        # _send/_send_media 递增一致（见 common.text.outbox_sent_pages）
        self._sent_today = self._db.sent_pages_today(
            int(self._cfg.throttle["page_char_limit"]))
        self._day = time.localtime().tm_yday
        self._last_send = 0.0
        self._limit_audited_day = -1         # daily_limit 熔断 audit 已记过的 yday

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

    def _alert_all(self, text: str) -> None:
        """监控告警（M2）：复用出站通道推全部白名单用户。enqueue 是同步 DB 写，
        不涉及网络、不会抛出破坏调用方主路径；cfg 无 whitelist 属性（测试
        FakeCfg）时静默跳过。"""
        for user in sorted(getattr(self._cfg, "whitelist", None) or ()):
            self._db.enqueue(None, user, text)

    async def _drain_once(self) -> None:
        today = time.localtime().tm_yday
        if today != self._day:
            # 日界滚动在 token 守卫**之前**：token 夜间失效（重扫窗最长 600s）时
            # 滚动与 media 清理也不能停——否则两者只在 token 活跃的白天发生
            # （T6 修正）。计数从 DB 重算（而非直接置 0），与 __init__ 恢复同口径
            self._day = today
            self._sent_today = self._db.sent_pages_today(
                int(self._cfg.throttle["page_char_limit"]))
            await self._media_cleanup_once()
        if not self._token_ref["token"]:
            # I-1 守卫：token 空窗期（401/403 清空 → 重连扫码窗最长 600s）绝不
            # claim outbox——空 token 发送必败，5 次尝试会在几十秒内烧光进死信
            # （M1 无 re-drive）。token 恢复后（_swap_token 原子替换 + 下轮 0.5s
            # 轮询/notify 唤醒）自动续投，积压消息活到重连。
            return
        if self._sent_today >= self._cfg.throttle["daily_send_limit"]:
            if self._limit_audited_day != today:
                # 熔断告警：每个熔断周期只记一次（循环 0.5s 一轮，不逐轮刷屏）
                self._db.audit("daily_limit",
                               f"sent={self._sent_today} "
                               f"limit={self._cfg.throttle['daily_send_limit']} "
                               f"出站熔断至明日")
                self._limit_audited_day = today
                self._alert_all(f"⚠️ 今日出站已达上限（sent={self._sent_today} "
                                f"limit={self._cfg.throttle['daily_send_limit']}），"
                                f"已熔断至明日")
            return  # 熔断：超每日上限暂停出站（/status 可见，明日自动恢复）

        for item in self._db.next_outbox_batch(limit=_BATCH):
            if item.kind == "image":
                err = await self._send_media(item)
                if err is None:
                    self._db.mark_sent(item.id)   # 发送条数在 _send/_send_media 内计
                else:
                    self._db.mark_send_failed(item.id, err)
                    if self._db.get_outbox(item.id).state == "dead":
                        self._db.audit("dead_letter",
                                       f"count={self._db.dead_letter_count()} id={item.id}")
                        self._alert_all(f"⚠️ 出站图片死信（id={item.id}）："
                                        f"{item.media_path}")
                continue
            pages = split_text(item.text, self._cfg.throttle["page_char_limit"])
            err = None   # None=全部页送达；str=失败原因（空 token / 未确认 / 异常）
            for page in pages:
                await self._respect_interval()
                err = await self._send(item.to_user, page)
                if err:
                    break   # 任一页失败即止：整条留待重试（item 级重试语义）
            if err is None:
                self._db.mark_sent(item.id)       # 页数在 _send 内逐页计
            else:
                self._db.mark_send_failed(item.id, err)
                if self._db.get_outbox(item.id).state == "dead":
                    self._db.audit("dead_letter",
                                   f"count={self._db.dead_letter_count()} id={item.id}")
                    if not item.text.startswith("⚠️"):
                        # 只告警普通消息：系统性发送故障下告警自身也会死信，
                        # 再对告警告警会 ⚠️→死信→⚠️ 无限自激刷爆 outbox/audit。
                        self._alert_all(f"⚠️ 出站死信（id={item.id}）："
                                        f"{item.text[:60]}…")

    async def _media_cleanup_once(self) -> None:
        """日界搭车清理过期 media（启动时另清一次，见 app.py；同一函数两处挂载）。
        未终态 outbox 行引用的文件受保护（db.active_media_paths）。异常只审计
        不打断——出站主路径绝不因清理挂掉。"""
        root = getattr(self._cfg, "repo_root", None)
        if root is None:
            return   # 测试 FakeCfg 无 repo_root
        try:
            days = float(getattr(self._cfg, "media_retention_days", 14.0))
            if days <= 0:
                return   # 0/负值 = 关闭清理
            from gateway.media import cleanup_expired_media
            n = await asyncio.to_thread(
                cleanup_expired_media, root, days,
                self._db.active_media_paths())
            if n:
                self._db.audit("media_cleanup", f"removed={n}")
                log.info("media 清理：%d 个过期文件", n)
        except Exception as e:
            log.warning("media 清理异常（忽略）: %r", e)
            try:
                self._db.audit("media_cleanup_error", repr(e))
            except Exception:
                pass

    async def _send(self, to_user: str, text: str) -> str | None:
        """发送单页。成功返回 None；失败返回原因（直传 mark_send_failed 的 last_error）。"""
        # TRD "token 陷阱"对策：context_token 只用该用户最新入站消息的。无入站
        # 历史 → 拿不到有效 token，空 token 会 HTTP 200 但静默不投递——绝不发送，
        # 失败原因交由 outbox 重试/死信路径（用户下次来信后即有 token 可投）。
        ctx = self._db.latest_context_token(to_user)
        if not ctx:
            return _NO_TOKEN_ERR
        token = self._token_ref["token"]
        base = self._token_ref["base_url"] or None
        try:
            # typing 是 cosmetic 功能且端点独立：故障时只告警，绝不阻断发送主路径
            try:
                await self._typing_on(to_user, ctx)
            except Exception as e:
                log.warning("typing_on 失败（忽略，继续发送）: user=%s err=%r", to_user, e)
            ok = await self._ilink.sendmessage(to_user, ctx, text, token, base)
        except Exception as e:
            log.warning("sendmessage 异常: user=%s err=%r", to_user, e)
            return f"sendmessage 异常: {e!r}"
        finally:
            try:
                await self._typing_off(to_user)
            except Exception:
                pass   # typing 收尾失败不影响发送结果判定
        if ok:
            # 逐页计数（M1 移交项清偿）：微信侧真实发送条数 = 熔断开销口径。
            # 文本每页与图片 caption 都经 _send，此处一并覆盖；重试重发的页再计
            # ——正是真实发出条数。发送失败的页不计（对端未收到）
            self._sent_today += 1
            return None
        return _UNCONFIRMED_ERR

    async def _send_media(self, item) -> str | None:
        """图片行投递：上传 →（caption 文本条）→ 图片条。重试语义 = 整行重做
        （同文本分页的 item 级重试先例：caption 已发出而图失败时，重试会重发
        caption——单用户自用可接受，换取的是 downloadParam 永不过期缓存问题）。"""
        ctx = self._db.latest_context_token(item.to_user)
        if not ctx:
            return _NO_TOKEN_ERR
        token = self._token_ref["token"]
        base = self._token_ref["base_url"] or None
        try:
            up = await upload_media(self._ilink, item.media_path or "",
                                    item.to_user, token, base)
        except FileNotFoundError:
            return f"图片文件不存在: {item.media_path}"
        except Exception as e:
            return f"CDN 上传失败: {e!r}"
        caption = (item.caption or "").strip()
        if caption:
            await self._respect_interval()
            err = await self._send(item.to_user, caption)
            if err:
                return f"caption 发送失败: {err}"
        await self._respect_interval()
        try:
            try:
                await self._typing_on(item.to_user, ctx)
            except Exception as e:
                log.warning("typing_on 失败（忽略，继续发送）: user=%s err=%r",
                            item.to_user, e)
            ok = await self._ilink.send_image_message(
                item.to_user, ctx,
                download_param=up.download_param,
                aes_key_hex=up.aes_key.hex(),
                size_cipher=up.size_cipher, token=token, base_url=base)
        except Exception as e:
            log.warning("send_image_message 异常: user=%s err=%r", item.to_user, e)
            return f"send_image_message 异常: {e!r}"
        finally:
            try:
                await self._typing_off(item.to_user)
            except Exception:
                pass
        if ok:
            self._sent_today += 1   # 图片条实发 1 条（caption 已在 _send 内计）
            return None
        return _UNCONFIRMED_ERR

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
