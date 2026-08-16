"""SQLite 唯一事实源。所有访问都在事件循环线程同步执行（WAL 下微秒级，M1 并发 2~3 可接受）。"""
import json
import sqlite3
import time
import uuid
from pathlib import Path

from common.models import InboundMessage, OutboxItem, SessionBinding, Task

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  wechat_user TEXT NOT NULL,
  cwd TEXT NOT NULL,
  claude_uuid TEXT NOT NULL,
  policy TEXT NOT NULL DEFAULT 'auto',
  created_at INTEGER NOT NULL,
  last_active_at INTEGER NOT NULL,
  UNIQUE(wechat_user, cwd, claude_uuid)
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(wechat_user, last_active_at);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  msg_id TEXT UNIQUE NOT NULL,
  from_user TEXT NOT NULL,
  text TEXT NOT NULL DEFAULT '',
  context_token TEXT NOT NULL DEFAULT '',
  received_at INTEGER NOT NULL,
  state TEXT NOT NULL DEFAULT 'received'
);
CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  message_id INTEGER REFERENCES messages(id),
  session_id INTEGER NOT NULL REFERENCES sessions(id),
  prompt TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'chat',
  state TEXT NOT NULL DEFAULT 'pending',
  attempts INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 3,
  claude_bg_id TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_state ON tasks(state, created_at);
