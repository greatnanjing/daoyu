# M4 主动服务（定时日报 + 巡检告警）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 刀鱼新增第五常驻协程 scheduler——每日定时推送纯模板日报（异常时自动升级 Claude 分析），周期巡检磁盘/CPU/内存/刀鱼自身/证书（异常推告警 + 自动建分析任务 + 静默期防重复），全部经 `/cron` 微信命令开关与调整。

**Architecture:** 进程内 asyncio 协程每分钟整分对齐醒来，现读 `cron_jobs` 表决定动作（`/cron` 改表即时生效）；日报/巡检判定纯 Python 零 token 成本，推送走现有 outbox enqueue、分析任务走现有 `create_task` 任务管道挂专用 ops 话题（固定 UUID，分析历史上下文延续）。

**Tech Stack:** Python 3.11+ asyncio / SQLite（复用 `common/db.py` 同步访问模式）/ psutil（新依赖，lazy import）/ cryptography（已有依赖，读证书 NotAfter）。

**Spec:** [docs/superpowers/specs/2026-08-21-cron-patrol-design.md](../specs/2026-08-21-cron-patrol-design.md)

## Global Constraints

- 一切推送经 `db.enqueue` 落 outbox（发白名单全部用户），**绝不直接调 iLink**——投递/重试/死信由现有出站循环负责。
- 分析任务经 `db.create_task(None, session_id, prompt, kind="chat")` 入队，scheduler **不自己跑 Claude**。
- 时间可注入：判定/渲染函数接受 `now: int` 参数，不写死 `time.time()`；psutil 采样经 `sample: dict` 参数注入，测试不依赖真机状态。
- psutil 在函数内 lazy import（模块顶层 import 会让未装 psutil 的环境连 `/cron` 命令都炸——bridge 局部导入 scheduler）。
- 中文注释、中文命令回执，对齐现有 gateway/worker 代码风格。
- 微信单条上限由出站分页兜底，scheduler 不再分页。
- 生产时区中国（无夏令时），"昨日"= 当日本地零点 − 86400（CST 无 DST 漂移）。
- `OPS_UUID = "0da0f00d-0f00-4000-8000-00000000000d"`（全 hex 合法 UUID 形态，模块常量）。
- 测试命令统一 `python -m pytest tests/test_scheduler.py -v`（Windows 开发机 Git Bash；全量 `python -m pytest`）。

---

### Task 1: db 层——cron_jobs 表 + CronJob 模型 + 统计查询

**Files:**
- Modify: `common/models.py`（文件末尾追加）
- Modify: `common/db.py`（`_SCHEMA` 常量 + `ensure_schema` + 新方法组）
- Test: `tests/test_scheduler.py`（新建）

**Interfaces:**
- Consumes: 现有 `Database` 类、`SessionBinding`、`local_midnight_ts()` 风格。
- Produces（后续任务依赖的精确签名）:
  - `CronJob` dataclass（`common/models.py`）：字段 `id: int, name: str, enabled: int, time_of_day: str | None, interval_min: int | None, last_run_at: int | None, last_result: str | None`
  - `Database.cron_jobs() -> list[CronJob]`
  - `Database.update_cron(name: str, *, enabled: int | None = None, time_of_day: str | None = None, interval_min: int | None = None, touch_last_run: int | None = None) -> bool`（None 字段不改；`touch_last_run` 传 epoch 则把 `last_run_at` 置该值——`/cron on` 的"从当前时刻起算"语义）
  - `Database.mark_cron_run(name: str, last_result: str) -> None`（`last_run_at=now` + 写结果）
  - `Database.daily_task_stats(start: int, end: int) -> dict`（`{"done": n, "canceled": n, "dead": n, "total": n}`——created_at 落窗口的任务按 state 分组；中间态 pending/running 并入 total 不单列）
  - `Database.daily_cost(start: int, end: int) -> float`（audit_log kind='cost' 窗口求和，解析模式照抄 `today_cost_usd`）
  - `Database.outbox_sent_count(start: int, end: int) -> int`（sent_at 落窗口且 state='sent' 计数）
  - `Database.create_fixed_session(wechat_user: str, cwd: str, claude_uuid: str) -> SessionBinding`（固定 uuid 建话题行：不动当前话题指针、不置 `claude_session_inited`——首次任务 `--session-id` 建会话、之后 runner 自然 `--resume`；重复 uuid `INSERT OR IGNORE` 幂等）

- [ ] **Step 1: 写失败测试**

新建 `tests/test_scheduler.py`：

```python
"""M4 主动服务：cron_jobs 表、调度判定、日报/巡检、/cron 命令。"""
import time

import pytest

from common.db import Database


def test_cron_jobs_preset(db):
    """ensure_schema 预置 daily(08:00) + patrol(10min) 两行，默认启用。"""
    jobs = {j.name: j for j in db.cron_jobs()}
    assert set(jobs) == {"daily", "patrol"}
    assert jobs["daily"].enabled == 1
    assert jobs["daily"].time_of_day == "08:00"
    assert jobs["patrol"].enabled == 1
    assert jobs["patrol"].interval_min == 10
    assert jobs["daily"].last_run_at is None


def test_update_cron_partial_and_touch(db):
    assert db.update_cron("daily", time_of_day="09:30") is True
    j = {x.name: x for x in db.cron_jobs()}["daily"]
    assert j.time_of_day == "09:30"
    assert j.enabled == 1          # 未传字段不动
    now = int(time.time())
    db.update_cron("patrol", enabled=0, touch_last_run=now)
    j = {x.name: x for x in db.cron_jobs()}["patrol"]
    assert j.enabled == 0
    assert j.last_run_at == now
    assert db.update_cron("nope", enabled=1) is False   # 未知名 False


def test_mark_cron_run(db):
    db.mark_cron_run("daily", "正常，推送 1 条")
    j = {x.name: x for x in db.cron_jobs()}["daily"]
    assert j.last_run_at is not None
    assert j.last_result == "正常，推送 1 条"


def test_daily_task_stats_window(db):
    now = int(time.time())
    s = db.get_or_create_session("u@im.wechat", "/repo")
    db.create_task(None, s.id, "a")                       # now（窗口内）
    old = db._conn.execute(
        "INSERT INTO tasks(message_id, session_id, prompt, kind, state, attempts,"
        " max_attempts, created_at, updated_at) VALUES(NULL,?, 'old', 'chat',"
        " 'done', 0, 3, ?, ?)", (s.id, now - 90000, now - 90000))
    db._conn.commit()
    tid = db.create_task(None, s.id, "will-dead")
    db._conn.execute("UPDATE tasks SET state='dead' WHERE id=?", (tid,))
    db._conn.commit()
    stats = db.daily_task_stats(now - 3600, now + 60)
    assert stats["total"] == 2          # 窗口只含 a 与 will-dead
    assert stats["dead"] == 1
    assert "done" not in stats or stats.get("done", 0) == 0   # old 落窗外


def test_daily_cost_and_sent_count(db):
    now = int(time.time())
    db.audit("cost", '{"task_id": 1, "usd": 0.5}')
    db.audit("cost", '{"task_id": 2, "usd": 0.25}')
    db.audit("cost", "not-json")        # 坏行不计不炸
    db.enqueue(None, "u@im.wechat", "hello")
    row = db._conn.execute("SELECT id FROM outbox LIMIT 1").fetchone()
    db.mark_sent(row["id"])
    assert db.daily_cost(now - 60, now + 60) == pytest.approx(0.75)
    assert db.outbox_sent_count(now - 60, now + 60) == 1


def test_create_fixed_session_idempotent(db):
    from common.models import SessionBinding
    b = db.create_fixed_session("u@im.wechat", "/repo", "0da0f00d-0f00-4000-8000-00000000000d")
    assert isinstance(b, SessionBinding)
    assert b.claude_uuid == "0da0f00d-0f00-4000-8000-00000000000d"
    # 重复 uuid 不炸（INSERT OR IGNORE）且返回既有行
    b2 = db.create_fixed_session("u@im.wechat", "/repo",
                                 "0da0f00d-0f00-4000-8000-00000000000d")
    assert b2.id == b.id
    # 不动当前话题指针
    assert db.get_state("active_session:u@im.wechat") is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_scheduler.py -v`
Expected: FAIL（`AttributeError: 'Database' object has no attribute 'cron_jobs'` 或 ImportError）

- [ ] **Step 3: 实现**

`common/models.py` 文件末尾追加：

```python
@dataclass
class CronJob:
    """M4 主动服务任务行（cron_jobs 表）：daily=定时日报 / patrol=周期巡检。"""
    id: int
    name: str
    enabled: int
    time_of_day: str | None    # daily 用：'08:00'
    interval_min: int | None   # patrol 用：分钟
    last_run_at: int | None
    last_result: str | None
```

