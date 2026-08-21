"""M5A 通知 HTTP 入口：build_app 路由行为（鉴权/校验/广播）+ 开关。"""
from types import SimpleNamespace

from aiohttp.test_utils import TestClient, TestServer

from gateway.notify_http import build_app, run_notify_http


def _cfg(whitelist=None, token=""):
    return SimpleNamespace(
        whitelist=set(whitelist or {"a@im.wechat", "b@im.wechat"}),
        secrets={"notify_token": token} if token else {})


async def test_notify_http_ok_broadcast(db):
    async with TestClient(TestServer(build_app(db, _cfg()))) as cli:
        r = await cli.post("/notify", json={"title": "备份完成", "body": "ok"})
        assert r.status == 200
        assert await r.json() == {"queued": 2}
    rows = db._conn.execute("SELECT to_user, text FROM outbox").fetchall()
    assert {r["to_user"] for r in rows} == {"a@im.wechat", "b@im.wechat"}
    assert all(r["text"] == "🔔 备份完成\nok" for r in rows)


async def test_notify_http_title_required(db):
    async with TestClient(TestServer(build_app(db, _cfg()))) as cli:
        assert (await cli.post("/notify", json={"body": "x"})).status == 400
        assert (await cli.post("/notify", json={"title": "  "})).status == 400
        assert (await cli.post("/notify", data="not json",
                               headers={"Content-Type": "application/json"}
                               )).status == 400


async def test_notify_http_token(db):
    cfg = _cfg(token="s3cret")
    async with TestClient(TestServer(build_app(db, cfg))) as cli:
        assert (await cli.post("/notify",
                               json={"title": "t"})).status == 401
        r = await cli.post("/notify", json={"title": "t"},
                           headers={"Authorization": "Bearer s3cret"})
        assert r.status == 200


async def test_run_notify_http_disabled_returns(db):
    cfg = SimpleNamespace(whitelist={"a@im.wechat"}, secrets={},
                          notify={"http_enabled": False})
    assert await run_notify_http(db, cfg) is None   # 即返、不监听
