"""主入口：组装 gateway + worker，运行入站管道。gateway 永不阻塞、绝不等 Claude。"""
import asyncio
import json
import logging
import time

import aiohttp

from common.config import load_config
from common.db import Database
from common.models import InboundMessage
from gateway.bridge import execute_bridge, execute_ilink_op
from gateway.ilink import ILinkClient
from gateway.media import MediaError, download_inbound_image
from gateway.outbound import OutboundLoop
from gateway.proxy import execute_proxy
from gateway.reconnect import ReconnectTimer
from gateway.router import route
from worker.pool import WorkerPool
from worker.runner import TaskRunner

log = logging.getLogger("daoyu")


async def _noop_reconnect() -> None:
    """入站管线路径的重连回调占位：真正的重连走 state + ReconnectTimer。"""


async def _save_inbound_images(db, cfg, ilink, image_items, from_user):
    """下载解密全部图 → data/media/inbound/。返回 (成功路径列表, 最后错误或 None)。
    ilink=None（无连接/测试）或单图失败：回执 ⚠️，不让异常逃逸（入站管道不炸）。
    ⚠️ 回执依赖调用方已按 msg_id 查重（I-1/F1）——重投不重复下载、不重复回执。"""
    paths, last_err = [], None
    for img in image_items:
        try:
            if ilink is None:
                raise MediaError("iLink 连接不可用")
            paths.append(await download_inbound_image(
                ilink, img, cfg.repo_root / "data" / "media" / "inbound"))
        except Exception as e:
            last_err = e
            log.warning("入站图片处理失败: %r", e)
    if len(paths) < len(image_items):
        db.enqueue(None, from_user,
                   f"⚠️ 图片接收失败（{last_err}），请重发或改用文字")
    return paths, last_err