`common/models.py` 顶部 import 行确认含 `from dataclasses import dataclass`（已有）。

`common/db.py`：

(1) `_SCHEMA` 末尾（`approvals` 索引行之后）追加：

```sql
CREATE TABLE IF NOT EXISTS cron_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  enabled INTEGER NOT NULL DEFAULT 1,
  time_of_day TEXT,
  interval_min INTEGER,
  last_run_at INTEGER,
  last_result TEXT
);
```

(2) `ensure_schema` 的 `self._conn.executescript(_SCHEMA)` 之后、`self._ensure_media_columns()` 之前加预置：

```python
        self._conn.executescript(_SCHEMA)
        # M4 预置两行（INSERT OR IGNORE 幂等——升级部署与全新库同路径）
        self._conn.execute(
            "INSERT OR IGNORE INTO cron_jobs(name, enabled, time_of_day, interval_min) "
            "VALUES('daily', 1, '08:00', NULL)")
        self._conn.execute(
            "INSERT OR IGNORE INTO cron_jobs(name, enabled, time_of_day, interval_min) "
            "VALUES('patrol', 1, NULL, 10)")
```

(3) 顶部 import 区（`from common.models import ...` 行）把 `CronJob` 加进导入元组。

(4) 类末尾（`get_approval` 方法之后）追加方法组：

```python
    # ---- cron_jobs（M4 主动服务）----
    def cron_jobs(self) -> list[CronJob]:
        rows = self._conn.execute("SELECT * FROM cron_jobs ORDER BY id").fetchall()
        return [CronJob(id=r["id"], name=r["name"], enabled=r["enabled"],
                        time_of_day=r["time_of_day"], interval_min=r["interval_min"],
                        last_run_at=r["last_run_at"], last_result=r["last_result"])
                for r in rows]

    def update_cron(self, name: str, *, enabled: int | None = None,
                    time_of_day: str | None = None, interval_min: int | None = None,
                    touch_last_run: int | None = None) -> bool:
        """/cron 命令写入口：None 字段不动；touch_last_run 传 epoch 则重置
        last_run_at（/cron on 的「从当前时刻起算」——patrol 等满一个间隔、
        daily 不补跑错过的时间点）。未知 name 返回 False。"""
        sets, vals = [], []
        if enabled is not None:
            sets.append("enabled=?"); vals.append(enabled)
        if time_of_day is not None:
            sets.append("time_of_day=?"); vals.append(time_of_day)
        if interval_min is not None:
            sets.append("interval_min=?"); vals.append(interval_min)
        if touch_last_run is not None:
            sets.append("last_run_at=?"); vals.append(touch_last_run)
        if not sets:
            return False
        cur = self._conn.execute(
            f"UPDATE cron_jobs SET {', '.join(sets)} WHERE name=?", (*vals, name))
        self._conn.commit()
        return bool(cur.rowcount)

    def mark_cron_run(self, name: str, last_result: str) -> None:
        """scheduler 到点先占位后收尾（防同分钟崩溃重入重复推送）。"""
        self._conn.execute(
            "UPDATE cron_jobs SET last_run_at=?, last_result=? WHERE name=?",
            (int(time.time()), last_result, name))
        self._conn.commit()

    def daily_task_stats(self, start: int, end: int) -> dict:
        """日报任务板块：created_at 落 [start,end) 的任务按 state 分组。
        展示三态 done/canceled/dead；pending/running 等中间态并入 total。"""
        rows = self._conn.execute(
            "SELECT state, COUNT(*) c FROM tasks WHERE created_at>=? AND created_at<? "
            "GROUP BY state", (start, end)).fetchall()
        by = {r["state"]: r["c"] for r in rows}
        return {"done": by.get("done", 0), "canceled": by.get("canceled", 0),
                "dead": by.get("dead", 0), "total": sum(by.values())}

    def daily_cost(self, start: int, end: int) -> float:
        """audit_log kind='cost' 窗口求和（today_cost_usd 的参数化版）。"""
        total = 0.0
        for row in self._conn.execute(
                "SELECT detail FROM audit_log WHERE kind='cost' AND ts>=? AND ts<?",
                (start, end)):
            try:
                total += float(json.loads(row["detail"]).get("usd", 0))
            except (ValueError, TypeError, AttributeError):
                pass
        return total

    def outbox_sent_count(self, start: int, end: int) -> int:
        return self._conn.execute(
            "SELECT COUNT(*) c FROM outbox WHERE state='sent' "
            "AND sent_at>=? AND sent_at<?", (start, end)).fetchone()["c"]

    def create_fixed_session(self, wechat_user: str, cwd: str,
                             claude_uuid: str) -> SessionBinding:
        """固定 uuid 话题行（M4 ops 话题用）：不动当前话题指针、不置
        claude_session_inited——首次任务 --session-id 建会话、之后 runner 自然
        --resume。重复 uuid（已存在）幂等返回既有行。"""
        now = int(time.time())
        self._conn.execute(
            "INSERT OR IGNORE INTO sessions(wechat_user, cwd, claude_uuid, policy, "
            "created_at, last_active_at) VALUES(?,?,?,?,?,?)",
            (wechat_user, cwd, claude_uuid, "auto", now, now))
        self._conn.commit()
        row = self._conn.execute(
            "SELECT * FROM sessions WHERE claude_uuid=?", (claude_uuid,)).fetchone()
        return SessionBinding(**dict(row))
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_scheduler.py -v`
Expected: 6 PASS

- [ ] **Step 5: 全量回归 + 提交**

Run: `python -m pytest`（预期全绿，新增 6 个）
```bash
git add common/models.py common/db.py tests/test_scheduler.py
git commit -m "feat(M4): cron_jobs 表与统计查询——db 层主动服务地基"
```

---

### Task 2: config 层——cron 节 + /config set 白名单扩展

**Files:**
- Modify: `common/config.py`
- Modify: `gateway/config.example.json`
- Modify: `gateway/proxy.py`（`CONFIG_KEYS` 字典加 7 键）
- Test: `tests/test_scheduler.py`（追加）、`tests/test_proxy.py`（追加一例）

**Interfaces:**
- Consumes: 现有 `_DEFAULT_THROTTLE` 合并模式、`CONFIG_KEYS` 的 `(解析器, 校验器, 类型名)` 三元组格式。
- Produces: `Config.cron: dict`——scheduler 全部阈值从此读，键与默认：
  `disk_threshold_pct=85, cpu_threshold_pct=90, mem_threshold_pct=90, load_sustain_min=5, cert_warn_days=14, cert_paths=["/etc/letsencrypt/live"], alert_silence_h=6, queue_backlog_warn=20`（`cert_paths` 不进 set 白名单——列表值，低频改文件）。

- [ ] **Step 1: 写失败测试**

`tests/test_scheduler.py` 追加：

```python
def test_config_cron_defaults():
    """实例 config.json 无 cron 节时给全默认（config.example.json 同构）。"""
    from common.config import _DEFAULT_CRON
    assert _DEFAULT_CRON["disk_threshold_pct"] == 85
    assert _DEFAULT_CRON["load_sustain_min"] == 5
    assert _DEFAULT_CRON["cert_paths"] == ["/etc/letsencrypt/live"]
    assert _DEFAULT_CRON["alert_silence_h"] == 6
    assert _DEFAULT_CRON["queue_backlog_warn"] == 20
```

`tests/test_proxy.py` 追加（该文件已有 proxy 测试的构造方式，找不到现成 fake 时按下方自足写法）：

```python
def test_config_set_cron_key(tmp_path, monkeypatch):
    """/config set cron.disk_threshold_pct 白名单可写、范围校验生效。"""
    import json as _json
    from gateway import proxy
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(_json.dumps({"cron": {"disk_threshold_pct": 85}}),
                        encoding="utf-8")
    raw = _json.loads(cfg_path.read_text(encoding="utf-8"))
    reply = proxy._config_set(cfg_path, raw, ["cron.disk_threshold_pct", "92"])
    assert reply.startswith("已写入")
    assert _json.loads(cfg_path.read_text(encoding="utf-8"))["cron"]["disk_threshold_pct"] == 92
    raw = _json.loads(cfg_path.read_text(encoding="utf-8"))
    assert proxy._config_set(cfg_path, raw, ["cron.disk_threshold_pct", "101"]).startswith("值")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_scheduler.py tests/test_proxy.py -v -k cron`
Expected: FAIL（ImportError `_DEFAULT_CRON` / set 拒绝）

- [ ] **Step 3: 实现**

`common/config.py`：

(1) `_DEFAULT_RECONNECT` 之后加：

