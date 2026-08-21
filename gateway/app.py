"""主入口：组装 gateway + worker，运行入站管道。gateway 永不阻塞、绝不等 Claude。"""
import asyncio
import json
import logging
import time
from pathlib import Path

import aiohttp

from common.config import load_config
from common.db import Database
from common.models import InboundMessage
from gateway.bridge import execute_bridge, execute_ilink_op
from gateway.ilink import ILinkClient
from gateway.media import (MediaError, download_inbound_image,
                           download_inbound_media)
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


_pending_timers: dict[str, asyncio.TimerHandle] = {}


def _merge_window_s(cfg) -> float | None:
    """合并窗口秒数；throttle 缺失或 merge_window_s<=0 → None（禁用：立即建任务，
    旧路径行为）。生产 load_config 默认 merge_window_s=2.0 恒启用；测试 fake cfg
    常不设 throttle 或缺 merge_window_s 键 → 禁用，既有断言立即建任务的测试零回归。"""
    t = getattr(cfg, "throttle", None)
    if not t:
        return None
    try:
        w = float(t.get("merge_window_s", 0))
    except (TypeError, ValueError):
        return None
    return w if w > 0 else None


async def _flush_merge_pending(db, cfg, pool, outbound, from_user,
                               *, recover: bool = False) -> None:
    """flush 该用户暂存：拼 texts → create_task → 队列感知 ACK → 清 KV/计时。
    recover=True 时 ACK 措辞「已恢复」（启动恢复路径）。无暂存则空操作。"""
    _pending_timers.pop(from_user, None)
    key = f"merge_pending:{from_user}"
    raw = db.get_state(key)
    if not raw:
        return
    try:
        data = json.loads(raw)
    except ValueError:
        db.delete_state(key)
        return
    db.delete_state(key)
    session = db.get_session(data.get("session_id")) or \
        db.get_active_binding(from_user, cfg.default_cwd)
    prompt = "\n".join(data.get("texts") or [])
    if not prompt:
        return
    db.create_task(None, session.id, prompt, kind="chat")
    # create_task 已 commit；pending_task_count 此刻含刚建的本条 → 即队列位次（pos==1 无前序）
    pos = db.pending_task_count(session.id)
    verb = "已恢复" if recover else "已合并"
    ack = f"✅ {verb} {len(data['texts'])} 条消息，开始处理"
    if pos > 1:
        ack += f"（当前任务完成后接上，你排在第 {pos} 位）"
    db.enqueue(None, from_user, ack)
    if recover:
        db.audit("merge_recover", f"user={from_user} count={len(data['texts'])}")
    if pool:
        await pool.submit_check()
    if outbound:
        outbound.notify()


def _schedule_flush(db, cfg, pool, outbound, from_user) -> None:
    """重置/设置该用户的 flush 计时器（asyncio call_later；重启丢 KV 兜底）。"""
    window = float(cfg.throttle.get("merge_window_s", 2.0))
    old = _pending_timers.pop(from_user, None)
    if old:
        old.cancel()
    loop = asyncio.get_event_loop()
    _pending_timers[from_user] = loop.call_later(
        window, lambda: asyncio.create_task(
            _flush_merge_pending(db, cfg, pool, outbound, from_user)))


async def _append_merge_pending(db, cfg, pool, outbound, from_user,
                                text: str, msg_id: str) -> None:
    """纯 chat 文本进窗口：KV 在则追加+重置计时；不在则建+首条 ACK+调度 flush。"""
    key = f"merge_pending:{from_user}"
    cur = db.get_state(key)
    if cur:
        try:
            data = json.loads(cur)
            data["texts"] = (data.get("texts") or []) + [text]
            db.set_state(key, json.dumps(data, ensure_ascii=False))
        except ValueError:
            db.delete_state(key)
            cur = None
    if not cur:
        session = db.get_active_binding(from_user, cfg.default_cwd)
        data = {"texts": [text], "session_id": session.id,
                "first_msg_id": msg_id, "started_at": int(time.time())}
        db.set_state(key, json.dumps(data, ensure_ascii=False))
        window = float(cfg.throttle.get("merge_window_s", 2.0))
        db.enqueue(None, from_user,
                   f"✅ 收到，正在合并后续消息"
                   f"（{window:.0f}s 内无新增即开始处理）")
        if outbound:
            outbound.notify()
    _schedule_flush(db, cfg, pool, outbound, from_user)


