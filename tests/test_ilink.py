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