async def handle_inbound(db, cfg, pool, outbound, msg: dict, ilink=None) -> None:
    """入站管道：类型过滤 → 白名单 → 落盘去重 → 路由 → 本地秒回或入队。
    M3：item_list 遍历——文本照旧，图片（type==2）下载落盘后"发图即对话"。"""
    if msg.get("message_type") != 1:
        return
    if msg.get("group_id"):
        return  # 群消息忽略（iLink 群聊未正式支持，防误回）
    from_user = msg.get("from_user_id", "")
    if from_user not in cfg.whitelist:
        log.info("非白名单用户 %s，忽略", from_user)
        return

    text_parts: list[str] = []
    image_items: list[dict] = []
    for item in msg.get("item_list") or []:
        if item.get("type") == 2:
            image_items.append(item.get("image_item") or {})
        elif item.get("text_item"):
            # 文本判定不看 type==1：兼容缺 type 键的既有消息构造（M1 起仅取
            # text_item 不校验 type），非图元素带 text_item 即文本。
            text_parts.append(item["text_item"].get("text", ""))
    text = "".join(text_parts).strip()
    msg_key = str(msg.get("message_id") or msg.get("seq") or "")
    if not msg_key:
        log.warning("消息缺 message_id/seq，跳过: %r", msg)
        return
    # I-1/F1：图片下载与 ⚠️ 失败回执都发生在 insert_message 去重之前——先按
    # msg_id 查重（iLink 重连后消息会重投），已存在则整条跳过：重投不再重复
    # 下载 CDN 密文、不再重复回执。成功图首次到达路径行为不变；insert_message
    # 的 UNIQUE 去重仍兜底文本路径。
    if db.message_exists(msg_key):
        return
    media_path: str | None = None
    if image_items:
        image_paths, fail_err = await _save_inbound_images(
            db, cfg, ilink, image_items, from_user)
        media_path = image_paths[0] if image_paths else None
    else:
        image_paths, fail_err = [], None
    if db.insert_message(InboundMessage(
            msg_id=msg_key, from_user=from_user, text=text,
            context_token=msg.get("context_token", ""),
            received_at=int(time.time()), media_path=media_path)) is None:
        return  # msg_id 去重（iLink 重连后消息会重投）

    # 重连 Y/N 确认拦截
    pending_user = db.get_state("reconnect_confirm")
    if pending_user == from_user and text.strip().upper() in ("Y", "N"):
        db.set_state("reconnect_confirm", "")
        if text.strip().upper() == "Y":
            db.enqueue(None, from_user, "好的，正在重新连接…")
            db.set_state("reconnect_now", "1")
        else:
            db.enqueue(None, from_user, "已取消重新连接。")
        if outbound:
            outbound.notify()
        return

    # 审批 Y/N 拦截（strict 档 approval MCP 推来的请求）：排在重连拦截之后、
    # 正常路由之前。只认 Y/N 单字，其余文本不拦截照常入队/路由。
    if text.strip().upper() in ("Y", "N"):
        appr = db.pending_approval(from_user)
        if appr is not None:
            allow = text.strip().upper() == "Y"
            db.decide_approval(appr["id"], "approved" if allow else "denied")
            db.enqueue(None, from_user,
                       "✅ 已允许，Claude 继续" if allow else "🚫 已拒绝")
            if outbound:
                outbound.notify()   # 回执即时送达（approval server 2s 轮询收终态）
            return

    if image_items and not text and not image_paths:
        return   # 纯图且全部下载失败：已有 ⚠️ 回执，不建任务（防空文本进路由）
    if image_paths and not text:
        # 纯图消息：不走路由（空文本无命令语义），直接 chat 任务——发图即对话
        session = db.get_active_binding(from_user, cfg.default_cwd)
        prompt = "\n".join(
            f"[用户发来图片，已保存到 {p}，请查看并回应]" for p in image_paths)
        db.create_task(None, session.id, prompt, kind="chat")
        db.enqueue(None, from_user, "✅ 收到图片，处理中")
        if pool:
            await pool.submit_check()
        if outbound:
            outbound.notify()
        return

    try:
        slash = set(json.loads(db.get_state("slash_commands") or "[]"))
    except ValueError:
        slash = set()
    r = route(text, slash)

    if r.kind == "ilink":
        reply = await execute_ilink_op(db, r, from_user, cfg, _noop_reconnect)
        db.enqueue(None, from_user, reply)
    elif r.kind == "bridge":
        reply = await execute_bridge(db, pool, r, from_user, cfg)
        db.enqueue(None, from_user, reply)
    elif r.kind == "unknown":
        if r.command is None:   # 裸 "/" 等无命令名情形，args 已是人类可读提示
            db.enqueue(None, from_user, r.args)
        else:
            sug = r.detail.get("suggestion")
            hint = f"未知命令 /{r.command}。" + \
                (f"最接近：/{sug}" if sug else "发送 /help 查看可用命令")
            db.enqueue(None, from_user, hint)
    elif r.kind == "proxy":
        db.enqueue(None, from_user, await execute_proxy(db, r, cfg))
    else:  # chat / forward
        session = db.get_active_binding(from_user, cfg.default_cwd)   # 当前话题指针
        prompt = text if r.kind == "chat" else f"/{r.command} {r.args}".strip()
        if image_paths:
            prompt += "\n" + "\n".join(
                f"[用户发来图片，已保存到 {p}，请查看]" for p in image_paths)
        db.create_task(None, session.id, prompt, kind=r.kind)
        db.enqueue(None, from_user, "✅ 收到，处理中")
        if pool:
            await pool.submit_check()   # 即时唤醒调度，不等下一个轮询周期
    if outbound:
        outbound.notify()