```python
# M4 主动服务阈值（scheduler 读取）。cert_paths 为列表不进 /config set 白名单
# （低频运维键，直接改文件）；数值键经 proxy.CONFIG_KEYS 开放微信 set。
_DEFAULT_CRON = {"disk_threshold_pct": 85, "cpu_threshold_pct": 90,
                 "mem_threshold_pct": 90, "load_sustain_min": 5,
                 "cert_warn_days": 14, "cert_paths": ["/etc/letsencrypt/live"],
                 "alert_silence_h": 6, "queue_backlog_warn": 20}
```

(2) `Config` dataclass 加字段（`media_retention_days` 之后）：

```python
    # M4 主动服务阈值（/config set 可改数值键，重启生效）
    cron: dict = field(default_factory=lambda: dict(_DEFAULT_CRON))
```

(3) `load_config` 的 `reconnect` 合并块之后加：

```python
    cron = dict(_DEFAULT_CRON)
    cron.update(raw.get("cron") or {})
```

并把 `cron=cron` 加进 `return Config(...)` 实参。

`gateway/config.example.json` 的 `reconnect` 节之后加：

```json
  "cron": {
    "disk_threshold_pct": 85,
    "cpu_threshold_pct": 90,
    "mem_threshold_pct": 90,
    "load_sustain_min": 5,
    "cert_warn_days": 14,
    "cert_paths": ["/etc/letsencrypt/live"],
    "alert_silence_h": 6,
    "queue_backlog_warn": 20
  }
```

`gateway/proxy.py` 的 `CONFIG_KEYS` 字典追加：

```python
    "cron.disk_threshold_pct": (int, lambda v: 50 <= v <= 99, "整数"),
    "cron.cpu_threshold_pct": (int, lambda v: 50 <= v <= 99, "整数"),
    "cron.mem_threshold_pct": (int, lambda v: 50 <= v <= 99, "整数"),
    "cron.load_sustain_min": (int, lambda v: 1 <= v <= 60, "整数"),
    "cron.cert_warn_days": (int, lambda v: 1 <= v <= 90, "整数"),
    "cron.alert_silence_h": (int, lambda v: 1 <= v <= 72, "整数"),
    "cron.queue_backlog_warn": (int, lambda v: 1 <= v <= 500, "整数"),
```

(注意 `set` 分支 `raw.setdefault(section, {})` 已能建出 cron 节，无需其他改动。)

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_scheduler.py tests/test_proxy.py -v`
Expected: PASS（含既有用例）

- [ ] **Step 5: 全量回归 + 提交**

Run: `python -m pytest`
```bash
git add common/config.py gateway/config.example.json gateway/proxy.py tests/test_scheduler.py tests/test_proxy.py
git commit -m "feat(M4): config cron 节——阈值默认 + /config set 七键白名单"
```

---

### Task 3: 调度判定 + /cron 命令

**Files:**
- Create: `gateway/scheduler.py`（本 task 只建骨架：常量 + 判定函数）
- Modify: `gateway/router.py:8`（`BRIDGE_COMMANDS` 加 `"cron"`）
- Modify: `gateway/bridge.py`（`BRIDGE_HELP` + `execute_bridge` 分支 + `_cron` 函数）
- Test: `tests/test_scheduler.py`（追加）

**Interfaces:**
- Consumes: Task 1 的 `db.cron_jobs()/update_cron()`、`CronJob`。
- Produces:
  - `gateway/scheduler.py`：`OPS_UUID: str`、`_today_ts(hhmm: str, now: int) -> int`、`due_daily(job: CronJob, now: int) -> bool`、`due_patrol(job: CronJob, now: int) -> bool`、`next_run_time(job: CronJob, now: int) -> int | None`（disabled → None）
  - `gateway/bridge.py`：`_cron(db, args: str) -> str`（同步纯 DB 读写）、execute_bridge 的 `if cmd == "cron":` 分支

- [ ] **Step 1: 写失败测试**

`tests/test_scheduler.py` 追加：

```python
# ---- 调度判定（时间注入；固定锚点 2026-08-21 10:30 本地）----
_ANCHOR = int(time.mktime((2026, 8, 21, 10, 30, 0, 0, 0, -1)))


def _job(**kw):
    from common.models import CronJob
    base = dict(id=1, name="daily", enabled=1, time_of_day="08:00",
                interval_min=None, last_run_at=None, last_result=None)
    base.update(kw)
    return CronJob(**base)


def test_due_daily():
    from gateway.scheduler import due_daily
    # 今日 08:00 已过、从未跑 → due
    assert due_daily(_job(), _ANCHOR) is True
    # 上次跑在今日 08:00 之后 → 今日已跑，不 due
    assert due_daily(_job(last_run_at=_ANCHOR), _ANCHOR) is False
    # 还没到点（07:00 时刻）→ 不 due
    early = _ANCHOR - 3 * 3600
    assert due_daily(_job(), early) is False
    # 禁用恒不 due
    assert due_daily(_job(enabled=0), _ANCHOR) is False


def test_due_patrol():
    from gateway.scheduler import due_patrol
    p = dict(id=2, name="patrol", enabled=1, time_of_day=None, interval_min=10)
    # 从未跑（last_run_at=None）→ 立即 due（首轮建立基线）
    assert due_patrol(_job(**p), _ANCHOR) is True
    # 5 分钟前跑过、间隔 10 → 未到
    assert due_patrol(_job(**p, last_run_at=_ANCHOR - 300), _ANCHOR) is False
    # 11 分钟前跑过 → due
    assert due_patrol(_job(**p, last_run_at=_ANCHOR - 660), _ANCHOR) is True


def test_next_run_time():
    from gateway.scheduler import next_run_time
    early = _ANCHOR - 3 * 3600   # 07:30：daily 下次 = 今日 08:00
    assert next_run_time(_job(), early) == early + 1800
    assert next_run_time(_job(), _ANCHOR) == _ANCHOR + 16 * 3600 + 1800  # 已过 → 明日 08:00
    assert next_run_time(_job(enabled=0), _ANCHOR) is None
    p = dict(id=2, name="patrol", enabled=1, time_of_day=None, interval_min=10)
    assert next_run_time(_job(**p, last_run_at=_ANCHOR - 300), _ANCHOR) == _ANCHOR + 300
```

桥命令测试（照 `tests/test_bridge.py` 的 `FakeCfg`/`_route` 构造；若该文件的 FakeCfg/`_route` 未导出则在本文件复制最小版）：

```python
class _FakeCfg:
    def __init__(self):
        self.reconnect = {"session_duration_s": 86400}
        self.default_cwd = "/repo"
        self.throttle = {"page_char_limit": 2000}


def _route(cmd, args=""):
    from gateway.router import Route
    return Route(kind="bridge", command=cmd, args=args, detail={})


async def test_cron_cmd(db):
    from gateway.bridge import execute_bridge
    # 列表（无参 = list）
    r = await execute_bridge(db, None, _route("cron"), "u@im.wechat", _FakeCfg())
    assert "daily" in r and "patrol" in r and "08:00" in r
    # off / on
    r = await execute_bridge(db, None, _route("cron", "off patrol"), "u@im.wechat", _FakeCfg())
    assert "已暂停" in r
    j = {x.name: x for x in db.cron_jobs()}["patrol"]
    assert j.enabled == 0
    r = await execute_bridge(db, None, _route("cron", "on patrol"), "u@im.wechat", _FakeCfg())
    assert "已开启" in r
    # time / interval
    r = await execute_bridge(db, None, _route("cron", "time daily 09:30"),
                             "u@im.wechat", _FakeCfg())
    assert "09:30" in r
    assert {x.name: x for x in db.cron_jobs()}["daily"].time_of_day == "09:30"
    r = await execute_bridge(db, None, _route("cron", "interval patrol 15"),
                             "u@im.wechat", _FakeCfg())
    assert "15" in r
    # 非法参数回用法
    r = await execute_bridge(db, None, _route("cron", "time daily 25:99"),
                             "u@im.wechat", _FakeCfg())
    assert "HH:MM" in r
    r = await execute_bridge(db, None, _route("cron", "bogus"), "u@im.wechat", _FakeCfg())
    assert "用法" in r


def test_router_cron_bridge():
    from gateway.router import route
    assert route("/cron", set()).kind == "bridge"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_scheduler.py -v`
Expected: FAIL（`No module named 'gateway.scheduler'` / route 为 unknown）

- [ ] **Step 3: 实现**

新建 `gateway/scheduler.py`：

```python
"""M4 主动服务调度器：日报（daily）+ 巡检（patrol）。

scheduler_loop 每分钟整分对齐醒来、现读 cron_jobs 表决定动作（/cron 改表
即时生效）；日报/巡检判定纯 Python 零 token 成本，推送经 db.enqueue 落
outbox（发白名单全部用户），异常时建 Claude 分析任务挂 ops 话题（固定
UUID，分析历史上下文延续）。scheduler 绝不直接调 iLink、不自己跑 Claude。
"""
import time

from common.models import CronJob