async def handle_inbound(db, cfg, pool, outbound, msg: dict, ilink=None) -> None:
    """入站管道：类型过滤 → 白名单 → 落盘去重 → 路由 → 本地秒回或入队。
    M3：图片（type==2）下载落盘后"发图即对话"；M5B：语音（3）转写即文字/
    无转写存档回执、文件（4）带名入 prompt、视频（5）ffmpeg 抽帧提示。"""
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
    voice_items: list[dict] = []    # M5B：无转写的语音（有转写的并入 text_parts）
    file_items: list[dict] = []
    video_items: list[dict] = []
    for item in msg.get("item_list") or []:
        t = item.get("type")
        if t == 2:
            image_items.append(item.get("image_item") or {})
        elif t == 3:
            vi = item.get("voice_item") or {}
            vt = str(vi.get("text") or "").strip()
            if vt:
                text_parts.append(vt)   # 服务端转写：当用户文字（官方同构）
            else:
                voice_items.append(vi)
        elif t == 4:
            file_items.append(item.get("file_item") or {})
        elif t == 5:
            video_items.append(item.get("video_item") or {})
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
    media_lines: list[str] = []   # 建任务用的媒体提示行（图/文件/视频，M5B 泛化）
    if image_items:
        image_paths, fail_err = await _save_inbound_images(
            db, cfg, ilink, image_items, from_user)
        media_path = image_paths[0] if image_paths else None
        media_lines += [f"[用户发来图片，已保存到 {p}，请查看并回应]"
                        for p in image_paths]
    else:
        image_paths, fail_err = [], None
    # M5B：无转写语音存档回执不建任务；文件/视频下载落盘入 prompt 行。
    # in_dir 惰性求值：纯文本消息的 cfg 可无 repo_root（与 _save_inbound_images
    # 内部访问时机一致——只有存在媒体项才触碰）。
    if voice_items or file_items or video_items:
        in_dir = cfg.repo_root / "data" / "media" / "inbound"
    for vi in voice_items:
        try:
            if ilink is None:
                raise MediaError("iLink 连接不可用")
            p = await download_inbound_media(ilink, vi.get("media") or {},
                                             in_dir, "voice", "silk")
            db.enqueue(None, from_user,
                       f"⚠️ 语音未能转写（已存档 {Path(p).name}），"
                       f"请补发文字或转写内容")
        except Exception as e:
            log.warning("入站语音处理失败: %r", e)
            db.enqueue(None, from_user, "⚠️ 语音接收失败，请重发或改用文字")
    for fi in file_items:
        try:
            if ilink is None:
                raise MediaError("iLink 连接不可用")
            name = str(fi.get("file_name") or "file.bin")
            ext = Path(name).suffix.lstrip(".") or "bin"
            p = await download_inbound_media(ilink, fi.get("media") or {},
                                             in_dir, "file", ext)
            size_mb = Path(p).stat().st_size / 1048576
            media_lines.append(
                f"[用户发来文件 {name}（{size_mb:.1f}MB），已保存到 {p}，请查看处理]")
            if media_path is None:
                media_path = p
        except Exception as e:
            log.warning("入站文件处理失败: %r", e)
            db.enqueue(None, from_user, f"⚠️ 文件接收失败（{e}），请重发")
    for vd in video_items:
        try:
            if ilink is None:
                raise MediaError("iLink 连接不可用")
            p = await download_inbound_media(ilink, vd.get("media") or {},
                                             in_dir, "vid", "mp4")
            media_lines.append(
                f"[用户发来视频，已保存到 {p}，请查看处理"
                f"（如需看内容可用 ffmpeg 抽帧，未装则如实告知）]")
            if media_path is None:
                media_path = p
        except Exception as e:
            log.warning("入站视频处理失败: %r", e)
            db.enqueue(None, from_user, f"⚠️ 视频接收失败（{e}），请重发")
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

    # /delete Y/N 确认拦截：桥命令 /delete 预置 delete_confirm:<user> 后才真删。
    # 排在审批之后（两者并存时审批优先，delete 门下一轮仍有效）。
    pending_del = db.get_state(f"delete_confirm:{from_user}")
    if pending_del and text.strip().upper() in ("Y", "N"):
        db.delete_state(f"delete_confirm:{from_user}")
        if text.strip().upper() == "Y":
            try:
                spec = json.loads(pending_del)
            except ValueError:
                spec = None
            if spec and spec.get("type") == "session":
                n = db.delete_session_rows(int(spec["id"]))
                db.enqueue(None, from_user,
                           f"🗑️ 已删除话题（连同 {n} 个任务记录）。" if n >= 0
                           else "该话题已不存在。")
            elif spec and spec.get("type") == "task":
                ok = db.delete_task_rows(int(spec["id"]))
                db.enqueue(None, from_user,
                           f"🗑️ 已删除任务 #{spec['id']}。" if ok else "该任务已不存在。")
            db.audit("delete", f"user={from_user} spec={pending_del}")
        else:
            db.enqueue(None, from_user, "已取消删除。")
        if outbound:
            outbound.notify()
        return

    if ((image_items or voice_items or file_items or video_items)
            and not text and not media_lines):
        return   # 纯媒体无可用内容（全部下载失败/纯语音存档回执）：已有 ⚠️ 回执，
        # 不建任务（防空文本进路由——route("") 判 chat 会建空 prompt 任务）
    if media_lines and not text:
        # 纯媒体消息：不走路由（空文本无命令语义），直接 chat 任务——媒体即对话
        session = db.get_active_binding(from_user, cfg.default_cwd)
        await _flush_merge_pending(db, cfg, pool, outbound, from_user)
        db.create_task(None, session.id, "\n".join(media_lines), kind="chat")
        db.enqueue(None, from_user, "✅ 收到媒体，处理中")
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
        if r.kind == "chat" and not media_lines and _merge_window_s(cfg) is not None:
            # M5C1：纯文本进合并窗口（不立即建任务）；语音转写并入 text_parts
            # 同样走此路径（语义即用户文字）。合并禁用（merge_window_s<=0 或
            # throttle 缺失）时落 else 立即建任务，保持既有行为零回归。
            await _append_merge_pending(db, cfg, pool, outbound, from_user,
                                        text, msg_key)
        else:
            # forward（slash 转发）或 chat-with-media 或合并禁用：先 flush 暂存
            # （序不倒）再建任务
            await _flush_merge_pending(db, cfg, pool, outbound, from_user)
            prompt = text if r.kind == "chat" else f"/{r.command} {r.args}".strip()
            if media_lines:
                prompt += "\n" + "\n".join(media_lines)
            db.create_task(None, session.id, prompt, kind=r.kind)
            db.enqueue(None, from_user, "✅ 收到，处理中")
            if pool:
                await pool.submit_check()
    if outbound:
        outbound.notify()


