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


class CdnClientError(ILinkError):
    """CDN 上传 4xx（客户端错误）：立败不重试（官方 cdn-upload.js 语义）。"""


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

    # ---- M3 媒体（字段级协议见 spec §2）----

    async def getuploadurl(self, *, filekey: str, media_type: int, to_user_id: str,
                           rawsize: int, rawfilemd5: str, filesize: int,
                           no_need_thumb: bool, aeskey: str,
                           token: str | None = None,
                           base_url: str | None = None) -> dict:
        return await self._post(
            "ilink/bot/getuploadurl",
            {"filekey": filekey, "media_type": media_type,
             "to_user_id": to_user_id, "rawsize": rawsize,
             "rawfilemd5": rawfilemd5, "filesize": filesize,
             "no_need_thumb": no_need_thumb, "aeskey": aeskey,
             "base_info": base_info()}, token, base_url)

    async def cdn_upload(self, url: str, ciphertext: bytes) -> str:
        """POST 密文到 CDN（裸请求：无 iLink 鉴权头——官方 cdn-upload.js 同）。
        成功取响应头 x-encrypted-param。4xx 抛 CdnClientError（立败）、
        5xx 抛 ILinkError（上层重试）。"""
        async with self._session.post(
                url, data=ciphertext,
                headers={"Content-Type": "application/octet-stream"}) as res:
            if 400 <= res.status < 500:
                err = res.headers.get("x-error-message", "")
                raise CdnClientError(f"CDN 上传客户端错误 {res.status}: {err}")
            if res.status != 200:
                err = res.headers.get("x-error-message", f"status {res.status}")
                raise ILinkError(f"CDN 上传服务端错误: {err}")
            param = res.headers.get("x-encrypted-param")
            if not param:
                raise ILinkError("CDN 上传响应缺 x-encrypted-param 头")
            return param

    async def cdn_download(self, url: str) -> bytes:
        """GET 密文（裸请求；full_url 或拼接 download URL 均可）。
        错误日志只记 URL 前 40 字符（spec §3.5 脱敏：CDN 签名 URL 不整串进日志）。"""
        async with self._session.get(url) as res:
            if res.status != 200:
                raise ILinkError(f"CDN 下载 {res.status}: {url[:40]}…")
            return await res.read()

    async def send_image_message(self, to_user: str, context_token: str, *,
                                 download_param: str, aes_key_hex: str,
                                 size_cipher: int, token: str | None = None,
                                 base_url: str | None = None) -> bool:
        """发图（sendmessage 媒体 item）。容错与 sendmessage 一致：网络/协议
        异常不逃逸，返回 False 交 outbox 重试。

        aes_key_hex 是 hex32 字符串；media.aes_key = base64(hex32 ASCII)——
        官方 send.ts 形态（Buffer.from(aeskey.toString("hex")).toString("base64"))。
        M3 真机验收（2026-08-19）实证：传 base64(raw16B)（24 字符）微信端解不出
        key、图片空白；微信自身发图也是 base64(hex32 ASCII)（44 字符）。CDN 密文
        本身仍用 raw16B 加密（两个形态编码的是同一把 key）。"""
        client_id = f"daoyu-{random.randint(0, 0xFFFFFFFFF):09x}"
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
                    "item_list": [{"type": 2, "image_item": {
                        "media": {"encrypt_query_param": download_param,
                                   "aes_key": base64.b64encode(
                                       aes_key_hex.encode("ascii")).decode(),
                                   "encrypt_type": 1},
                        "mid_size": size_cipher}}],
                },
                    "base_info": base_info()}, token, base_url)
        except (ILinkError, aiohttp.ClientError, ValueError) as e:
            log.warning("send_image_message 发送失败（送达未确认）: %s", e,
                        exc_info=True)
            return False
        errcode = data.get("errcode", 0)
        if errcode:
            log.warning("send_image_message 被拒: errcode=%s errmsg=%s",
                        errcode, data.get("errmsg"))
        return not errcode