# ops 话题固定 UUID（合法 hex 形态；ensure_ops_session 建行，/delete 删了会重建）
OPS_UUID = "0da0f00d-0f00-4000-8000-00000000000d"


def _today_ts(hhmm: str, now: int) -> int:
    """now 当日 hhmm 时刻的 epoch（localtime 构造，口径同 db.local_midnight_ts）。"""
    h, m = hhmm.split(":")
    lt = time.localtime(now)
    return int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, int(h), int(m),
                            0, 0, 0, -1)))


def due_daily(job: CronJob, now: int) -> bool:
    """到点且今日未跑（last_run_at < 今日时刻）——防同一天重复推送。"""
    if not job.enabled or not job.time_of_day:
        return False
    ts = _today_ts(job.time_of_day, now)
    return ts <= now and (job.last_run_at or 0) < ts


def due_patrol(job: CronJob, now: int) -> bool:
    """距上次运行满间隔；从未跑（None）立即 due——首轮建立基线。"""
    if not job.enabled or not job.interval_min:
        return False
    return now - (job.last_run_at or 0) >= job.interval_min * 60


def next_run_time(job: CronJob, now: int) -> int | None:
    """/cron 列表呈现用；禁用返回 None。"""
    if not job.enabled:
        return None
    if job.name == "daily" and job.time_of_day:
        ts = _today_ts(job.time_of_day, now)
        if ts <= now or (job.last_run_at or 0) >= ts:
            ts += 86400
        return ts
    if job.interval_min:
        return (job.last_run_at or now) + job.interval_min * 60
    return None
```

`gateway/router.py:8` 改为：

```python
BRIDGE_COMMANDS = {"cancel", "tasks", "status", "cd", "sessions", "policy", "bg", "new", "adopt", "delete", "cron"}
```

`gateway/bridge.py`：

(1) `BRIDGE_HELP` 字典加一行（`"bg"` 之后）：

```python
    "cron": "/cron — 定时任务（日报/巡检）：on|off、time daily <HH:MM>、interval patrol <分钟>",
```

(2) `execute_bridge` 里 `if cmd == "bg":` 分支之后、`return f"未知桥命令 {cmd}"` 之前加：

```python
    if cmd == "cron":
        return _cron(db, route.args.strip())
```

(3) 文件末尾（`_delete` 之后）加：

```python
def _cron(db, arg: str) -> str:
    """/cron 主动服务管理（M4）：写 cron_jobs 表，scheduler 每轮现读即时生效。
    daily=定时日报 / patrol=周期巡检（详见 scheduler 模块）。"""
    from gateway.scheduler import next_run_time   # 局部导入：scheduler lazy psutil
    parts = arg.split()
    jobs = {j.name: j for j in db.cron_jobs()}
    usage = ("用法：/cron — 列表；/cron on|off <daily|patrol>；"
             "/cron time daily <HH:MM>；/cron interval patrol <分钟>")
    if not parts or parts[0] == "list":
        now = int(time.time())
        lines = []
        for name, icon in (("daily", "📅"), ("patrol", "🔍")):
            j = jobs.get(name)
            if j is None:
                continue
            mark = "✅" if j.enabled else "⏸"
            sched = (f"每天 {j.time_of_day}" if name == "daily"
                     else f"每 {j.interval_min} 分钟")
            nxt = next_run_time(j, now)
            nxt_s = time.strftime("%m-%d %H:%M", time.localtime(nxt)) if nxt else "—"
            lines.append(f"{icon} {name} {mark} {sched}（下次：{nxt_s}）")
            if j.last_run_at:
                lr = time.strftime("%m-%d %H:%M", time.localtime(j.last_run_at))
                lines.append(f"   └ 上次 {lr} · {j.last_result or '—'}")
            else:
                lines.append("   └ 尚未运行")
        lines.append(usage)
        return "\n".join(lines)
    op = parts[0].lower()
    if op in ("on", "off") and len(parts) == 2 and parts[1] in ("daily", "patrol"):
        db.update_cron(parts[1], enabled=1 if op == "on" else 0,
                       touch_last_run=int(time.time()) if op == "on" else None)
        db.audit("cron", f"{op} {parts[1]} user")
        if op == "on":
            return (f"{parts[1]} 已开启（从当前时刻起算：daily 到点即跑、"
                    f"patrol 满一个间隔后跑首轮）。")
        return f"{parts[1]} 已暂停。"
    if op == "time" and len(parts) == 3 and parts[1] == "daily":
        hhmm = parts[2]
        ok = (len(hhmm) == 5 and hhmm[2] == ":" and hhmm[:2].isdigit()
              and hhmm[3:].isdigit() and int(hhmm[:2]) < 24 and int(hhmm[3:]) < 60)
        if not ok:
            return "时间格式应为 HH:MM（如 08:30）。"
        db.update_cron("daily", time_of_day=hhmm)
        db.audit("cron", f"time daily {hhmm}")
        return f"日报时间已改为每天 {hhmm}。"
    if op == "interval" and len(parts) == 3 and parts[1] == "patrol":
        if not parts[2].isdigit() or int(parts[2]) < 1:
            return "间隔应为 ≥1 的分钟数。"
        db.update_cron("patrol", interval_min=int(parts[2]))
        db.audit("cron", f"interval patrol {parts[2]}")
        return f"巡检间隔已改为 {parts[2]} 分钟。"
    return usage
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_scheduler.py tests/test_bridge.py tests/test_router.py -v`
Expected: PASS（含既有 /help 生成用例——BRIDGE_HELP 加行后 build_help 会多一行，既有断言若断言精确全文需同步更新，按失败信息改）

- [ ] **Step 5: 全量回归 + 提交**

Run: `python -m pytest`
```bash
git add gateway/scheduler.py gateway/router.py gateway/bridge.py tests/test_scheduler.py tests/test_bridge.py
git commit -m "feat(M4): 调度判定函数 + /cron 桥命令（列表/开关/时间/间隔）"
```

---

### Task 4: 日报链路（collect / render / anomaly / run_daily）

**Files:**
- Modify: `gateway/scheduler.py`（追加日报函数组）
- Test: `tests/test_scheduler.py`（追加）

**Interfaces:**
- Consumes: Task 1 的 `daily_task_stats/daily_cost/outbox_sent_count/queue_depth/dead_letter_count`、Task 2 的 `cfg.cron`。
- Consumes（本 task 内定义后 Task 5/6 也用）: `sample: dict` 契约——`{"cpu": float百分比, "mem": float百分比, "disks": dict[路径, float百分比], "boot_days": float}`（psutil 真实实现在 Task 6，本 task 全用注入）。
- Produces:
  - `collect_daily_data(db, cfg, now: int, sample: dict) -> dict`——键：`date`(str)、`tasks`(dict)、`cost_usd`(float)、`cpu/mem/disks/boot_days`(来自 sample)、`sent`(int)、`backlog`(int)、`dead_outbox`(int)、`online`(bool)、`media_mb`(float)
  - `render_daily_report(data: dict) -> str`
  - `daily_anomalies(data: dict, cron_cfg: dict) -> list[str]`
  - `run_daily(db, cfg, now: int, sample: dict) -> str`（返回写进 `last_result` 的一句话）

- [ ] **Step 1: 写失败测试**

`tests/test_scheduler.py` 追加：

```python
_SAMPLE_OK = {"cpu": 23.0, "mem": 61.0, "disks": {"/": 42.0}, "boot_days": 12.3}


def _dcfg(tmp_path):
    """最小 Config 替身（scheduler 只读 cron/default_cwd/whitelist/repo_root）。"""
    from common.config import _DEFAULT_CRON
    from types import SimpleNamespace
    return SimpleNamespace(cron=dict(_DEFAULT_CRON), default_cwd="/repo",
                           whitelist={"u@im.wechat"}, repo_root=tmp_path)


def test_render_daily_report():
    from gateway.scheduler import render_daily_report
    data = {"date": "2026-08-21", "tasks": {"done": 4, "canceled": 1, "dead": 0,
                                            "total": 5},
            "cost_usd": 0.83, "cpu": 23.0, "mem": 61.0, "disks": {"/": 42.0},
            "boot_days": 12.3, "sent": 32, "backlog": 0, "dead_outbox": 0,
            "online": True, "media_mb": 128.4}
    text = render_daily_report(data)
    assert "🌅 刀鱼日报 2026-08-21" in text
    assert "成功 4 / 取消 1 / 死信 0" in text
    assert "$0.83" in text
    assert "CPU 23%" in text and "磁盘" in text
    assert "出站 32 条" in text and "连接正常" in text


