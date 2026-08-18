import base64
import json

import pytest
from aioresponses import aioresponses

from gateway.ilink import BASE_URL, ILinkClient

QR_PATH = "/ilink/bot/get_bot_qrcode?bot_type=3"


def _body(req) -> dict:
    """从 aioresponses 记录的请求取 JSON body。

    aiohttp 的 ``session.post(json=...)`` 传参在 aioresponses 记录里是 dict 原对象；
    ``data=`` 传参才是序列化后的 str/bytes。两种都兼容，断言强度不变。
    """
    if "json" in req.kwargs:
        payload = req.kwargs["json"]
        return payload if isinstance(payload, dict) else json.loads(payload)
    return json.loads(req.kwargs["data"])


@pytest.fixture
async def client():
    # aioresponses 拦截 ClientSession 层请求，自建 session 即可（无需 aiohttp_client fixture）
    from aiohttp import ClientSession
    async with ClientSession() as s:
        yield ILinkClient(s)


def test_make_headers_shape():
    from gateway.ilink import make_headers
    h = make_headers("TOKEN")
    assert h["Authorization"] == "Bearer TOKEN"
    assert h["AuthorizationType"] == "ilink_bot_token"
    assert h["iLink-App-Id"] == "bot"
    assert h["iLink-App-ClientVersion"] == "132099"
    uin = base64.b64decode(h["X-WECHAT-UIN"]).decode()
    assert uin.isdigit() and 0 <= int(uin) <= 0xFFFFFFFF
    assert make_headers()["X-WECHAT-UIN"] != h["X-WECHAT-UIN"]  # 每次随机


async def test_getupdates_roundtrip(client):
    with aioresponses() as m:
        m.post(f"{BASE_URL}/ilink/bot/getupdates",
               payload={"msgs": [], "get_updates_buf": "BUF1", "errcode": 0},
               headers={"Content-Type": "application/octet-stream"})
        result = await client.getupdates("")
        assert result["get_updates_buf"] == "BUF1"
        req = m.requests[("POST", __import__("yarl").URL(f"{BASE_URL}/ilink/bot/getupdates"))][0]
        body = _body(req)
        assert body["base_info"]["channel_version"] == "2.4.3"
        assert "get_updates_buf" in body


async def test_sendmessage_full_fields(client):
    with aioresponses() as m:
        m.post(f"{BASE_URL}/ilink/bot/sendmessage", payload={})
        ok = await client.sendmessage("u@im.wechat", "CTX", "你好")
        assert ok is True
        req = list(m.requests.values())[0][0]
        body = _body(req)
        msg = body["msg"]
        assert msg["from_user_id"] == ""           # 全字段断言（静默不投递防线）
        assert msg["to_user_id"] == "u@im.wechat"
        assert msg["client_id"].startswith("daoyu-") and len(msg["client_id"]) == 15
        assert msg["message_type"] == 2 and msg["message_state"] == 2
        assert msg["context_token"] == "CTX"
        assert msg["item_list"] == [{"type": 1, "text_item": {"text": "你好"}}]
        assert body["base_info"]["bot_agent"].startswith("daoyu/")


async def test_sendmessage_errcode_is_failure(client):
    with aioresponses() as m:
        m.post(f"{BASE_URL}/ilink/bot/sendmessage", payload={"errcode": 500, "errmsg": "boom"})
        assert await client.sendmessage("u", "c", "t") is False


async def test_sendmessage_http_error_is_failure(client):
    with aioresponses() as m:
        m.post(f"{BASE_URL}/ilink/bot/sendmessage", status=502, payload={})
        assert await client.sendmessage("u", "c", "t") is False


async def test_sendmessage_network_error_returns_false(client):
    import aiohttp
    with aioresponses() as m:
        m.post(f"{BASE_URL}/ilink/bot/sendmessage",
               exception=aiohttp.ClientConnectionError("net down"))
        assert await client.sendmessage("u", "c", "t", token="T") is False


async def test_getconfig_typing_ticket(client):
    with aioresponses() as m:
        m.post(f"{BASE_URL}/ilink/bot/getconfig", payload={"typing_ticket": "TK"})
        assert await client.getconfig("u", "CTX") == "TK"


async def test_login_status_states(client):
    cases = [
        ({"status": "confirmed", "bot_token": "T", "baseurl": "https://x"},
         {"bot_token": "T", "baseurl": "https://x"}),
        ({"status": "expired"}, {"expired": True}),
        ({"status": "binded_redirect"}, {"already_connected": True}),
        ({"status": "scaned_but_redirect", "redirect_host": "h.example"},
         {"redirect_base": "https://h.example"}),
        ({"status": "scanned"}, {"scanned": True}),
        ({"status": "need_verifycode"}, {"need_verifycode": True}),
        ({"status": "wait"}, {}),
    ]
    for payload, expected in cases:
        with aioresponses() as m:
            m.get(f"{BASE_URL}/ilink/bot/get_qrcode_status?qrcode=Q", payload=payload)
            assert await client.poll_login_status("Q") == expected