CREATE TABLE IF NOT EXISTS outbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER REFERENCES tasks(id),
  to_user TEXT NOT NULL,
  text TEXT NOT NULL,
  seq INTEGER NOT NULL DEFAULT 0,
  state TEXT NOT NULL DEFAULT 'pending',
  attempts INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 5,
  last_error TEXT,
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_outbox_state ON outbox(state, id);
CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL,
  kind TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS state (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS approvals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER NOT NULL,
  to_user TEXT NOT NULL,
  tool_name TEXT NOT NULL,
  input_json TEXT NOT NULL DEFAULT '',
  state TEXT NOT NULL DEFAULT 'pending',
  created_at INTEGER NOT NULL,
  decided_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_approvals_state ON approvals(state, id);
"""


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row

    def ensure_schema(self) -> None:
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._migrate_sessions_table()
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def _migrate_sessions_table(self) -> None:
        """旧 sessions 表（UNIQUE(wechat_user, cwd)，一目录一会话）无损迁移为
        新表（UNIQUE(wechat_user, cwd, claude_uuid)，一目录多话题）。SQLite 不能
        ALTER 约束：建 sessions_v2 → INSERT SELECT 全部旧行（保留所有字段含 id，
        tasks.session_id 外键不漂移）→ DROP 旧表 → RENAME。检测 sqlite_master 建表
        SQL 是否已含新约束来决定是否迁移（幂等）。FK 开关必须在事务外设置
        （PRAGMA 在事务内是 no-op），DROP 父表期间关 FK 防孤儿检查。"""
        new_unique = "UNIQUE(wechat_user, cwd, claude_uuid)"
        row = self._conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='sessions'"
        ).fetchone()
        if row is None or new_unique in (row["sql"] or ""):
            return   # 新库（_SCHEMA 直接建新约束）/ 已迁移
        self._conn.execute("PRAGMA foreign_keys=OFF")
        try:
            self._conn.execute("BEGIN")
            self._conn.execute(
                "CREATE TABLE sessions_v2 ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "wechat_user TEXT NOT NULL, "
                "cwd TEXT NOT NULL, "
                "claude_uuid TEXT NOT NULL, "
                "policy TEXT NOT NULL DEFAULT 'auto', "
                "created_at INTEGER NOT NULL, "
                "last_active_at INTEGER NOT NULL, "
                + new_unique + ")")
            self._conn.execute(
                "INSERT INTO sessions_v2(id, wechat_user, cwd, claude_uuid, policy, "
                "created_at, last_active_at) "
                "SELECT id, wechat_user, cwd, claude_uuid, policy, created_at, "
                "last_active_at FROM sessions")
            self._conn.execute("DROP TABLE sessions")
            self._conn.execute("ALTER TABLE sessions_v2 RENAME TO sessions")
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        finally:
            self._conn.execute("PRAGMA foreign_keys=ON")

    # ---- state KV（bot_token / get_updates_buf / slash_commands / cwd 指针等）----
    def get_state(self, key: str, default: str | None = None) -> str | None:
        row = self._conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_state(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO state(key, value, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, value, int(time.time())))
        self._conn.commit()

    def delete_state(self, key: str) -> None:
        """删 KV（幂等，key 不存在也成功）。如 bg_blocked_since:<task_id> 计时。"""
        self._conn.execute("DELETE FROM state WHERE key=?", (key,))
        self._conn.commit()

    # ---- audit ----
    def audit(self, kind: str, detail: str) -> None:
        self._conn.execute(
            "INSERT INTO audit_log(ts, kind, detail) VALUES(?,?,?)",
            (int(time.time()), kind, detail))
        self._conn.commit()

    def today_cost_usd(self) -> float:
        lt = time.localtime()
        day_start = int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))
        total = 0.0
        for row in self._conn.execute(
                "SELECT detail FROM audit_log WHERE kind='cost' AND ts>=?", (day_start,)):
            try:
                total += float(json.loads(row["detail"]).get("usd", 0))
            except (ValueError, TypeError, AttributeError):
                pass
        return total

    def queue_depth(self) -> int:
        return self._conn.execute(
            "SELECT COUNT(*) c FROM tasks WHERE state='pending'").fetchone()["c"]

    # ---- messages ----
    def insert_message(self, msg: InboundMessage) -> int | None:
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO messages(msg_id, from_user, text, context_token, received_at) "
            "VALUES(?,?,?,?,?)",
            (msg.msg_id, msg.from_user, msg.text, msg.context_token, msg.received_at))
        self._conn.commit()
        return cur.lastrowid if cur.rowcount else None

    def latest_context_token(self, from_user: str) -> str | None:
        row = self._conn.execute(
            "SELECT context_token FROM messages WHERE from_user=? "
            "ORDER BY id DESC LIMIT 1", (from_user,)).fetchone()
        return row["context_token"] if row else None

    # ---- sessions（同目录多话题）----
    def get_or_create_session(self, wechat_user: str, cwd: str) -> SessionBinding:
        """该目录若无话题行则建；已有一行或多行时返回最新（last_active_at DESC，
        id DESC 决胜）。迁移期兼容既有调用方（单目录单会话假设下行为不变）。"""
        row = self._conn.execute(
            "SELECT * FROM sessions WHERE wechat_user=? AND cwd=? "
            "ORDER BY last_active_at DESC, id DESC LIMIT 1",
            (wechat_user, cwd)).fetchone()
        if row is None:
            now = int(time.time())
            cur = self._conn.execute(
                "INSERT INTO sessions(wechat_user, cwd, claude_uuid, policy, created_at, last_active_at) "
                "VALUES(?,?,?,?,?,?)",
                (wechat_user, cwd, str(uuid.uuid4()), "auto", now, now))
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM sessions WHERE id=?", (cur.lastrowid,)).fetchone()
        return SessionBinding(**dict(row))

    def create_topic(self, wechat_user: str, cwd: str) -> SessionBinding:
        """同目录开新话题：总是新建行（新 claude_uuid、policy='auto'），当前话题
        指针切过去。"""
        now = int(time.time())
        cur = self._conn.execute(
            "INSERT INTO sessions(wechat_user, cwd, claude_uuid, policy, created_at, last_active_at) "
            "VALUES(?,?,?,?,?,?)",
            (wechat_user, cwd, str(uuid.uuid4()), "auto", now, now))
        self._conn.commit()
        row = self._conn.execute(
            "SELECT * FROM sessions WHERE id=?", (cur.lastrowid,)).fetchone()
        binding = SessionBinding(**dict(row))
        self.set_active_session(wechat_user, binding.id)
        return binding

    def latest_topic_in(self, wechat_user: str, cwd: str) -> SessionBinding | None:
        """该目录最新话题行（/cd <路径> 指向用）；目录无话题返回 None。"""
        row = self._conn.execute(
            "SELECT * FROM sessions WHERE wechat_user=? AND cwd=? "
            "ORDER BY last_active_at DESC, id DESC LIMIT 1",
            (wechat_user, cwd)).fetchone()
        return SessionBinding(**dict(row)) if row else None

    def get_session(self, session_id: int) -> SessionBinding | None:
        row = self._conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        return SessionBinding(**dict(row)) if row else None

    def set_policy(self, session_id: int, policy: str) -> None:
        self._conn.execute("UPDATE sessions SET policy=? WHERE id=?", (policy, session_id))
        self._conn.commit()

    def touch_session(self, session_id: int) -> None:
        self._conn.execute(
            "UPDATE sessions SET last_active_at=? WHERE id=?",
            (int(time.time()), session_id))
        self._conn.commit()

    def list_sessions(self, wechat_user: str) -> list[SessionBinding]:
        """全部话题，按 last_active_at DESC 全局排序（id DESC 决胜，同秒创建时
        序号确定）。/sessions 展示与 /cd #n 解析同用此序。"""
        rows = self._conn.execute(
            "SELECT * FROM sessions WHERE wechat_user=? "
            "ORDER BY last_active_at DESC, id DESC",
            (wechat_user,)).fetchall()
        return [SessionBinding(**dict(r)) for r in rows]

    def last_task_summary(self, session_id: int) -> str | None:
        """/sessions 摘要：该话题最后一条任务的 prompt 截 30 字；bg 加前缀；无任务 None。"""
        row = self._conn.execute(
            "SELECT prompt, kind FROM tasks WHERE session_id=? ORDER BY id DESC LIMIT 1",
            (session_id,)).fetchone()
        if row is None:
            return None
        prompt = row["prompt"][:30]
        return f"[bg] {prompt}" if row["kind"] == "bg" else prompt

    # ---- 每用户指针：当前 cwd（旧）+ 当前话题（state KV）----
    def set_active_cwd(self, wechat_user: str, cwd: str) -> None:
        self.set_state(f"cwd:{wechat_user}", cwd)

    def get_active_cwd(self, wechat_user: str, default: str) -> str:
        return self.get_state(f"cwd:{wechat_user}", default) or default

    def set_active_session(self, wechat_user: str, session_id: int) -> None:
        """当前话题指针：state KV active_session:<user> 存 sessions 行 id。"""
        self.set_state(f"active_session:{wechat_user}", str(session_id))

    def get_active_binding(self, wechat_user: str, default_cwd: str,
                           touch: bool = True) -> SessionBinding:
        """用户当前话题。指针有效 → 该行；指针缺失/失效（老库首次、脏数据）→
        经旧 cwd 指针兼容推导（无则 default_cwd）取/建该目录最新话题并回写指针。
        touch=True（默认，真实使用路径）顺带 touch_session 维护活跃时间；纯查看
        （/sessions、/cd 展示）传 touch=False，避免查看本身改变全局序号。"""
        raw = self.get_state(f"active_session:{wechat_user}")
        if raw and raw.isdigit():
            s = self.get_session(int(raw))
            if s is not None and s.wechat_user == wechat_user:
                if touch:
                    self.touch_session(s.id)
                    s = self.get_session(s.id)
                return s
        cwd = self.get_active_cwd(wechat_user, default_cwd)
        s = self.get_or_create_session(wechat_user, cwd)
        self.set_active_session(wechat_user, s.id)
        if touch:
            self.touch_session(s.id)
            s = self.get_session(s.id)
        return s

    # ---- tasks ----
    def create_task(self, message_id: int | None, session_id: int, prompt: str,
                    kind: str = "chat", max_attempts: int = 3) -> int:
        now = int(time.time())
        cur = self._conn.execute(
            "INSERT INTO tasks(message_id, session_id, prompt, kind, state, "
            "max_attempts, created_at, updated_at) VALUES(?,?,?,?, 'pending',?,?,?)",
            (message_id, session_id, prompt, kind, max_attempts, now, now))
        self._conn.commit()
        return cur.lastrowid

    def _task_row(self, row) -> Task:
        return Task(id=row["id"], message_id=row["message_id"], session_id=row["session_id"],
                    prompt=row["prompt"], kind=row["kind"], state=row["state"],
                    attempts=row["attempts"], max_attempts=row["max_attempts"],
                    created_at=row["created_at"], updated_at=row["updated_at"],
                    claude_bg_id=row["claude_bg_id"])

    def get_task(self, task_id: int) -> Task | None:
        row = self._conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return self._task_row(row) if row else None

    def pending_sessions(self) -> list[int]:
        rows = self._conn.execute(
            "SELECT session_id FROM tasks WHERE state='pending' "
            "GROUP BY session_id ORDER BY MIN(id)").fetchall()
        return [r["session_id"] for r in rows]

    def claim_next_pending(self, session_ids: set[int]) -> Task | None:
        """pending→running（attempts+1）。UPDATE...RETURNING 单语句原子领取，防并发双取。"""
        for sid in session_ids:
            cur = self._conn.execute(
                "UPDATE tasks SET state='running', attempts=attempts+1, updated_at=? "
                "WHERE id=(SELECT id FROM tasks WHERE state='pending' AND session_id=? "
                "ORDER BY id LIMIT 1) RETURNING *",
                (int(time.time()), sid))
            row = cur.fetchone()
            self._conn.commit()
            if row:
                return self._task_row(row)
        return None

    def finish_task(self, task_id: int, state: str) -> None:
        if state == "failed":
            row = self._conn.execute(
                "SELECT attempts, max_attempts FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row and row["attempts"] < row["max_attempts"]:
                state = "pending"   # 未耗尽 → 回队列重试
            else:
                state = "dead"      # 重试耗尽 → 死信（task context 确认：否则 dead）
        self._conn.execute(
            "UPDATE tasks SET state=?, updated_at=? WHERE id=?",
            (state, int(time.time()), task_id))
        self._conn.commit()

    def set_bg_id(self, task_id: int, bg_id: str) -> None:
        """--bg 启动成功后落盘后台任务 id（watcher 按 id 匹配 agents 条目）。"""
        self._conn.execute(
            "UPDATE tasks SET claude_bg_id=?, updated_at=? WHERE id=?",
            (bg_id, int(time.time()), task_id))
        self._conn.commit()

    def running_bg_tasks(self) -> list[Task]:
        """bg watcher 的监视对象：已启动（有 bg id）且仍在 running 的后台任务。"""
        rows = self._conn.execute(
            "SELECT * FROM tasks WHERE kind='bg' AND state='running' "
            "AND claude_bg_id IS NOT NULL ORDER BY id").fetchall()
        return [self._task_row(r) for r in rows]

    def reset_running_tasks(self) -> int:
        """崩溃恢复：running → pending 重跑。例外：已有 claude_bg_id 的 bg 任务
        保持 running——它的本体在 claude 后台守护进程里，网关重启不代表任务死了，
        重跑 = 重复烧预算；改由 bg watcher 按 agents 列表接管（条目消失 → canceled）。
        尚无 bg_id 的 bg 任务（启动途中崩溃）照常重置。"""
        cur = self._conn.execute(
            "UPDATE tasks SET state='pending', updated_at=? WHERE state='running' "
            "AND NOT (kind='bg' AND claude_bg_id IS NOT NULL)",
            (int(time.time()),))
        self._conn.commit()
        return cur.rowcount

    def active_tasks(self) -> list[Task]:
        rows = self._conn.execute(
            "SELECT * FROM tasks WHERE state IN ('running','pending') ORDER BY id").fetchall()
        return [self._task_row(r) for r in rows]

    def cancel_task(self, task_id: int) -> bool:
        cur = self._conn.execute(
            "UPDATE tasks SET state='canceled', updated_at=? WHERE id=? AND state='pending'",
            (int(time.time()), task_id))
        self._conn.commit()
        return bool(cur.rowcount)

    # ---- outbox ----
    def enqueue(self, task_id: int | None, to_user: str, text: str) -> int:
        cur = self._conn.execute(
            "INSERT INTO outbox(task_id, to_user, text, created_at) VALUES(?,?,?,?)",
            (task_id, to_user, text, int(time.time())))
        self._conn.commit()
        return cur.lastrowid

    def _outbox_row(self, row) -> OutboxItem:
        return OutboxItem(id=row["id"], task_id=row["task_id"], to_user=row["to_user"],
                          text=row["text"], seq=row["seq"], state=row["state"],
                          attempts=row["attempts"], max_attempts=row["max_attempts"],
                          last_error=row["last_error"], created_at=row["created_at"])

    def get_outbox(self, outbox_id: int) -> OutboxItem | None:
        row = self._conn.execute("SELECT * FROM outbox WHERE id=?", (outbox_id,)).fetchone()
        return self._outbox_row(row) if row else None

    def next_outbox_batch(self, limit: int = 10) -> list[OutboxItem]:
        cur = self._conn.execute(
            "UPDATE outbox SET attempts=attempts+1 WHERE id IN "
            "(SELECT id FROM outbox WHERE state='pending' ORDER BY id LIMIT ?) "
            "RETURNING *", (limit,))
        rows = cur.fetchall()
        self._conn.commit()
        return [self._outbox_row(r) for r in rows]

    def mark_sent(self, outbox_id: int) -> None:
        self._conn.execute("UPDATE outbox SET state='sent' WHERE id=?", (outbox_id,))
        self._conn.commit()

    def mark_send_failed(self, outbox_id: int, error: str) -> None:
        row = self._conn.execute(
            "SELECT attempts, max_attempts FROM outbox WHERE id=?", (outbox_id,)).fetchone()
        state = "dead" if row and row["attempts"] >= row["max_attempts"] else "pending"
        self._conn.execute(
            "UPDATE outbox SET state=?, last_error=? WHERE id=?", (state, error, outbox_id))
        self._conn.commit()

    def dead_letter_count(self) -> int:
        return self._conn.execute(
            "SELECT COUNT(*) c FROM outbox WHERE state='dead'").fetchone()["c"]

    def retry_failed_outbox(self) -> int:
        """启动恢复兜底：failed→pending（正常路径 mark_send_failed 不留 failed 态）。"""
        cur = self._conn.execute("UPDATE outbox SET state='pending' WHERE state='failed'")
        self._conn.commit()
        return cur.rowcount

    # ---- approvals（审批，M2）----
    def create_approval(self, task_id: int, to_user: str, tool_name: str,
                        input_json: str) -> int:
        cur = self._conn.execute(
            "INSERT INTO approvals(task_id, to_user, tool_name, input_json, created_at) "
            "VALUES(?,?,?,?,?)",
            (task_id, to_user, tool_name, input_json, int(time.time())))
        self._conn.commit()
        return cur.lastrowid

    def decide_approval(self, approval_id: int, state: str) -> bool:
        """仅 pending 可改 approved/denied/expired；返回是否生效。"""
        cur = self._conn.execute(
            "UPDATE approvals SET state=?, decided_at=? "
            "WHERE id=? AND state='pending'",
            (state, int(time.time()), approval_id))
        self._conn.commit()
        return bool(cur.rowcount)

    def pending_approval(self, to_user: str):
        """最早一条 pending 审批。created_at 超 330s（300s 审批超时 + 30s 轮询
        余量）的 stale 行不返回：主进程被 cgroup 整组杀死时 approval server 孙
        进程来不及自置 expired，永久 pending 行会一直劫持用户的 Y/N 拦截。"""
        return self._conn.execute(
            "SELECT * FROM approvals WHERE to_user=? AND state='pending' "
            "AND created_at > ? ORDER BY id LIMIT 1",
            (to_user, int(time.time()) - 330)).fetchone()

    def get_approval(self, approval_id: int):
        return self._conn.execute(
            "SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