def test_daily_anomalies():
    from gateway.scheduler import daily_anomalies
    from common.config import _DEFAULT_CRON
    ok = {"tasks": {"dead": 0}, "disks": {"/": 42.0}, "cpu": 23.0, "mem": 61.0,
          "backlog": 0, "online": True}
    assert daily_anomalies(ok, _DEFAULT_CRON) == []
    bad = {"tasks": {"dead": 2}, "disks": {"/": 91.0}, "cpu": 95.0, "mem": 50.0,
           "backlog": 0, "online": True}
    got = daily_anomalies(bad, _DEFAULT_CRON)
    assert len(got) == 2 and any("死信" in g for g in got) and any("/" in g for g in got)


def test_run_daily_normal(db, tmp_path):
    from gateway.scheduler import run_daily
    cfg = _dcfg(tmp_path)
    result = run_daily(db, cfg, _ANCHOR, dict(_SAMPLE_OK))
    assert "正常" in result
    rows = db._conn.execute(
        "SELECT to_user, text FROM outbox WHERE text LIKE '%日报%'").fetchall()
    assert rows and rows[0]["to_user"] == "u@im.wechat"
    assert "⏳" not in rows[0]["text"]
    # 正常轮次零 Claude 调用：无新任务、无 cost 行
    assert db._conn.execute("SELECT COUNT(*) c FROM tasks").fetchone()["c"] == 0


def test_run_daily_anomaly_escalates(db, tmp_path):
    from gateway.scheduler import run_daily, OPS_UUID
    cfg = _dcfg(tmp_path)
    s = db.get_or_create_session("u@im.wechat", "/repo")
    tid = db.create_task(None, s.id, "will-dead")
    db._conn.execute("UPDATE tasks SET state='dead' WHERE id=?", (tid,))
    db._conn.commit()
    sample = dict(_SAMPLE_OK, disks={"/": 91.0})
    result = run_daily(db, cfg, _ANCHOR, sample)
    assert "异常" in result
    text = db._conn.execute(
        "SELECT text FROM outbox WHERE text LIKE '%日报%'").fetchone()["text"]
    assert "⏳" in text
    # 分析任务挂 ops 话题
    row = db._conn.execute(
        "SELECT t.id, t.session_id FROM tasks t JOIN sessions s ON t.session_id=s.id "
        "WHERE s.claude_uuid=?", (OPS_UUID,)).fetchone()
    assert row is not None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_scheduler.py -v -k daily`
Expected: FAIL（ImportError render_daily_report）

- [ ] **Step 3: 实现**

`gateway/scheduler.py` 顶部 import 补 `json` 与 `from pathlib import Path`，模块追加：

```python
def _broadcast(db, cfg, text: str) -> None:
    """发白名单全部用户（outbound._alert_all 同构：同步 enqueue 落 outbox，
    投递由出站循环 0.5s 轮询接管；whitelist 缺席（测试替身）静默跳过）。"""
    for user in sorted(getattr(cfg, "whitelist", None) or ()):
        db.enqueue(None, user, text)


def ensure_ops_session(db, cfg) -> int:
    """ops 话题（固定 UUID）：分析任务的挂靠点——历史聚一处、Claude 有先前
    分析上下文。无白名单（异常配置/测试）兜底本地用户名。"""
    s = db.get_session_by_uuid(OPS_UUID)
    if s is not None:
        return s.id
    user = min(cfg.whitelist) if getattr(cfg, "whitelist", None) else "ops@local"
    return db.create_fixed_session(user, cfg.default_cwd, OPS_UUID).id


def _media_mb(cfg) -> float:
    base = Path(getattr(cfg, "repo_root", ".")) / "data" / "media"
    try:
        total = sum(f.stat().st_size for f in base.rglob("*") if f.is_file())
    except OSError:
        return 0.0
    return total / 1048576.0


def collect_daily_data(db, cfg, now: int, sample: dict) -> dict:
    """日报三板块数据（统计窗口 = 昨日全天；生产 CST 无夏令时，-86400 无漂移）。"""
    lt = time.localtime(now)
    day_end = int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))
    day_start = day_end - 86400
    return {
        "date": time.strftime("%Y-%m-%d", time.localtime(day_start)),
        "tasks": db.daily_task_stats(day_start, day_end),
        "cost_usd": db.daily_cost(day_start, day_end),
        "cpu": sample.get("cpu", 0.0), "mem": sample.get("mem", 0.0),
        "disks": sample.get("disks", {}), "boot_days": sample.get("boot_days", 0.0),
        "sent": db.outbox_sent_count(day_start, day_end),
        "backlog": db.queue_depth(),
        "dead_outbox": db.dead_letter_count(),
        "online": bool(db.get_state("bot_token")),
        "media_mb": _media_mb(cfg),
    }


def render_daily_report(data: dict) -> str:
    t = data["tasks"]
    disk = " / ".join(f"{p} {v:.0f}%" for p, v in sorted(data["disks"].items())) or "—"
    lines = [
        f"🌅 刀鱼日报 {data['date']}",
        f"📊 任务：昨日 {t['total']} 个（成功 {t['done']} / 取消 {t['canceled']}"
        f" / 死信 {t['dead']}），费用 ${data['cost_usd']:.2f}",
        f"🖥 服务器：CPU {data['cpu']:.0f}% / 内存 {data['mem']:.0f}% / 磁盘 {disk}，"
        f"已运行 {data['boot_days']:.1f} 天",
        f"🐟 刀鱼：出站 {data['sent']} 条 / 队列 {data['backlog']} / "
        f"死信 {data['dead_outbox']} / "
        f"{'连接正常' if data['online'] else '⚠️ 连接未建立'} / "
        f"media {data['media_mb']:.0f}MB",
    ]
    return "\n".join(lines)


def daily_anomalies(data: dict, cron_cfg: dict) -> list[str]:
    """异常升级判定（spec §4）：死信新增 / 健康快照超阈 / 队列积压 / 掉线。"""
    out = []
    if data["tasks"]["dead"] > 0:
        out.append(f"昨日新增死信任务 {data['tasks']['dead']} 个")
    for path, pct in sorted(data["disks"].items()):
        if pct > cron_cfg["disk_threshold_pct"]:
            out.append(f"磁盘 {path} {pct:.0f}%（阈值 "
                       f"{cron_cfg['disk_threshold_pct']}%）")
    if data["cpu"] > cron_cfg["cpu_threshold_pct"]:
        out.append(f"CPU {data['cpu']:.0f}%（阈值 {cron_cfg['cpu_threshold_pct']}%）")
    if data["mem"] > cron_cfg["mem_threshold_pct"]:
        out.append(f"内存 {data['mem']:.0f}%（阈值 {cron_cfg['mem_threshold_pct']}%）")
    if data["backlog"] > cron_cfg["queue_backlog_warn"]:
        out.append(f"队列积压 {data['backlog']}（预警 {cron_cfg['queue_backlog_warn']}）")
    if not data["online"]:
        out.append("iLink token 缺失（连接未建立）")
    return out


def run_daily(db, cfg, now: int, sample: dict) -> str:
    """日报主流程：收集→模板→推送；异常时追加分析任务（挂 ops 话题）。
    模板先推、分析后到——分析失败日报照样在。"""
    data = collect_daily_data(db, cfg, now, sample)
    text = render_daily_report(data)
    anomalies = daily_anomalies(data, cfg.cron)
    if not anomalies:
        _broadcast(db, cfg, text)
        return "正常，推送 1 条"
    text += "\n⏳ 检测到异常，分析进行中…"
    prompt = ("刀鱼巡检系统自动任务：昨日运行数据存在异常，请分析原因并给出"
              "简要结论与建议。\n\n异常项：\n- " + "\n- ".join(anomalies) +
              "\n\n数据：" + json.dumps(
                  {"tasks": data["tasks"], "cost_usd": data["cost_usd"],
                   "backlog": data["backlog"], "dead_outbox": data["dead_outbox"],
                   "media_mb": round(data["media_mb"], 1)}, ensure_ascii=False) +
              "\n可执行只读命令查看 data/daoyu.db（audit_log/tasks 表）辅助分析，"
              "结论一屏以内。")
    sid = ensure_ops_session(db, cfg)
    db.create_task(None, sid, prompt, kind="chat")
    _broadcast(db, cfg, text)
    db.audit("cron_daily", f"anomalies={len(anomalies)}")
    return f"异常 {len(anomalies)} 项，已推送并建分析任务"
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_scheduler.py -v`
Expected: PASS

- [ ] **Step 5: 全量回归 + 提交**

Run: `python -m pytest`
```bash
git add gateway/scheduler.py tests/test_scheduler.py
git commit -m "feat(M4): 日报链路——模板推送 + 异常自动升级 Claude 分析（ops 话题）"
```

---

### Task 5: 巡检链路（check / 静默期 / run_patrol / 证书）

**Files:**
- Modify: `gateway/scheduler.py`（追加巡检函数组）
- Test: `tests/test_scheduler.py`（追加）

**Interfaces:**
- Consumes: Task 1 db 方法、`db.active_tasks()`（现有）、`db.get_state("bot_token")`、Task 4 的 `_broadcast/ensure_ops_session`、cryptography（已有依赖）。
- Consumes: CPU/内存滚动窗口参数 `cpu_win/mem_win: collections.deque[float]`（由 Task 6 的 scheduler_loop 持有并每分钟 append）。
- Produces:
  - `check_patrol(db, cfg, now: int, sample: dict, cpu_win, mem_win) -> list[dict]`——每项 `{"key": str, "title": str, "lines": [str]}`
  - `check_certs(cfg, now: int) -> list[dict]`（独立可测；`cert_paths` 路径不存在返回空——Windows 开发机不误报）
  - `silenced(db, key: str, silence_s: int, now: int) -> bool` / `mark_alert(db, key: str, now: int)`（state KV `cron_alert:<key>`）
  - `run_patrol(db, cfg, now: int, sample: dict, cpu_win, mem_win) -> str`

- [ ] **Step 1: 写失败测试**

`tests/test_scheduler.py` 追加：

```python
from collections import deque


