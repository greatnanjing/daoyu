# M5C1 入站文本体验 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 纯文本连发消息合并为一个 prompt（per-user KV 暂存窗口 + 启动恢复）+ 新任务 ACK 队列位次感知（真中途注入结构不可，仅语义）。

**Architecture:** handle_inbound 的 chat-text-no-media 路径改为进合并窗口（不立即建任务）；task-creating 的非 chat 路径（forward/媒体）先 flush 该用户暂存再建任务；flush = 拼 texts→create_task→队列 ACK→清 KV。窗口 KV 持久化（`merge_pending:<user>`）+ asyncio call_later 计时；启动扫描残留 KV 恢复。

**Tech Stack:** Python 3.11+ / asyncio / SQLite WAL。

**Spec:** [docs/superpowers/specs/2026-08-21-input-merge-design.md](../specs/2026-08-21-input-merge-design.md)

## Global Constraints

- **真·中途注入结构不可**（[worker/runner.py:226-231](../../../worker/runner.py#L226-L231) `proc.stdin.close()`）——B 仅 ACK 队列位次语义，不造注入通道。
- 窗口仅作用于**纯文本 chat 消息**（无媒体、非 slash、非 Y-N 拦截）；text 含语音转写时也算纯文本（语义同）。
- **flush-first 仅在 task-creating 非 chat 路径**（forward / 媒体即对话 / 纯媒体任务）——spec §3.2 原述「slash/Y-N 先 flush」经实现分析收窄：Y-N/bridge/proxy 不建任务，窗口计时器自行 flush；session_id 在 append 时锁定故 `/cd` 不影响已暂存 batch。此收窄在计划内，spec 口径以此为准。
- 首条即时 ACK「✅ 收到，正在合并后续消息（Ns 内无新增即开始处理）」+ 窗口内静默 + flush ACK「✅ 已合并 N 条消息，开始处理」（pos>1 追加「（当前任务完成后接上，排在第 M 位）」）。
- 崩溃恢复：启动扫描 `merge_pending:*` KV 逐个 flush（session 已删则跳过不炸）+ audit。
- 窗口默认 `throttle.merge_window_s = 2.0`（进 /config set 白名单，重启生效）。
- 永不阻塞不变量：窗口只等后续消息不等 Claude；首条即时 ACK 保反馈。
- 测试基线：**423 全绿**（`python -m pytest`）；Windows venv `.venv/Scripts/python`。
- 文档/注释/文案简体中文；代码标识符英文。

---

### Task 1: db helpers（pending_task_count + scan_merge_pending）

**Files:**
- Modify: `common/db.py`（state 区之后加两方法）
- Test: `tests/test_db.py`（追加）

**Interfaces:**
- Consumes: 既有 `state` 表（key/value/updated_at）、`tasks` 表（session_id/state）。
- Produces: `db.pending_task_count(session_id: int) -> int`（含本条之前已有的 pending/running，不含终态）；`db.scan_merge_pending() -> list[tuple[str, str]]`（`[(user, value_json), ...]`，user = key 去前缀）。

- [ ] **Step 1: Write the failing tests**

`tests/test_db.py` 追加：

```python
def test_pending_task_count(db):
    """pending/running 计数；终态不计。"""
    db.insert_message(InboundMessage(msg_id="m1", from_user="u@im.wechat",
                                     text="hi", context_token="c", received_at=1))
    s = db.get_or_create_session("u@im.wechat", "/repo")
    t1 = db.create_task(None, s.id, "a", kind="chat")
    assert db.pending_task_count(s.id) == 1
    t2 = db.create_task(None, s.id, "b", kind="chat")
    assert db.pending_task_count(s.id) == 2
    db.mark_done(t1)
    assert db.pending_task_count(s.id) == 1          # done 不计
    # running 仍计
    db._conn.execute("UPDATE tasks SET state='running' WHERE id=?", (t2,))
    db._conn.commit()
    assert db.pending_task_count(s.id) == 1
    # 其他 session 不串
    s2 = db.get_or_create_session("u@im.wechat", "/other")
    assert db.pending_task_count(s2.id) == 0


def test_scan_merge_pending(db):
    """扫描 merge_pending:* KV，返回 (user, value) 列表。"""
    assert db.scan_merge_pending() == []
    db.set_state("merge_pending:a@im.wechat", '{"texts":["x"]}')
    db.set_state("merge_pending:b@im.wechat", '{"texts":["y","z"]}')
    db.set_state("other_key", "noise")              # 非 merge_pending 前缀不收
    found = dict(db.scan_merge_pending())
    assert found == {"a@im.wechat": '{"texts":["x"]}',
                     "b@im.wechat": '{"texts":["y","z"]}'}
```

（`InboundMessage` 需在 test_db.py 顶部已 import——若无则补 `from common.models import InboundMessage`。`mark_done` 与 `get_or_create_session` 为既有方法，先 grep 确认方法名准确——若 mark 终态方法名不同（如 `mark_done`/`complete_task`）按实际调用。）

- [ ] **Step 2: Run to verify failures**

Run: `python -m pytest tests/test_db.py -v -k "pending_task_count or scan_merge_pending"`
Expected: FAIL（`AttributeError: ... has no attribute 'pending_task_count'`）

- [ ] **Step 3: Implement**

`common/db.py` `delete_state` 方法之后加：

```python
    def pending_task_count(self, session_id: int) -> int:
        """该 session 已有 pending/running 任务数（B 队列位次：不含即将创建的本条）。"""
        row = self._conn.execute(
            "SELECT COUNT(*) c FROM tasks "
            "WHERE session_id=? AND state IN ('pending','running')",
            (session_id,)).fetchone()
        return row["c"] if row else 0

    def scan_merge_pending(self) -> list[tuple[str, str]]:
        """扫描所有 merge_pending:<user> KV（启动崩溃恢复用）。返回 [(user, value_json)]。"""
        prefix = "merge_pending:"
        rows = self._conn.execute(
            "SELECT key, value FROM state WHERE key LIKE ?", (prefix + "%",)
        ).fetchall()
        return [(r["key"][len(prefix):], r["value"]) for r in rows]
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_db.py -v -k "pending_task_count or scan_merge_pending"`
Expected: 2 PASS

- [ ] **Step 5: Commit**

```bash
git add common/db.py tests/test_db.py
git commit -m "feat(M5C1): db helpers——pending_task_count 队列位次 + scan_merge_pending 启动恢复扫描"
```

---

### Task 2: 配置（merge_window_s 默认 + CONFIG_KEYS 白名单）

**Files:**
- Modify: `common/config.py:15-16`（`_DEFAULT_THROTTLE` 加键）
- Modify: `gateway/config.example.json`（throttle 节加键）
- Modify: `gateway/proxy.py:17-22`（CONFIG_USAGE 串）、`gateway/proxy.py:288-292`（CONFIG_KEYS 白名单）、概览列表（约 334-336 行）
- Test: `tests/test_config.py`（追加）

**Interfaces:**
- Consumes: 既有 `_DEFAULT_THROTTLE` 合并机制、CONFIG_KEYS 扁平 dotted 键结构。
- Produces: `cfg.throttle["merge_window_s"]`（默认 2.0）；`/config set throttle.merge_window_s <值>` 可改（重启生效）。

- [ ] **Step 1: Write the failing tests**

`tests/test_config.py` 追加：

```python
def test_load_config_merge_window_default(tmp_path):
    _write_config(tmp_path, {})
    cfg = load_config(tmp_path)
    assert cfg.throttle["merge_window_s"] == 2.0
    _write_config(tmp_path, {"throttle": {"merge_window_s": 0.5}})
    cfg = load_config(tmp_path)
    assert cfg.throttle["merge_window_s"] == 0.5
```

`tests/test_proxy.py` 扩既有两用例（不新建测试）——`test_config_set_all_seven_keys`（约 455 行 cases 列表）加一行：

```python
        ("throttle.merge_window_s", "0.5", 0.5),
```

并在其末尾断言区（约 469-475 行）加 `assert doc["throttle"]["merge_window_s"] == 0.5`。

`test_config_set_rejects_out_of_range`（约 517 行 bad 列表）加一行：

```python
           ("throttle.merge_window_s", "0"),            # > 0
```

- [ ] **Step 2: Run to verify failures**

Run: `python -m pytest tests/test_config.py tests/test_proxy.py -v`
Expected: config 测试 FAIL（`merge_window_s` 键不在默认）

- [ ] **Step 3: Implement**

`common/config.py` `_DEFAULT_THROTTLE`（第 15-16 行）加键：

```python
_DEFAULT_THROTTLE = {"min_send_interval_s": 1.0, "progress_window_s": 2.5,
                     "page_char_limit": 2000, "daily_send_limit": 500,
                     "merge_window_s": 2.0}
```

`gateway/config.example.json` throttle 节加：

```json
    "merge_window_s": 2.0,
```

`gateway/proxy.py` CONFIG_KEYS（约 288-292 行）加：

```python
    "throttle.merge_window_s": (float, lambda v: v > 0, "数值"),
```

CONFIG_USAGE 串（约 17-22 行）throttle 键列表加 `merge_window_s`：

```python
CONFIG_USAGE = ("用法：/config — 概览；/config set <键> <值>（可改键："
                "throttle.min_send_interval_s/progress_window_s/"
                "page_char_limit/daily_send_limit/merge_window_s、budget.max_turns/max_usd、"
                "worker.concurrency、cron.disk_threshold_pct/cpu_threshold_pct/"
                "mem_threshold_pct/load_sustain_min/cert_warn_days/"
                "alert_silence_h/queue_backlog_warn；重启生效）")
```

概览列表（约 334-336 行 `thr = " · ".join(...)` 处）若逐键枚举则加 `merge_window_s` 行；若按 `throttle.get(key, '默认')` 循环则补该键到枚举。

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_config.py tests/test_proxy.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add common/config.py gateway/config.example.json gateway/proxy.py \
        tests/test_config.py tests/test_proxy.py
git commit -m "feat(M5C1): merge_window_s 配置——throttle 默认 2.0 + /config set 白名单"
```

---

### Task 3: 合并窗口逻辑 + 入站路由改造 + 启动恢复

**Files:**
- Modify: `gateway/app.py`（模块级 `_pending_timers` + `_append_merge_pending`/`_schedule_flush`/`_flush_merge_pending` 三函数；`handle_inbound` chat 分支改 append、forward/媒体分支加 flush-first；`main_async` 崩溃恢复段加 scan_merge_pending 恢复）
- Test: `tests/test_merge.py`（新建）

**Interfaces:**
- Consumes: Task 1 的 `pending_task_count`/`scan_merge_pending`；Task 2 的 `cfg.throttle["merge_window_s"]`；既有 `db.get_state`/`set_state`/`delete_state`/`get_active_binding`/`get_session`/`create_task`/`enqueue`。
- Produces: 模块级 `_pending_timers: dict[str, asyncio.TimerHandle]`；`_flush_merge_pending(db, cfg, pool, outbound, from_user)`（供 flush-first 与计时器调用）；启动恢复行为。

- [ ] **Step 1: Write the failing tests**

`tests/test_merge.py` 新建：

```python
"""M5C1 连发消息合并窗口：纯文本聚合为一个 prompt；flush-first；启动恢复。"""
import asyncio
import json
import time

from common.db import Database
from common.models import InboundMessage
from gateway.app import (_flush_merge_pending, _append_merge_pending,
                         handle_inbound)

USER = "u@im.wechat"


class Cfg:
    def __init__(self, tmp_path, window=2.0):
        self.repo_root = tmp_path
        self.whitelist = {USER}
        self.default_cwd = str(tmp_path)
        self.throttle = {"min_send_interval_s": 0.0, "progress_window_s": 0.0,
                         "page_char_limit": 2000, "daily_send_limit": 500,
                         "merge_window_s": window}


def _msg(msg_id, text):
    return {"message_id": msg_id, "seq": msg_id, "from_user_id": USER,
            "message_type": 1, "context_token": "CTX",
            "item_list": [{"type": 1, "text_item": {"text": text}}]}


def _task_prompts(db):
    return [r["prompt"] for r in db._conn.execute(
        "SELECT prompt FROM tasks ORDER BY id")]


def _outbox_texts(db):
    return [r["text"] for r in db._conn.execute("SELECT text FROM outbox ORDER BY id")]


async def test_merge_two_text_messages_single_task(tmp_path):
    db = Database(tmp_path / "t.db"); db.ensure_schema()
    cfg = Cfg(tmp_path, window=0.05)
    await handle_inbound(db, cfg, None, None, _msg(1, "第一步"), ilink=None)
    await handle_inbound(db, cfg, None, None, _msg(2, "第二步"), ilink=None)
    assert _task_prompts(db) == []                      # 窗口内未建任务
    assert any("正在合并" in t for t in _outbox_texts(db))   # 首条 ACK
    await asyncio.sleep(0.15)                           # 过窗口
    prompts = _task_prompts(db)
    assert len(prompts) == 1 and prompts[0] == "第一步\n第二步"
    assert any("已合并 2 条" in t for t in _outbox_texts(db))


async def test_single_message_flushes_on_window(tmp_path):
    db = Database(tmp_path / "t.db"); db.ensure_schema()
    cfg = Cfg(tmp_path, window=0.05)
    await handle_inbound(db, cfg, None, None, _msg(1, "单条"), ilink=None)
    await asyncio.sleep(0.15)
    assert _task_prompts(db) == ["单条"]
    assert any("已合并 1 条" in t for t in _outbox_texts(db))


async def test_queue_position_in_ack(tmp_path):
    """B：已有 pending 任务时 flush ACK 追加队列位次。"""
    db = Database(tmp_path / "t.db"); db.ensure_schema()
    s = db.get_or_create_session(USER, str(tmp_path))
    db.create_task(None, s.id, "前序任务", kind="chat")    # 已有 1 pending
    cfg = Cfg(tmp_path, window=0.05)
    await handle_inbound(db, cfg, None, None, _msg(1, "新消息"), ilink=None)
    await asyncio.sleep(0.15)
    assert any("排在第 2 位" in t for t in _outbox_texts(db))


async def test_slash_command_flushes_pending_first(tmp_path):
    """forward（slash 转发）先 flush 暂存文本任务再建自身任务——序不倒。"""
    db = Database(tmp_path / "t.db"); db.ensure_schema()
    db.set_state("slash_commands", json.dumps(["review"]))
    cfg = Cfg(tmp_path, window=0.05)
    await handle_inbound(db, cfg, None, None, _msg(1, "上下文"), ilink=None)
    await handle_inbound(db, cfg, None, None, _msg(2, "/review"), ilink=None)
    prompts = _task_prompts(db)
    assert prompts[0] == "上下文"          # flush 在前
    assert prompts[1] == "/review"         # slash 任务在后
    await asyncio.sleep(0.15)              # 计时器已无悬挂 flush（slash 先 flush 清了 KV）


async def test_startup_recovery_flushes_pending(tmp_path):
    """崩溃恢复：残留 merge_pending KV → 启动 create_task + ACK + audit + 清 KV。"""
    db = Database(tmp_path / "t.db"); db.ensure_schema()
    s = db.get_or_create_session(USER, str(tmp_path))
    db.set_state(f"merge_pending:{USER}", json.dumps(
        {"texts": ["遗留1", "遗留2"], "session_id": s.id,
         "first_msg_id": "x", "started_at": int(time.time())}))
    # 直接调 flush（recover=True 等价于 main_async 启动恢复的逐条 flush 调用）
    await _flush_merge_pending(db, Cfg(tmp_path), None, None, USER, recover=True)
    assert _task_prompts(db) == ["遗留1\n遗留2"]
    assert any("已恢复 2 条" in t for t in _outbox_texts(db))
    assert db.get_state(f"merge_pending:{USER}") is None
    assert any(r["kind"] == "merge_recover" for r in db._conn.execute(
        "SELECT kind FROM audit_log"))
```

注：`test_startup_recovery_flushes_pending` 调 `_flush_merge_pending` 直接验证恢复路径（等价于 `main_async` 启动恢复里的逐条 flush 调用）。ACK 文案「已恢复 N 条」与运行时 flush 的「已合并 N 条」区分——恢复路径用「恢复」措辞（见 Step 3 的恢复函数实现；若实现把恢复也走 `_flush_merge_pending` 通用路径则文案统一「已合并」，测试断言随之调整）。

- [ ] **Step 2: Run to verify failures**

Run: `python -m pytest tests/test_merge.py -v`
Expected: FAIL（`ImportError: cannot import name '_flush_merge_pending'`）

- [ ] **Step 3: Implement app.py**

(a) 模块级（`handle_inbound` 之前）加计时器字典与三函数：

```python
_pending_timers: dict[str, asyncio.TimerHandle] = {}


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
```

```python
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
```

(b) `handle_inbound` 的 `else: # chat / forward` 分支（约 251-257 行）改造：

```python
    else:  # chat / forward
        session = db.get_active_binding(from_user, cfg.default_cwd)   # 当前话题指针
        if r.kind == "chat" and not media_lines:
            # M5C1：纯文本进合并窗口（不立即建任务）；语音转写并入 text_parts
            # 同样走此路径（语义即用户文字）
            await _append_merge_pending(db, cfg, pool, outbound, from_user,
                                        text, msg_key)
        else:
            # forward（slash 转发）或 chat-with-media：先 flush 暂存（序不倒）再建任务
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
```

（chat-with-media 与 forward 的「先 flush」共用 `_flush_merge_pending`；纯媒体任务段（约 218-227 行）也加 flush-first——在该 `db.create_task` 前一行加 `await _flush_merge_pending(db, cfg, pool, outbound, from_user)`。）

(c) `main_async` 崩溃恢复段（`reset_running_tasks()` 之后、版本探测之前）加：

```python
    # M5C1：合并窗口崩溃恢复——残留 merge_pending KV 逐个 flush
    recovered = db.scan_merge_pending()
    for user, raw in recovered:
        try:
            await _flush_merge_pending(db, cfg, None, None, user, recover=True)
        except Exception as e:
            log.warning("合并窗口恢复失败 user=%s: %r", user, e)
    if recovered:
        log.info("崩溃恢复：恢复 %d 个合并窗口暂存", len(recovered))
```

（启动期 pool/outbound 未构造——传 None；flush 内 `if pool/outbound` 守卫跳过 submit/notify；任务入 pending 队列、ACK 入 outbox，gateway 起来后自然投递/调度。）

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_merge.py tests/test_inbound_media.py tests/test_e2e.py -v`
Expected: 全 PASS（M3 图片/M5B 媒体路径不受影响——先 flush 暂存为空时 `_flush_merge_pending` 空操作）

- [ ] **Step 5: Commit**

```bash
git add gateway/app.py tests/test_merge.py
git commit -m "feat(M5C1): 连发合并窗口（KV 持久化+asyncio 计时+启动恢复）+ chat 路径 reroute + flush-first"
```

---

### Task 4: E2E + 文档同步

**Files:**
- Modify: `tests/test_e2e.py`（追加连发合并 E2E）
- Modify: `README.md`（日常使用节加连发合并说明）
- Modify: `CLAUDE.md`（当前状态 + M5C1 清单 + 组件 + 测试数 + 硬约束补录）
- Modify: `docs/superpowers/specs/2026-08-21-input-merge-design.md`（状态行 + §3.2 收窄注记）

**Interfaces:**
- Consumes: Task 1-3 全部。

- [ ] **Step 1: E2E**

`tests/test_e2e.py` 追加（复用既有 `FakeCfg`，给其 throttle 加 `merge_window_s`）——先 Read 该文件 `FakeCfg.__init__`，在 `self.throttle = {...}` 加 `"merge_window_s": 0.05`（测试用短窗口）。末尾追加：

```python
async def test_e2e_merge_two_messages_single_task(tmp_path, monkeypatch):
    """M5C1 E2E：连发两条 chat → 单任务 prompt 含两段 → fake claude 跑完。"""
    cfg = FakeCfg(tmp_path, monkeypatch)
    cfg.throttle["merge_window_s"] = 0.05
    db = Database(tmp_path / "e2e.db"); db.ensure_schema()
    runner = TaskRunner(db, cfg, process_registry={})
    pool = WorkerPool(db, cfg, runner=runner, concurrency=2, poll_interval_s=0.01)
    loop_task = asyncio.create_task(pool.run_forever())
    try:
        await handle_inbound(db, cfg, pool, None, inbound(1, "第一步"))
        await handle_inbound(db, cfg, pool, None, inbound(2, "第二步"))
        await _wait_done(db, timeout=10)
        prompts = [r["prompt"] for r in db._conn.execute("SELECT prompt FROM tasks")]
        assert prompts == ["第一步\n第二步"]
    finally:
        loop_task.cancel()
        await asyncio.gather(loop_task, return_exceptions=True)
```

- [ ] **Step 2: Run E2E + 全量**

Run: `python -m pytest tests/test_e2e.py -v` → 新增 PASS
Run: `python -m pytest` → 全绿。记总数（423 + 新增）。

- [ ] **Step 3: 文档同步**

`README.md` 日常使用节加一段：连发消息会自动合并为一个 prompt（2 秒窗口，可 `/config set throttle.merge_window_s` 调）；任务排队时 ACK 显示位次。

`CLAUDE.md`：
- 当前状态追加 M5C1 句（N 测试全绿、真机验收另行）。
- 常用命令测试数 423 → N。
- 新增「M5C1 功能清单」节：合并窗口（KV+计时+启动恢复）+ 队列 ACK + 纯文本限定 + flush-first + 真注入结构不可。
- 硬性约束补：claude `-p` stdin 一次性关闭（中途注入不可——B 仅语义）。
- 组件清单 app.py 描述补「M5C1 合并窗口」。

`docs/superpowers/specs/2026-08-21-input-merge-design.md`：
- 状态行 → `已实现（2026-08-21，N 测试全绿；真机验收另行）`
- §3.2 末加收窄注记：「实现分析后 flush-first 仅 task-creating 非 chat 路径（forward/媒体）；Y-N/bridge/proxy 不建任务、窗口计时器自行 flush；session_id append 时锁定故 /cd 不影响已暂存 batch。spec 原述『slash/Y-N 先 flush』以此收窄为准。」

- [ ] **Step 4: Commit**

```bash
git add tests/test_e2e.py README.md CLAUDE.md \
        docs/superpowers/specs/2026-08-21-input-merge-design.md
git commit -m "feat(M5C1): E2E 连发合并 + 文档同步（README/CLAUDE.md/spec §3.2 收窄注记）"
```

---

## 验收核查表（实现完成后、真机验收前）

- [ ] `python -m pytest` 全绿无新增 skip
- [ ] 真机验收：连发两条消息 → 微信收到「正在合并」+「已合并 2 条」+ 单轮回复；窗口内发 /status → 即时回执不破坏合并；任务排队时 ACK 显示位次
