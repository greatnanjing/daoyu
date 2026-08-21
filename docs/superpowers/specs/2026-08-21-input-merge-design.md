# 刀鱼 M5C1：入站文本体验（连发合并 + 队列感知 ACK）设计

- **日期**: 2026-08-21
- **状态**: 设计已确认（brainstorm 对话结论沉淀），待实现
- **配套文档**: [PRD.md](../../PRD.md) / [TRD.md](../../TRD.md) / [M5B 媒体 spec](2026-08-21-media2-design.md)
- **背景**: M5B 完成后推进第三方向「输入体验增强」的第一子项。范围经 brainstorm 选定：**纯文本连发消息合并为一个 prompt**（窗口暂存）+ **B 运行中追加的可达部分**（ACK 队列位次，非真注入——结构限制见 §6）。M5C2 出站可读性、M5C3 快捷命令为后续独立 spec。

---

## 1. 背景与决策记录

| 问题 | 结论 |
|---|---|
| 合并窗口持久化 | **DB-state-KV 持久化**（`merge_pending:<user>`）——崩溃恢复：启动扫描残留 KV 逐个 flush（消息已在 messages 表不丢）。弃纯内存 dict（ACK 已发但无任务、重启静默丢） |
| 窗口适用范围 | **仅纯文本 chat 消息**进窗口；forward（slash 转发）、bridge/ilink/proxy、Y-N 拦截、媒体消息——先 flush 该用户暂存再走原路径（避免与暂存竞态） |
| 窗口时长 | `throttle.merge_window_s` 默认 2.0s（进 /config set 白名单，throttle 节同档调） |
| ACK 策略 | 首条即时 ACK（合并意图）+ 窗口内静默 + flush ACK「已合并 N 条」+ 队列位次。弃静默到 flush（2s 静默在微信往返延迟叠加下像丢消息） |
| B 运行中追加 | **真·中途注入结构不可**（claude `-p` 读一次 stdin 即跑完，[worker/runner.py:226-231](../../../worker/runner.py#L226-L231) `proc.stdin.close()`）；可达部分 = ACK 队列位次语义（「追加=下一轮」是结构现实，诚实表达）。新任务查 session 已有 pending/running 计数追加位次信息 |
| text+media 合并 | **不做**——媒体消息先 flush 暂存文本任务再走 M5B 即时媒体任务路径（两任务、序不竞态）。跨类合并为三期留档 |
| 永不阻塞不变量 | 窗口延迟只等「后续消息」不等 Claude；首条即时 ACK 保反馈。gateway 仍绝不等 Claude（核心架构不变） |

## 2. 总体架构

```
chat 纯文本消息到达（handle_inbound）
  ├─ 该用户 merge_pending KV 在？ ─┬─ 在：追加 texts、重置计时、静默
  │                                └─ 不在：建 KV + 调度 merge_window_s 后 flush
  └─ slash/媒体/桥命令/Y-N 先 flush 该用户暂存（建文本任务+ACK）再走原路径

flush（计时到期 / 被先 flush / 启动恢复）:
  拼 texts → create_task（session = active binding）→ 队列感知 ACK → 清 KV
```

关键约束：

- 窗口是**每用户独立**的 buffer（单用户产品无所谓多用户，但语义正确）。
- flush 的 `session_id` = buffer 建立时的 `get_active_binding(from_user, default_cwd)`（首条到达时确定，窗口内 `/cd` 切话题不影响已暂存 batch——切话题前先 flush 是更稳妥语义，见 §3.2）。
- 启动恢复：`main_async` 崩溃恢复段扫描 `state` 表 `merge_pending:*` 键，逐个 flush（窗口已过，立即建任务 + ACK「已恢复 N 条暂存消息，开始处理」）。
- 计时用 asyncio `loop.call_later`——gateway 单事件循环，计时器在内存（重启即丢，但 KV 还在，启动恢复兜底；正常运行无计时器不 flush 的悬挂——计时器随主循环存活）。

## 3. 组件设计

### 3.1 `common/db.py`（KV + helper 扩展）

| 项 | 设计 |
|---|---|
| `merge_pending:<user>` KV | JSON `{"texts": [str,...], "session_id": int, "first_msg_id": str, "started_at": int}`。`set_state`/`get_state`/`delete_state` 既有机制复用 |
| `pending_task_count(session_id: int) -> int` | `SELECT COUNT(*) FROM tasks WHERE session_id=? AND state IN ('pending','running')`——B 队列位次用（不含即将创建的本条） |
| `scan_merge_pending() -> list[tuple[user, value]]` | `SELECT key, value FROM state WHERE key LIKE 'merge_pending:%'`——启动恢复扫描 |

### 3.2 入站路由（[gateway/app.py](../../../gateway/app.py) `handle_inbound`）

chat/forward 路径（现 [app.py:251-257](../../../gateway/app.py#L251-L257)）改造：

- **纯 chat 文本**（`r.kind == "chat"` 且无 media_lines）→ 进合并窗口（§3.3）；不立即 create_task。
- **forward / 带媒体 / 其余路径**：先 `await _flush_merge_pending(db, cfg, pool, outbound, from_user)`（若该用户有暂存）再走原路径——避免暂存与新即时任务竞态。
- Y-N 拦截（reconnect/approval/delete）与 bridge/ilink/proxy/unknown 同样先 flush 暂存再执行（顺序保证暂存文本不丢）。
- `/cd` 切话题：桥命令执行前先 flush（保证暂存 batch 归当前话题而非切后的）。

### 3.3 合并窗口逻辑（[gateway/app.py](../../../gateway/app.py) 新模块级函数）

```
_pending_timers: dict[str, asyncio.TimerHandle] = {}   # user -> handle（内存；重启丢，KV 兜底）

async def _append_merge_pending(db, cfg, pool, outbound, from_user, text, msg_id):
    """纯 chat 文本进窗口：KV 在则追加+重置计时；不在则建+调度 flush。"""
    key = f"merge_pending:{from_user}"
    cur = db.get_state(key)
    if cur:
        data = json.loads(cur)
        data["texts"].append(text)
        db.set_state(key, json.dumps(data, ensure_ascii=False))
    else:
        session = db.get_active_binding(from_user, cfg.default_cwd)
        data = {"texts": [text], "session_id": session.id,
                "first_msg_id": msg_id, "started_at": int(time.time())}
        db.set_state(key, json.dumps(data, ensure_ascii=False))
        db.enqueue(None, from_user,
                   f"✅ 收到，正在合并后续消息"
                   f"（{cfg.throttle.get('merge_window_s', 2.0):.0f}s 内无新增即开始处理）")
        if outbound: outbound.notify()
    _schedule_flush(db, cfg, pool, outbound, from_user)

def _schedule_flush(db, cfg, pool, outbound, from_user):
    """重置/设置该用户的 flush 计时器（call_later）。"""
    window = float(cfg.throttle.get("merge_window_s", 2.0))
    loop = asyncio.get_event_loop()
    old = _pending_timers.pop(from_user, None)
    if old: old.cancel()
    _pending_timers[from_user] = loop.call_later(
        window, lambda: asyncio.create_task(
            _flush_merge_pending(db, cfg, pool, outbound, from_user)))

async def _flush_merge_pending(db, cfg, pool, outbound, from_user):
    """flush：拼 texts → create_task → 队列感知 ACK → 清 KV/计时。无暂存则空操作。"""
    _pending_timers.pop(from_user, None)
    key = f"merge_pending:{from_user}"
    raw = db.get_state(key)
    if not raw:
        return
    data = json.loads(raw)
    db.delete_state(key)
    session = db.get_session(data["session_id"]) or db.get_active_binding(from_user, cfg.default_cwd)
    prompt = "\n".join(data["texts"])
    db.create_task(None, session.id, prompt, kind="chat")
    pos = db.pending_task_count(session.id)   # 含刚建的这条
    ack = f"✅ 已合并 {len(data['texts'])} 条消息，开始处理"
    if pos > 1:
        ack += f"（当前任务完成后接上，你排在第 {pos} 位）"
    db.enqueue(None, from_user, ack)
    if pool: await pool.submit_check()
    if outbound: outbound.notify()
```

### 3.4 启动恢复（[gateway/app.py](../../../gateway/app.py) `main_async` 崩溃恢复段）

`reset_running_tasks()` 之后加：

```python
for user, raw in db.scan_merge_pending():
    data = json.loads(raw)
    session = db.get_session(data["session_id"])
    if session:
        db.create_task(None, session.id, "\n".join(data["texts"]), kind="chat")
        db.enqueue(None, user, f"✅ 已恢复 {len(data['texts'])} 条暂存消息，开始处理")
        db.audit("merge_recover", f"user={user} count={len(data['texts'])}")
    db.delete_state(f"merge_pending:{user}")
if outbound: ...  # notify
```

（启动期 outbound/pool 尚未构造——恢复任务的 ACK 入 outbox、任务入 pending 队列，gateway 起来后 outbound 首轮投递、pool 首轮 `poll_interval_s` 自然拾取，无需 submit_check/notify。）

### 3.5 配置

`common/config.py` `_DEFAULT_THROTTLE` 加 `"merge_window_s": 2.0`；`gateway/config.example.json` throttle 节同键默认；`gateway/proxy.py` `CONFIG_KEYS` 白名单加 `throttle.merge_window_s`（重启生效，与既有 throttle 键同口径）。

## 4. 测试策略

| 层 | 内容 |
|---|---|
| db 层 | `pending_task_count`（含/不含 pending/running、终态不计）；`scan_merge_pending`（多用户、无残留） |
| 窗口 | 首条 ACK 合并意图 + KV 建立；窗口内追加静默+重置计时；flush 拼 texts+create_task+清 KV；窗口内 slash 先 flush 再执行；媒体消息先 flush 再建媒体任务；`/cd` 先 flush |
| ACK | flush ACK「已合并 N 条」；pos>1 追加位次；pos==1 不追加 |
| 启动恢复 | scan_merge_pending 残留 → create_task + ACK + 清 KV + audit；session 已删则跳过不炸 |
| E2E | 连发两条 chat（window=短）→ 单任务 prompt 含两段 → fake claude 跑完；slash 在窗口内 → flush 文本任务后再执行 slash |
| 回归 | 单条 chat 行为不变（窗口 flush 单条 prompt == 原文）；既有图片/媒体路径不受影响（先 flush 暂存再走） |

## 5. 风险与真机验收点

| 风险 | 缓解 |
|---|---|
| 窗口 2s 静默像丢消息 | 首条即时 ACK 合并意图；窗口可经 /config set 调（merge_window_s） |
| 计时器与主循环同生命周期 | call_later 挂 asyncio loop；重启 KV 兜底 + 启动恢复 |
| 窗口内 `/cd` 切话题 | 切话题前先 flush（暂存归切前话题） |
| flush 时 session 已删 | get_session None → 回退 active binding（[app.py:253](../../../gateway/app.py#L253) 先例），不炸 |

## 6. 明确不做

- **运行中真·中途注入**（claude `-p` stdin 一次性关闭，结构不可——见 [runner.py:226-231](../../../worker/runner.py#L226-L231)；B 仅 ACK 语义）
- bg blocked 会话喂输入（daemon 持有、`--resume` 被拒，M3 实测）
- text+media 跨类合并为单任务（三期留档）
- 自适应窗口时长（按消息节奏动态调整）
- 多用户并发窗口语义（单用户，per-user 已正确）