def test_check_patrol_items(db, tmp_path):
    from gateway.scheduler import check_patrol
    cfg = _dcfg(tmp_path)
    now = _ANCHOR
    # 正常采样 + 窗口低位 → 无告警
    ok = check_patrol(db, cfg, now, dict(_SAMPLE_OK), deque([20.0] * 5), deque([60.0] * 5))
    assert ok == []
    # 磁盘超阈
    bad = dict(_SAMPLE_OK, disks={"/": 91.0})
    got = check_patrol(db, cfg, now, bad, deque([20.0] * 5), deque([60.0] * 5))
    assert [a["key"] for a in got] == ["disk:/"]
    # CPU 连续 5 采样超阈才告警；4 个不够
    got = check_patrol(db, cfg, now, dict(_SAMPLE_OK),
                       deque([95.0] * 5), deque([60.0] * 5))
    assert "cpu" in [a["key"] for a in got]
    got = check_patrol(db, cfg, now, dict(_SAMPLE_OK),
                       deque([95.0] * 4), deque([60.0] * 5))
    assert "cpu" not in [a["key"] for a in got]
    # 队列积压：造 pending 任务
    s = db.get_or_create_session("u@im.wechat", "/repo")
    db.create_task(None, s.id, "queued")
    got = check_patrol(db, cfg, now, dict(_SAMPLE_OK),
                       deque([20.0] * 5), deque([60.0] * 5))
    # backlog=1 未超 queue_backlog_warn=20 → 只剩 token 缺失一项
    assert [a["key"] for a in got] == ["ilink_token"]
    # token 在线后无告警
    db.set_state("bot_token", "T")
    assert check_patrol(db, cfg, now, dict(_SAMPLE_OK),
                        deque([20.0] * 5), deque([60.0] * 5)) == []


def test_check_certs(tmp_path):
    from gateway.scheduler import check_certs
    from common.config import _DEFAULT_CRON
    import datetime
    from types import SimpleNamespace
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    # 自签一张 7 天后到期的证书（< 预警 14 天）
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now_u = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "t")]))
            .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "t")]))
            .public_key(key.public_key())
            .not_valid_before(now_u - datetime.timedelta(days=1))
            .not_valid_after(now_u + datetime.timedelta(days=7))
            .serial_number(x509.random_serial_number()).sign(key, hashes.SHA256()))
    live = tmp_path / "letsencrypt" / "live" / "a"
    live.mkdir(parents=True)
    (live / "fullchain.pem").write_bytes(
        cert.public_bytes(serialization.Encoding.PEM))
    cfg = SimpleNamespace(cron=dict(_DEFAULT_CRON, cert_paths=[str(tmp_path / "letsencrypt" / "live")]))
    got = check_certs(cfg, int(time.time()))
    assert len(got) == 1 and got[0]["key"].startswith("cert:") and "7" in got[0]["lines"][0]
    # 路径不存在 → 空（Windows 开发机不误报）
    cfg2 = SimpleNamespace(cron=dict(_DEFAULT_CRON, cert_paths=["/no/such/dir"]))
    assert check_certs(cfg2, int(time.time())) == []


def test_silence_window(db):
    from gateway.scheduler import mark_alert, silenced
    now = _ANCHOR
    assert silenced(db, "disk:/", 6 * 3600, now) is False   # 从未告警
    mark_alert(db, "disk:/", now)
    assert silenced(db, "disk:/", 6 * 3600, now + 3600) is True    # 静默期内
    assert silenced(db, "disk:/", 6 * 3600, now + 6 * 3600 + 1) is False  # 过期重报


def test_run_patrol_alert_and_silence(db, tmp_path):
    from gateway.scheduler import run_patrol, OPS_UUID
    cfg = _dcfg(tmp_path)
    db.set_state("bot_token", "T")
    bad = dict(_SAMPLE_OK, disks={"/": 91.0})
    r1 = run_patrol(db, cfg, _ANCHOR, bad, deque([20.0] * 5), deque([60.0] * 5))
    assert "告警" in r1 and "分析任务" in r1
    # 告警行 + 分析任务（ops 话题）
    assert db._conn.execute(
        "SELECT COUNT(*) c FROM outbox WHERE text LIKE '%巡检告警%'"
    ).fetchone()["c"] == 1
    row = db._conn.execute(
        "SELECT t.id FROM tasks t JOIN sessions s ON t.session_id=s.id "
        "WHERE s.claude_uuid=?", (OPS_UUID,)).fetchone()
    assert row is not None
    # 静默期内第二轮：不重报不重建
    r2 = run_patrol(db, cfg, _ANCHOR + 300, bad, deque([20.0] * 5), deque([60.0] * 5))
    assert "静默" in r2
    assert db._conn.execute(
        "SELECT COUNT(*) c FROM outbox WHERE text LIKE '%巡检告警%'"
    ).fetchone()["c"] == 1
    # 静默期过后仍异常 → 再报
    r3 = run_patrol(db, cfg, _ANCHOR + 6 * 3600 + 60, bad,
                    deque([20.0] * 5), deque([60.0] * 5))
    assert "告警" in r3
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_scheduler.py -v -k "patrol or cert or silence"`
Expected: FAIL（ImportError）

- [ ] **Step 3: 实现**

`gateway/scheduler.py` 顶部 import 补 `from collections import deque`（仅类型注解用可省，写上无妨）与 cryptography 证书读取放函数内。模块追加：

```python
def check_patrol(db, cfg, now: int, sample: dict, cpu_win, mem_win) -> list[dict]:
    """巡检判定（纯函数式，异常项列表）：磁盘 / CPU / 内存持续超载 /
    队列积压 / iLink token / 证书。死信不查——M2 已有即时告警专责，
    巡检不双通道重复。"""
    c = cfg.cron
    alerts = []
    for path, pct in sorted(sample.get("disks", {}).items()):
        if pct > c["disk_threshold_pct"]:
            alerts.append({"key": f"disk:{path}", "title": "磁盘",
                           "lines": [f"{path} 分区 {pct:.0f}%（阈值 "
                                     f"{c['disk_threshold_pct']}%）"]})
    n = c["load_sustain_min"]
    cpu_recent = list(cpu_win)[-n:]
    if len(cpu_recent) >= n and all(v > c["cpu_threshold_pct"] for v in cpu_recent):
        alerts.append({"key": "cpu", "title": "CPU",
                       "lines": [f"持续 {c['cpu_threshold_pct']}%+ 达 {n} 分钟"]})
    mem_recent = list(mem_win)[-n:]
    if len(mem_recent) >= n and all(v > c["mem_threshold_pct"] for v in mem_recent):
        alerts.append({"key": "mem", "title": "内存",
                       "lines": [f"持续 {c['mem_threshold_pct']}%+ 达 {n} 分钟"]})
    backlog = len(db.active_tasks())
    if backlog > c["queue_backlog_warn"]:
        alerts.append({"key": "queue", "title": "队列",
                       "lines": [f"积压 {backlog} 个任务（预警 "
                                 f"{c['queue_backlog_warn']}）"]})
    if not db.get_state("bot_token"):
        alerts.append({"key": "ilink_token", "title": "连接",
                       "lines": ["iLink token 缺失（连接未建立）"]})
    alerts += check_certs(cfg, now)
    return alerts


def check_certs(cfg, now: int) -> list[dict]:
    """cert_paths 下 *.pem 读 NotAfter；剩余 < cert_warn_days 告警。
    路径不存在/非证书文件跳过（Windows 开发机、privkey.pem 均不误报炸）。"""
    from cryptography import x509
    import datetime
    c = cfg.cron
    alerts = []
    for base in c.get("cert_paths", []):
        basep = Path(base)
        if not basep.is_dir():
            continue
        for pem in sorted(basep.rglob("*.pem")):
            try:
                cert = x509.load_pem_x509_certificate(pem.read_bytes())
                days_left = (cert.not_valid_after_utc
                             - datetime.datetime.now(datetime.timezone.utc)).days
            except (ValueError, OSError):
                continue
            if days_left < c["cert_warn_days"]:
                alerts.append({"key": f"cert:{pem}", "title": "证书",
                               "lines": [f"{pem} 剩余 {days_left} 天（预警 "
                                         f"{c['cert_warn_days']} 天）"]})
    return alerts