async def poll_loop(db, cfg, ilink, pool, outbound, token_ref) -> None:
    """iLink 长轮询收消息。token 失效（连续 401/403）时清空，触发重新扫码路径。"""
    buf = db.get_state("get_updates_buf", "")
    fails = 0
    while True:
        try:
            result = await ilink.getupdates(buf, token_ref["token"],
                                            token_ref["base_url"] or None)
        except Exception as e:
            fails += 1
            log.warning("getupdates 失败（连续 %d 次），5s 后重试: %s", fails, e)
            if fails >= 5 and ("HTTP 401" in str(e) or "HTTP 403" in str(e)):
                # 幂等门：token 已被清空后每轮失败不再重复清；精确匹配 "HTTP 401/403"
                # （ILinkError 文案形如 "POST ... HTTP 401: ..."），避免响应体里
                # 恰含 "401" 子串的普通错误误清仍有效的 token。
                if token_ref["token"]:
                    log.error("连续 %d 次 401/403：bot_token 已失效，清空等待重新登录", fails)
                    db.set_state("bot_token", "")
                    token_ref["token"] = ""
                    # 监控告警（M2）：连接失效推全部白名单用户（自动重连随
                    # ReconnectTimer 的空 token 兜底启动；enqueue 同步 DB 写安全）
                    for user in sorted(getattr(cfg, "whitelist", None) or ()):
                        db.enqueue(None, user,
                                   "⚠️ 微信连接已失效，正在自动重连——"
                                   "可能需要重新扫码（终端/二维码见服务器）")
                    if outbound:
                        outbound.notify()
            await asyncio.sleep(5)
            continue
        fails = 0
        new_buf = result.get("get_updates_buf")
        if new_buf:
            buf = new_buf
            db.set_state("get_updates_buf", buf)
        for m in result.get("msgs") or []:
            try:
                await handle_inbound(db, cfg, pool, outbound, m, ilink=ilink)
            except Exception:
                log.exception("处理入站消息失败: %r", m)


async def main_async() -> None:
    import sys
    for stream in (sys.stdout, sys.stderr):   # Windows 管道默认 cp936，中文日志会乱码
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    cfg = load_config()
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    db = Database(cfg.db_path)
    db.ensure_schema()

    # ---- 崩溃恢复：一切先落盘，重启后 running 重置重跑、出站待发重投 ----
    n = db.reset_running_tasks()
    if n:
        log.info("崩溃恢复：重置 %d 个 running 任务为 pending", n)
        db.audit("recovery", f"reset_running={n}")
    if db.retry_failed_outbox():
        log.info("崩溃恢复：failed 出站消息已重置为 pending")
    if db.dead_letter_count():
        db.audit("startup_dead_letter", f"count={db.dead_letter_count()}")

    token = db.get_state("bot_token")
    token_ref = {"token": token or "",
                 "base_url": db.get_state("bot_base_url", "") or ""}
    typing_state: dict = {}

    async with aiohttp.ClientSession() as session:
        ilink = ILinkClient(session)
        if not token:   # state 有 token 则复用，不扫码
            from gateway.login import terminal_login
            await terminal_login(db, ilink, token_ref["base_url"] or None)
            token_ref["token"] = db.get_state("bot_token") or ""
            token_ref["base_url"] = db.get_state("bot_base_url", "") or ""

        runner = TaskRunner(db, cfg, process_registry={})
        pool = WorkerPool(db, cfg, runner=runner,
                          concurrency=cfg.worker.get("concurrency", 3),
                          poll_interval_s=cfg.worker.get("poll_interval_s", 0.5))
        outbound = OutboundLoop(db, ilink, cfg, token_ref, typing_state)

        tasks = [
            asyncio.create_task(pool.run_forever(), name="worker-pool"),
            asyncio.create_task(outbound.run_forever(), name="outbound"),
            asyncio.create_task(ReconnectTimer(db, cfg, ilink, token_ref,
                                               typing_state, outbound).run_forever(),
                                name="reconnect"),
            asyncio.create_task(poll_loop(db, cfg, ilink, pool, outbound, token_ref),
                                name="poll"),
        ]
        log.info("刀鱼已启动（gateway+worker 同进程）")
        try:
            await asyncio.gather(*tasks)
        finally:
            for t in tasks:   # 持引用 + 退出时统一收尾（KeyboardInterrupt 路径）
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


def start() -> None:
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    start()
