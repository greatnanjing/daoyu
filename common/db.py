"""SQLite 唯一事实源。所有访问都在事件循环线程同步执行（WAL 下微秒级，M1 并发 2~3 可接受）。"""
import json
import sqlite3
import time
from pathlib import Path

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
"""


class Database:
    def __init__(self, path: str | Path):
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
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