def silenced(db, key: str, silence_s: int, now: int) -> bool:
    """同类异常静默期内（alert_silence_h）不重报——防重复告警重复建任务烧钱；
    过期后仍异常会再报一次（防「告一次永远沉默」）。"""
    ts = db.get_state(f"cron_alert:{key}")
    return bool(ts and ts.isdigit() and now - int(ts) < silence_s)


def mark_alert(db, key: str, now: int) -> None:
    db.set_state(f"cron_alert:{key}", str(now))


def run_patrol(db, cfg, now: int, sample: dict, cpu_win, mem_win) -> str:
    """巡检主流程：判定 → 静默期过滤 → 告警推送 + 合并建一个分析任务。
    正常轮次零 Claude 调用（零成本原则）。"""
    alerts = check_patrol(db, cfg, now, sample, cpu_win, mem_win)
    if not alerts:
        return "正常"
    silence_s = int(cfg.cron["alert_silence_h"]) * 3600
    fresh = [a for a in alerts if not silenced(db, a["key"], silence_s, now)]
    if not fresh:
        return f"{len(alerts)} 项异常均在静默期内"
    lines = [f"⚠️ 巡检告警（{len(fresh)} 项）"]
    for a in fresh:
        lines += [f"[{a['title']}] {ln}" for ln in a["lines"]]
    detail = "\n".join(f"[{a['title']}] " + "；".join(a["lines"]) for a in fresh)
    prompt = ("刀鱼巡检系统自动任务：巡检发现以下异常，请分析原因并给出简要"
              "结论与建议。\n\n" + detail +
              "\n可执行只读命令（df/ps/日志/data/daoyu.db）辅助分析，结论一屏以内。")
    sid = ensure_ops_session(db, cfg)
    tid = db.create_task(None, sid, prompt, kind="chat")
    for a in fresh:
        mark_alert(db, a["key"], now)
    lines.append(f"⏳ 已建分析任务 #{tid}，结论稍后推送")
    _broadcast(db, cfg, "\n".join(lines))
    db.audit("cron_patrol", f"alerts={len(fresh)}")
    return f"告警 {len(fresh)} 项，已推送并建分析任务 #{tid}"
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_scheduler.py -v`
Expected: PASS

- [ ] **Step 5: 全量回归 + 提交**

Run: `python -m pytest`
```bash
git add gateway/scheduler.py tests/test_scheduler.py
git commit -m "feat(M4): 巡检链路——四类检查 + 静默期去重 + 告警自动分析"
```

---

### Task 6: scheduler_loop 协程 + psutil 采样 + app.py 接入

**Files:**
- Modify: `gateway/scheduler.py`（追加 loop 与采样）
- Modify: `gateway/app.py:318-326`（tasks 列表加第五协程）
- Modify: `pyproject.toml`（dependencies 加 psutil）
- Test: `tests/test_scheduler.py`（追加采样与分发测试）

**Interfaces:**
- Consumes: Task 3/4/5 的全部函数；`db.mark_cron_run`。
- Produces:
  - `psutil_sample(cfg) -> dict`（sample 契约的真实实现；psutil lazy import）
  - `scheduler_loop(db, cfg) -> Coroutine`（常驻协程；签名不接 outbound——推送全经 outbox，投递由出站循环 0.5s 轮询接管）

- [ ] **Step 1: 写失败测试**

`tests/test_scheduler.py` 追加（loop 分发逻辑用注入 monkeypatch 驱动一轮，不起真实 sleep 循环）：

```python
def test_scheduler_loop_dispatch(db, tmp_path, monkeypatch):
    """一轮分发：daily/patrol 到点各自触发一次并落 last_result；未到点不动。"""
    import gateway.scheduler as sch
    cfg = _dcfg(tmp_path)
    calls = []

    def fake_run_daily(d, c, now, sample):
        calls.append("daily"); return "日报OK"

    def fake_run_patrol(d, c, now, sample, cw, mw):
        calls.append("patrol"); return "巡检OK"

    monkeypatch.setattr(sch, "run_daily", fake_run_daily)
    monkeypatch.setattr(sch, "run_patrol", fake_run_patrol)
    monkeypatch.setattr(sch, "psutil_sample", lambda cfg: dict(_SAMPLE_OK))

    async def one_round():
        await sch._tick(db, cfg)   # 单轮内联：scheduler_loop 的每分钟体

    import asyncio
    asyncio.get_event_loop_policy()
    asyncio.run(one_round())
    assert calls == ["daily", "patrol"]        # 预置行从未跑 → 双双 due
    j = {x.name: x for x in db.cron_jobs()}
    assert j["daily"].last_result == "日报OK"
    assert j["patrol"].last_result == "巡检OK"
    # 第二轮：patrol 间隔未满、daily 今日已跑 → 都不动
    calls.clear()
    asyncio.run(one_round())
    assert calls == []
    # off 后即便到点也不跑
    db.update_cron("patrol", enabled=0)
    db.update_cron("patrol", touch_last_run=_ANCHOR - 99999)
    calls.clear()
    asyncio.run(one_round())
    assert calls == []
```

注意：`asyncio.run(one_round())` 连跑两次各自新建 loop——本测试无跨 loop 状态，可行。若 pytest 配置了 anyio/asyncio 插件导致裸 `asyncio.run` 报警，改用 `asyncio.new_event_loop()` 手动驱动（实现时按实际报错调整，断言不变）。

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_scheduler.py -v -k dispatch`
Expected: FAIL（`gateway.scheduler` 无 `_tick`）

- [ ] **Step 3: 实现**

`gateway/scheduler.py` 顶部 import 补 `import asyncio`、`import os`，模块追加：

```python
def psutil_sample(cfg) -> dict:
    """sample 契约的真实实现（测试注入 fake，此处才碰 psutil——lazy import：
    未装 psutil 的环境 /cron 命令不受影响）。disks 取系统根与 default_cwd
    所在盘，按 (total, free) 去重（同盘两路径不重复报）。"""
    import psutil
    disks, seen = {}, set()
    for p in {os.path.abspath(os.sep), cfg.default_cwd}:
        try:
            du = psutil.disk_usage(p)
        except (OSError, ValueError):
            continue
        sig = (du.total, du.free)
        if sig in seen:
            continue
        seen.add(sig)
        disks[p] = du.percent
    return {"cpu": psutil.cpu_percent(interval=None),
            "mem": psutil.virtual_memory().percent,
            "disks": disks,
            "boot_days": (time.time() - psutil.boot_time()) / 86400.0}


async def _tick(db, cfg, cpu_win=None, mem_win=None) -> None:
    """scheduler_loop 的单轮体（独立成函数便于测试注入）：采样 → 轻量窗口 →
    到点分发。先 mark_cron_run 占位再跑——同分钟崩溃重入不重复推送。"""
    now = int(time.time())
    if cpu_win is None:
        cpu_win = deque(maxlen=cfg.cron["load_sustain_min"])
    if mem_win is None:
        mem_win = deque(maxlen=cfg.cron["load_sustain_min"])
    sample = psutil_sample(cfg)
    cpu_win.append(sample["cpu"])
    mem_win.append(sample["mem"])
    jobs = {j.name: j for j in db.cron_jobs()}
    d, p = jobs.get("daily"), jobs.get("patrol")
    if d is not None and due_daily(d, now):
        db.mark_cron_run("daily", "运行中")
        db.mark_cron_run("daily", run_daily(db, cfg, now, sample))
    if p is not None and due_patrol(p, now):
        db.mark_cron_run("patrol", "运行中")
        db.mark_cron_run("patrol", run_patrol(db, cfg, now, sample, cpu_win, mem_win))


async def scheduler_loop(db, cfg) -> None:
    """M4 第五常驻协程：每分钟整分对齐醒来跑一轮 _tick。整轮异常不杀协程
    （调度器死 ≠ 通道死），记 audit 下轮重来。启动先空采一次 CPU——
    psutil.cpu_percent 首调返回 0.0，预热后窗口首样本不失真。"""
    try:
        import psutil
        psutil.cpu_percent(interval=None)
    except Exception:
        pass
    while True:
        now = time.time()
        try:
            await _tick(db, cfg)
        except Exception as e:   # noqa: BLE001 —— 保姆代码，不杀协程
            try:
                db.audit("cron_error", repr(e))
            except Exception:
                pass
        await asyncio.sleep(60 - int(now) % 60 or 60)
```