async def poll_loop(db, cfg, ilink, pool, outbound, token_ref) -> None:
    """iLink 长轮询收消息。token 失效的两条路径都触发清空重连：
    HTTP 401/403 异常、应用层 errcode/ret = -14（官方语义 "session timeout"，
    HTTP 仍 200——openclaw-weixin README/monitor.js 实证，只盯 401 会在
    token 真死时静默空转）。"""
    buf = db.get_state("get_updates_buf", "")
    fails = 0
    stale = 0

    def _kill_token(reason: str) -> None:
        # 幂等门：token 已被清空后每轮失败不再重复清/重复告警
        if not token_ref["token"]:
            return
        log.error("bot_token 已失效（%s），清空等待重新登录", reason)
        db.set_state("bot_token", "")
        token_ref["token"] = ""
        # 监控告警（M2）：连接失效推全部白名单用户（自动重连随
        # ReconnectTimer 的空 token 兜底启动；enqueue 同步 DB 写安全）
        for user in sorted(getattr(cfg, "whitelist", None) or ()):
            db.enqueue(None, user,
                       "⚠️ 微信连接已失效（" + reason + "），正在自动重连——"
                       "可能需要重新扫码（终端/二维码见服务器）")
        if outbound:
            outbound.notify()

    while True:
        try:
            result = await ilink.getupdates(buf, token_ref["token"],
                                            token_ref["base_url"] or None)
        except Exception as e:
            fails += 1
            log.warning("getupdates 失败（连续 %d 次），5s 后重试: %s", fails, e)
            # 精确匹配 "HTTP 401/403"（ILinkError 文案形如 "POST ... HTTP 401: ..."），
            # 避免响应体里恰含 "401" 子串的普通错误误清仍有效的 token。
            if fails >= 5 and ("HTTP 401" in str(e) or "HTTP 403" in str(e)):
                _kill_token("连续 401/403")
            await asyncio.sleep(5)
            continue
        fails = 0
        # 应用层 -14：ret 正常为 0，errcode 仅错误时出现（可能为 null）。
        # 连续 5 次防抖（≈25s）与 401 路径一致，防服务端瞬时抖动误杀活 token。
        code = result.get("errcode")
        if code is None:
            code = result.get("ret")
        if code == -14:
            stale += 1
            log.warning("getupdates 返回 -14 session timeout（连续 %d 次）", stale)
            if stale >= 5:
                _kill_token("session timeout -14")
            await asyncio.sleep(5)
            continue
        stale = 0
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

    # M5C1：合并窗口崩溃恢复——残留 merge_pending KV 逐个 flush
    recovered = db.scan_merge_pending()
    for user, raw in recovered:
        try:
            await _flush_merge_pending(db, cfg, None, None, user, recover=True)
        except Exception as e:
            log.warning("合并窗口恢复失败 user=%s: %r", user, e)
    if recovered:
        log.info("崩溃恢复：恢复 %d 个合并窗口暂存", len(recovered))

    # ---- 版本探测（TRD §11 版本漂移对策）：实测版本 vs EXPECTED_CLAUDE_VERSION，
    # 漂移/失败只 audit+warning 不阻断（fail-open）。放启动一次性，不进
    # TaskRunner.__init__（测试大量构造 runner，避免普遍子进程开销）。
    from worker.version import check_claude_version
    await check_claude_version(db, cfg)

    # ---- media 过期清理（M3 审查追加项）：启动清一次；日常由出站循环日界滚动
    # 搭车（outbound._media_cleanup_once）。未终态 outbox 行引用的文件受保护。
    from gateway.media import cleanup_expired_media
    if cfg.media_retention_days > 0:
        n = await asyncio.to_thread(cleanup_expired_media, cfg.repo_root,
                                    cfg.media_retention_days,
                                    db.active_media_paths())
        if n:
            db.audit("media_cleanup", f"startup removed={n}")
            log.info("启动 media 清理：%d 个过期文件", n)

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

        from gateway.scheduler import scheduler_loop
        from gateway.notify_http import run_notify_http
        tasks = [
            asyncio.create_task(pool.run_forever(), name="worker-pool"),
            asyncio.create_task(outbound.run_forever(), name="outbound"),
            asyncio.create_task(ReconnectTimer(db, cfg, ilink, token_ref,
                                               typing_state, outbound).run_forever(),
                                name="reconnect"),
            asyncio.create_task(poll_loop(db, cfg, ilink, pool, outbound, token_ref),
                                name="poll"),
            asyncio.create_task(scheduler_loop(db, cfg), name="scheduler"),
            asyncio.create_task(run_notify_http(db, cfg), name="notify-http"),
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
