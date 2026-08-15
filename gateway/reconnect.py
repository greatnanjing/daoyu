"""24h 连接过期守护（TRD §3.2）：剩余 warning_before_s 预警（经 outbox 推微信）→
用户 Y/N 或剩 force_before_s 强制 → 推送二维码 URL 重新扫码 → 轮询至新 token 原子替换。"""
import asyncio
import logging
import time

log = logging.getLogger(__name__)


class ReconnectTimer:
    def __init__(self, db, cfg, ilink, token_ref: dict, typing_state: dict, outbound):
        self._db = db
        self._cfg = cfg
        self._ilink = ilink
        self._token_ref = token_ref      # {"token","base_url"}，与 poll/outbound 共享、原子替换
        self._typing = typing_state      # 旧连接的 typing ticket 一并作废
        self._outbound = outbound

    async def run_forever(self) -> None:
        while True:
            try:
                if not self._token_ref["token"]:
                    # 无人值守死窗兜底：poll_loop 连续 401/403 清空 token 后，
                    # 这里自动置 reconnect_now 重走扫码流程（_do_reconnect 会取
                    # 新二维码推微信），不再等 24h 计时器到点。
                    log.info("token 为空（401/403 已清或尚未登录），自动触发重新扫码连接")
                    self._db.set_state("reconnect_now", "1")
                if self._db.get_state("reconnect_now"):
                    self._db.set_state("reconnect_now", "")
                    await self._do_reconnect()
                else:
                    self._check_deadline()
            except Exception as e:   # 保姆代码：守护循环不许死
                log.exception("reconnect timer error: %s", e)
            await asyncio.sleep(self._sleep_s())

    def _check_deadline(self) -> None:
        rc = self._cfg.reconnect
        login_at = float(self._db.get_state("login_at") or 0)
        remain = login_at + rc.get("session_duration_s", 86400) - time.time()
        if remain <= rc.get("force_before_s", 1800):
            log.info("连接剩余 %.0fs，进入强制重连窗口", remain)
            self._db.set_state("reconnect_now", "1")
        elif remain <= rc.get("warning_before_s", 7200) and \
                not self._db.get_state("reconnect_warned"):
            self._db.set_state("reconnect_warned", "1")
            # 单用户产品（TRD）：白名单仅一人，Y/N 确认挂其名下即可；
            # 循环 set_state 同键会被后一次覆盖，白名单多人时只剩最后一人。
            if self._cfg.whitelist:
                self._db.set_state("reconnect_confirm", sorted(self._cfg.whitelist)[0])
            self._notify(f"⏰ 微信连接约 {max(remain, 0) / 3600:.1f} 小时后过期。"
                         f"回复 Y 立即重新扫码连接，N 忽略。")

    def _sleep_s(self) -> float:
        rc = self._cfg.reconnect
        login_at = float(self._db.get_state("login_at") or 0)
        wake_at = (login_at + rc.get("session_duration_s", 86400)
                   - rc.get("warning_before_s", 7200))
        return max(0.5, min(60.0, wake_at - time.time()))

    def _notify(self, text: str) -> None:
        for user in sorted(self._cfg.whitelist):
            self._db.enqueue(None, user, text)
        self._outbound.notify()

    async def _do_reconnect(self) -> None:
        self._db.set_state("reconnect_confirm", "")
        old = self._db.get_state("bot_token") or ""
        try:
            data = await self._ilink.get_bot_qrcode(
                [old] if old else [], self._token_ref["base_url"] or None)
        except Exception as e:
            log.warning("重连：获取二维码失败，重置计时后下轮再试: %r", e)
            self._db.set_state("login_at", str(time.time()))
            return
        qr = str(data.get("qrcode_img_content") or data.get("qrcode") or "")
        self._notify(f"🔄 连接需要续期，请用微信扫码（或打开链接）：\n{qr}")
        deadline = time.monotonic() + self._cfg.reconnect.get("qrcode_scan_timeout_s", 600)
        while time.monotonic() < deadline:
            try:
                r = await self._ilink.poll_login_status(data["qrcode"])
            except Exception:
                r = {}
            if r.get("bot_token"):
                self._swap_token(r["bot_token"], r.get("baseurl") or "")
                self._notify("✅ 重新连接成功。")
                return
            if r.get("already_connected"):
                # 服务端判定仍在连接：刷新计时即可，无需换 token
                self._db.set_state("login_at", str(time.time()))
                self._db.set_state("reconnect_warned", "")
                self._notify("✅ 服务端确认连接仍有效，已续期。")
                return
            await asyncio.sleep(2)
        log.warning("重连：扫码超时，重置计时后下轮再试")
        self._db.set_state("login_at", str(time.time()))
        self._db.set_state("reconnect_warned", "")   # 允许下轮 warning 重新预警

    def _swap_token(self, token: str, base_url: str) -> None:
        """运行时引用与持久化 state 同步替换；typing ticket 绑定旧连接，一并作废。"""
        self._token_ref["token"] = token
        self._token_ref["base_url"] = base_url
        self._db.set_state("bot_token", token)
        self._db.set_state("bot_base_url", base_url)
        self._db.set_state("login_at", str(time.time()))
        self._db.set_state("reconnect_warned", "")
        self._db.set_state("reconnect_confirm", "")
        self._typing.clear()
        self._db.audit("reconnect", "token swapped")
