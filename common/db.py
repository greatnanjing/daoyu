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
  UNIQUE(wechat_user, cwd)
);
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
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

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

    # ---- sessions ----
    def get_or_create_session(self, wechat_user: str, cwd: str) -> SessionBinding:
        row = self._conn.execute(
            "SELECT * FROM sessions WHERE wechat_user=? AND cwd=?",
            (wechat_user, cwd)).fetchone()
        if row is None:
            now = int(time.time())
            self._conn.execute(
                "INSERT INTO sessions(wechat_user, cwd, claude_uuid, policy, created_at, last_active_at) "
                "VALUES(?,?,?,?,?,?)",
                (wechat_user, cwd, str(uuid.uuid4()), "auto", now, now))
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM sessions WHERE wechat_user=? AND cwd=?",
                (wechat_user, cwd)).fetchone()
        return SessionBinding(**dict(row))

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
        rows = self._conn.execute(
            "SELECT * FROM sessions WHERE wechat_user=? ORDER BY last_active_at DESC",
            (wechat_user,)).fetchall()
        return [SessionBinding(**dict(r)) for r in rows]

    # ---- 每用户当前 cwd 指针（state KV）----
    def set_active_cwd(self, wechat_user: str, cwd: str) -> None:
        self.set_state(f"cwd:{wechat_user}", cwd)

    def get_active_cwd(self, wechat_user: str, default: str) -> str:
        return self.get_state(f"cwd:{wechat_user}", default) or default

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

    def reset_running_tasks(self) -> int:
        cur = self._conn.execute(
            "UPDATE tasks SET state='pending', updated_at=? WHERE state='running'",
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
        return self._conn.execute(
            "SELECT * FROM approvals WHERE to_user=? AND state='pending' "
            "ORDER BY id LIMIT 1", (to_user,)).fetchone()

    def get_approval(self, approval_id: int):
        return self._conn.execute(
            "SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
