"""iLink (ClawBot) 协议封装。纯协议、无业务；字段必须全填（缺字段会 200 但静默不投递）。"""
import base64
import json
import logging
import random
from urllib.parse import quote

import aiohttp

log = logging.getLogger(__name__)

BASE_URL = "https://ilinkai.weixin.qq.com"
ILINK_APP_ID = "bot"
ILINK_APP_CLIENT_VERSION = str((2 << 16) | (4 << 8) | 3)  # "2.4.3" → "132099"
CHANNEL_VERSION = "2.4.3"
BOT_AGENT = "daoyu/0.1.0 (python)"


def make_headers(token: str | None = None) -> dict:
    uin = str(random.randint(0, 0xFFFFFFFF))
    headers = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "X-WECHAT-UIN": base64.b64encode(uin.encode()).decode(),
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": ILINK_APP_CLIENT_VERSION,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def base_info() -> dict:
    return {"channel_version": CHANNEL_VERSION, "bot_agent": BOT_AGENT}


class ILinkError(Exception):
    pass


class ILinkClient:
    def __init__(self, session: aiohttp.ClientSession, base_url: str = BASE_URL):
        self._session = session
        self._base = base_url.rstrip("/")

    async def _post(self, path: str, body: dict, token: str | None = None,
                    base_url: str | None = None) -> dict:
        url = f"{base_url or self._base}/{path}"
        async with self._session.post(url, json=body, headers=make_headers(token)) as res:
            # 服务器 Content-Type 是 application/octet-stream，故手动 text + json.loads（content-type 无关）
            text = await res.text()
            if res.status != 200:
                raise ILinkError(f"POST {path} HTTP {res.status}: {text[:200]}")
            try:
                return json.loads(text)
            except ValueError:  # 200 但非 JSON 体
                return {}

    async def _get(self, path: str, token: str | None = None) -> dict:
        url = f"{self._base}/{path}"
        async with self._session.get(url, headers=make_headers(token)) as res:
            text = await res.text()
            if res.status != 200:
                raise ILinkError(f"GET {path} HTTP {res.status}: {text[:200]}")
            try:
                return json.loads(text)
            except ValueError:  # 200 但非 JSON 体
                return {}

    # ---- 登录 ----
    async def get_bot_qrcode(self, local_tokens: list[str],
                             base_url: str | None = None) -> dict:
        data = await self._post("ilink/bot/get_bot_qrcode?bot_type=3",
                                {"local_token_list": local_tokens}, None, base_url)
        if not data.get("qrcode"):
            data = await self._get("ilink/bot/get_bot_qrcode?bot_type=3")  # 旧版 GET 兜底
        if not data.get("qrcode"):
            raise ILinkError(f"get_bot_qrcode 无 qrcode: {data}")
        return data

    async def poll_login_status(self, qrcode: str, verify_code: str | None = None) -> dict:
        endpoint = f"ilink/bot/get_qrcode_status?qrcode={quote(qrcode, safe='')}"
        if verify_code:
            endpoint += f"&verify_code={quote(verify_code, safe='')}"
        status = await self._get(endpoint)
        state = status.get("status", "")
        if state == "confirmed" or status.get("bot_token"):
            return {"bot_token": status.get("bot_token"),
                    "baseurl": status.get("baseurl") or status.get("base_url") or self._base}
        if state == "binded_redirect" or status.get("binded_redirect"):
            return {"already_connected": True}
        if state == "expired":
            return {"expired": True}
        if state == "scaned_but_redirect":
            host = status.get("redirect_host")
            return {"redirect_base": f"https://{host}"} if host else {}
        if state == "scanned":
            return {"scanned": True}
        if state == "need_verifycode":
            return {"need_verifycode": True}
        if state == "verify_code_blocked":
            return {"verify_code_blocked": True}
        return {}

    # ---- 收发 ----
    async def getupdates(self, buf: str, token: str | None = None,
                         base_url: str | None = None) -> dict:
        return await self._post(
            "ilink/bot/getupdates",
            {"get_updates_buf": buf, "base_info": base_info()}, token, base_url)

    async def sendmessage(self, to_user: str, context_token: str, text: str,
                          token: str | None = None, base_url: str | None = None) -> bool:
        client_id = f"daoyu-{random.randint(0, 0xFFFFFFFFF):09x}"  # "daoyu-" + 9 hex = 15 字符
        try:
            data = await self._post(
                "ilink/bot/sendmessage",
                {"msg": {
                    "from_user_id": "",
                    "to_user_id": to_user,
                    "client_id": client_id,
                    "message_type": 2,
                    "message_state": 2,
                    "context_token": context_token,
                    "item_list": [{"type": 1, "text_item": {"text": text}}],
                },
                    "base_info": base_info()}, token, base_url)
        except (ILinkError, aiohttp.ClientError, ValueError) as e:
            # 送达未确认 → False 交由 outbox 重试；网络故障/坏响应体也不让异常逃逸
            log.warning("sendmessage 发送失败（送达未确认）: %s", e, exc_info=True)
            return False
        errcode = data.get("errcode", 0)
        if errcode:
            log.warning("sendmessage 被拒: errcode=%s errmsg=%s", errcode, data.get("errmsg"))
        return not errcode

    async def getconfig(self, ilink_user_id: str, context_token: str,
                        token: str | None = None, base_url: str | None = None) -> str:
        data = await self._post(
            "ilink/bot/getconfig",
            {"ilink_user_id": ilink_user_id, "context_token": context_token,
             "base_info": base_info()}, token, base_url)
        return data.get("typing_ticket", "")

    async def sendtyping(self, ilink_user_id: str, ticket: str, status: int,
                         token: str | None = None, base_url: str | None = None) -> None:
        await self._post(
            "ilink/bot/sendtyping",
            {"ilink_user_id": ilink_user_id, "typing_ticket": ticket,
             "status": status, "base_info": base_info()}, token, base_url)
