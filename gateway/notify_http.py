"""通知通道 HTTP 入口（M5A）：127.0.0.1 单路由 POST /notify。

外部本机进程（curl / 监控脚本 / 第三方系统）→ outbox 广播行（复用出站
协程，节流/日限全继承）。鉴权：secrets.env 设 notify_token 则要求
Bearer；不设则仅 localhost 绑定兜底。协程自保护：启动失败 audit + log
即返回，不杀 gateway 其余通道（同 scheduler 模式）。"""
import asyncio
import logging

from aiohttp import web

from common.notify import push_notification

log = logging.getLogger("daoyu")


def build_app(db, cfg) -> web.Application:
    """单路由应用（aiohttp TestServer 可直测）。"""
    token = (getattr(cfg, "secrets", None) or {}).get("notify_token", "")
    whitelist = sorted(getattr(cfg, "whitelist", None) or ())

    async def handle_notify(request: web.Request) -> web.Response:
        if token:
            if request.headers.get("Authorization", "") != f"Bearer {token}":
                return web.json_response({"error": "unauthorized"}, status=401)
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"error": "invalid json"}, status=400)
        if not isinstance(payload, dict):
            return web.json_response({"error": "json object required"}, status=400)
        title = str(payload.get("title", "")).strip()
        if not title:
            return web.json_response({"error": "title required"}, status=400)
        body = str(payload.get("body", ""))
        n = push_notification(db._conn, whitelist, title, body, source="http")
        return web.json_response({"queued": n})

    app = web.Application()
    app.router.add_post("/notify", handle_notify)
    return app


async def run_notify_http(db, cfg) -> None:
    """常驻协程：起 site 后挂起；http_enabled=False 直接返回（不监听）。"""
    notify_cfg = getattr(cfg, "notify", None) or {}
    if not notify_cfg.get("http_enabled", True):
        return
    listen = str(notify_cfg.get("listen", "127.0.0.1:8417"))
    host, _, port = listen.rpartition(":")
    try:
        runner = web.AppRunner(build_app(db, cfg), access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, host or "127.0.0.1", int(port))
        await site.start()
        log.info("通知 HTTP 入口已监听 http://%s:%s/notify", host, port)
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        raise
    except Exception as e:
        # 启动失败（端口占用等）：audit + log，协程安静退出——其余通道不受影响
        log.error("通知 HTTP 入口启动失败（不影响其余通道）: %r", e)
        db.audit("notify_http_error", repr(e)[:200])