async def test_get_bot_qrcode_post_first(client):
    with aioresponses() as m:
        m.post(f"{BASE_URL}{QR_PATH}", payload={"qrcode": "Q", "qrcode_img_content": "https://img"})
        data = await client.get_bot_qrcode([])
        assert data["qrcode"] == "Q"


# ---- M3 媒体（字段级协议见 spec §2，源：官方包 v2.4.6）----

CDN = "https://novac2c.cdn.weixin.qq.com/c2c"


async def test_getuploadurl_body_shape(client):
    with aioresponses() as m:
        m.post(f"{BASE_URL}/ilink/bot/getuploadurl",
               payload={"upload_full_url": "https://cdn/up"}, headers={
                   "Content-Type": "application/octet-stream"})
        resp = await client.getuploadurl(
            filekey="ab" * 16, media_type=1, to_user_id="u@im.wechat",
            rawsize=100, rawfilemd5="d41d8cd98f00b204e9800998ecf8427e",
            filesize=112, no_need_thumb=True, aeskey="cd" * 16)
        assert resp["upload_full_url"] == "https://cdn/up"
        req = m.requests[("POST", __import__("yarl").URL(
            f"{BASE_URL}/ilink/bot/getuploadurl"))][0]
        body = _body(req)
        assert body["media_type"] == 1 and body["no_need_thumb"] is True
        assert body["filekey"] == "ab" * 16 and body["aeskey"] == "cd" * 16
        assert body["rawsize"] == 100 and body["filesize"] == 112


async def test_cdn_upload_returns_encrypted_param_header(client):
    from gateway.ilink import ILinkError
    with aioresponses() as m:
        m.post("https://cdn/up", status=200,
               headers={"x-encrypted-param": "DL-PARAM"})
        param = await client.cdn_upload("https://cdn/up", b"ciphertext")
        assert param == "DL-PARAM"
        # 4xx → CdnClientError（立败）；5xx → ILinkError（可重试）
        m.post("https://cdn/e4", status=403,
               headers={"x-error-message": "forbidden"})
        import pytest as _pytest
        from gateway.ilink import CdnClientError
        with _pytest.raises(CdnClientError):
            await client.cdn_upload("https://cdn/e4", b"x")
        m.post("https://cdn/e5", status=503)
        with _pytest.raises(ILinkError):
            await client.cdn_upload("https://cdn/e5", b"x")


async def test_cdn_upload_missing_param_header(client):
    from gateway.ilink import ILinkError
    import pytest as _pytest
    with aioresponses() as m:
        m.post("https://cdn/up", status=200)
        with _pytest.raises(ILinkError):
            await client.cdn_upload("https://cdn/up", b"x")


async def test_cdn_download_returns_bytes(client):
    with aioresponses() as m:
        m.get(f"{CDN}/download?encrypted_query_param=EQ", body=b"\x01\x02\x03")
        buf = await client.cdn_download(f"{CDN}/download?encrypted_query_param=EQ")
        assert buf == b"\x01\x02\x03"


async def test_send_image_message_item_shape(client):
    with aioresponses() as m:
        m.post(f"{BASE_URL}/ilink/bot/sendmessage", payload={})
        ok = await client.send_image_message(
            "u@im.wechat", "CTX", download_param="DL-PARAM",
            aes_key_b64="QUJDREVGR0hJSktMTU4=", size_cipher=112)
        assert ok is True
        req = m.requests[("POST", __import__("yarl").URL(
            f"{BASE_URL}/ilink/bot/sendmessage"))][0]
        body = _body(req)
        msg = body["msg"]
        assert msg["message_type"] == 2 and msg["message_state"] == 2
        assert msg["context_token"] == "CTX"
        item = msg["item_list"][0]
        assert item["type"] == 2
        assert item["image_item"]["media"] == {
            "encrypt_query_param": "DL-PARAM",
            "aes_key": "QUJDREVGR0hJSktMTU4=", "encrypt_type": 1}
        assert item["image_item"]["mid_size"] == 112


async def test_send_image_message_errcode_false(client):
    with aioresponses() as m:
        m.post(f"{BASE_URL}/ilink/bot/sendmessage",
               payload={"errcode": 40001, "errmsg": "bad"})
        ok = await client.send_image_message(
            "u@im.wechat", "CTX", download_param="p", aes_key_b64="a==", size_cipher=1)
        assert ok is False