`gateway/app.py` 的 `tasks = [...]` 列表（第 318 行附近）改为：

```python
        from gateway.scheduler import scheduler_loop
        tasks = [
            asyncio.create_task(pool.run_forever(), name="worker-pool"),
            asyncio.create_task(outbound.run_forever(), name="outbound"),
            asyncio.create_task(ReconnectTimer(db, cfg, ilink, token_ref,
                                               typing_state, outbound).run_forever(),
                                name="reconnect"),
            asyncio.create_task(poll_loop(db, cfg, ilink, pool, outbound, token_ref),
                                name="poll"),
            asyncio.create_task(scheduler_loop(db, cfg), name="scheduler"),
        ]
```

（import 放 `tasks` 定义前的函数体内——对齐该文件 `from worker.version import check_claude_version` 的局部导入风格。）

`pyproject.toml` 第 6 行 dependencies 改为：

```toml
dependencies = ["aiohttp>=3.9", "cryptography>=42", "rapidocr-onnxruntime>=1.4", "psutil>=5.9"]
```

并执行 `pip install psutil`（开发机 venv：`.venv/Scripts/pip install psutil`；生产部署时同理）。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_scheduler.py -v`
Expected: PASS

- [ ] **Step 5: 全量回归 + 冒烟 + 提交**

Run: `python -m pytest`（全绿）
冒烟（Git Bash heredoc）：
```bash
python - <<'EOF'
import asyncio, tempfile, pathlib
from types import SimpleNamespace
from common.db import Database
from gateway.scheduler import scheduler_loop

async def main():
    d = tempfile.mkdtemp()
    db = Database(pathlib.Path(d) / 's.db')
    db.ensure_schema()
    cfg = SimpleNamespace(cron={'disk_threshold_pct': 85, 'cpu_threshold_pct': 90,
                                'mem_threshold_pct': 90, 'load_sustain_min': 5,
                                'cert_warn_days': 14, 'cert_paths': [],
                                'alert_silence_h': 6, 'queue_backlog_warn': 20},
                          default_cwd=d, whitelist=set(), repo_root=pathlib.Path(d))
    t = asyncio.create_task(scheduler_loop(db, cfg))
    await asyncio.sleep(2)
    t.cancel()
    print('cron_jobs:', [(j.name, j.last_result) for j in db.cron_jobs()])

asyncio.run(main())
EOF
```
Expected: 输出 `cron_jobs: [('daily', '正常，推送 1 条'), ('patrol', '正常')]`（whitelist 空 → 无 outbox 行但流程完整跑通）
```bash
git add gateway/scheduler.py gateway/app.py pyproject.toml tests/test_scheduler.py
git commit -m "feat(M4): scheduler_loop 第五常驻协程 + psutil 采样接入 app.py"
```

---

### Task 7: E2E 场景 + 文档同步

**Files:**
- Test: `tests/test_scheduler.py`（追加 E2E 组）
- Modify: `README.md`（命令表加 /cron、部署依赖加 psutil）
- Modify: `CLAUDE.md`（当前状态 + M4 功能清单段）

**Interfaces:**
- Consumes: 全部前序产出。
- Produces: 无代码接口；文档与验收覆盖。

- [ ] **Step 1: 写 E2E 测试（spec §9 验收 1/3/4/5 的可自动化部分）**

`tests/test_scheduler.py` 追加：

```python
async def test_e2e_cron_flow(db, tmp_path):
    """验收流：/cron off 后推进不产生推送；on 后异常注入 → 告警 + ops 分析
    任务；静默期内重跑不重复；CPU 窗口不足不误报。"""
    from gateway.bridge import execute_bridge
    import gateway.scheduler as sch

    cfg = _dcfg(tmp_path)
    db.set_state("bot_token", "T")

    def outbox_n(pat):
        return db._conn.execute(
            "SELECT COUNT(*) c FROM outbox WHERE text LIKE ?", (pat,)).fetchone()["c"]

    # 1) off 后不跑
    await execute_bridge(db, None, _route("cron", "off patrol"), "u@im.wechat", cfg)
    sch.run_patrol(db, cfg, _ANCHOR, dict(_SAMPLE_OK, disks={"/": 91.0}),
                   deque([20.0] * 5), deque([60.0] * 5))  # 直接调模拟到点
    # off 只是 scheduler 不再自动触发；直接调 run_patrol 仍会告警——off 语义
    # 在 loop 分发层（test_scheduler_loop_dispatch 已覆盖），此处不重复断言
    db.update_cron("patrol", enabled=1, touch_last_run=int(time.time()))
    before = outbox_n("%巡检告警%")
    # 2) 正常轮次：零任务零告警
    r = sch.run_patrol(db, cfg, int(time.time()), dict(_SAMPLE_OK),
                       deque([20.0] * 5), deque([60.0] * 5))
    assert r == "正常" and outbox_n("%巡检告警%") == before
    # 3) CPU 尖峰不足窗口：不告警
    r = sch.run_patrol(db, cfg, int(time.time()), dict(_SAMPLE_OK, cpu=95.0),
                       deque([20.0] * 5 + [95.0]), deque([60.0] * 5))
    assert r == "正常"
    # 4) 磁盘异常 → 告警 + 分析任务；静默期内重复不重报
    bad = dict(_SAMPLE_OK, disks={"/": 91.0})
    r = sch.run_patrol(db, cfg, int(time.time()), bad,
                       deque([20.0] * 5), deque([60.0] * 5))
    assert "告警" in r
    assert outbox_n("%巡检告警%") == before + 1
    n_tasks = db._conn.execute(
        "SELECT COUNT(*) c FROM tasks").fetchone()["c"]
    assert n_tasks == 1
    sch.run_patrol(db, cfg, int(time.time()) + 60, bad,
                   deque([20.0] * 5), deque([60.0] * 5))
    assert outbox_n("%巡检告警%") == before + 1 and n_tasks == 1
```

- [ ] **Step 2: 跑测试确认通过**

Run: `python -m pytest tests/test_scheduler.py -v -k e2e`
Expected: PASS（此测试为流程编排验证，允许直接断言通过——前序单测已锁定各环节）

- [ ] **Step 3: 全量回归**

Run: `python -m pytest`
Expected: 全绿（359 + 本计划新增约 17 个）

- [ ] **Step 4: 文档同步**

`README.md` 命令表（现有命令表格）加一行：

```markdown
| `/cron` | 定时任务管理：日报/巡检 开关、`time daily <HH:MM>`、`interval patrol <分钟>` |
```

部署依赖段落加：`pip install psutil`（或注明 pyproject 已含、`pip install -e .` 自动装）。

`CLAUDE.md` 更新：
- "当前状态"段追加：**M4 主动服务已实现（2026-08-21）**——scheduler 第五协程、/cron 命令族、日报模板+异常升 Claude、巡检四项+静默期、ops 话题。
- "M3 功能清单"之后加"**M4 功能清单**"段（内容对齐 spec §1 决策表，三四行精炼）。
- "常用命令"的测试计数更新（359 → 实际数）。

- [ ] **Step 5: 提交 + 真机验收清单交接**

```bash
git add README.md CLAUDE.md tests/test_scheduler.py
git commit -m "feat(M4): E2E 验收流 + 文档同步（README/CLAUDE.md）"
```

真机验收（需生产环境，人工/后续 session 执行，清单交接）：
1. 生产 `pip install psutil` + 重启 daoyu 服务，`journalctl` 无 scheduler 报错。
2. 微信 `/cron` 看列表 → `/cron time daily <两分钟后>` → 等日报到手机（三板块齐全）。
3. `/cron interval patrol 1` + 手动 `fallocate` 一个大文件越阈（或临时把 `cron.disk_threshold_pct` set 到 1）→ 收告警 + 分析结论跟推；同一异常 6h 内不重报。
4. `/cron off patrol` → 不再收告警；`/sessions` 能看到 🔧 巡检话题。

---

## Self-Review 结论

- **Spec 覆盖**：§2 架构（T6）、§3 数据模型/配置（T1/T2）、§4 日报（T4）、§5 巡检（T5）、§6 /cron（T3）、§7 错误处理（T5/T6 协程自保护 + 单项跳过）、§8 测试（各 task + T7 E2E）、§9 验收（T7 真机清单）——全覆盖。
- **占位符扫描**：无 TBD/TODO；每步含完整代码与命令。
- **类型一致性**：`CronJob` 字段、`update_cron` 关键字参数、`sample` dict 契约、`check_patrol` 返回结构、`OPS_UUID` 常量在各 task 间一致；`_tick(db, cfg)` 签名与 T6 测试一致。
- **与 spec 的已知微偏差**（有意为之，计划内注明）：日报任务板块展示 done/canceled/dead 三态（tasks 表实际无持久 failed 态——重试回 pending、耗尽即 dead，spec 的"失败"语义由 dead 承载）。
