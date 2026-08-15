import json

import pytest

from common.db import Database  # noqa: F401（conftest 的 db fixture 依赖）


def test_ensure_schema_idempotent(db, tmp_path):
    db.ensure_schema()  # 二次调用不报错
    names = {r[0] for r in db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"messages", "tasks", "outbox", "sessions", "audit_log", "state"} <= names


def test_wal_mode(db, tmp_path):
    mode = db._conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode == "wal"


def test_state_kv_roundtrip(db):
    assert db.get_state("missing", "dft") == "dft"
    db.set_state("bot_token", "T1")
    assert db.get_state("bot_token") == "T1"
    db.set_state("bot_token", "T2")  # 覆盖
    assert db.get_state("bot_token") == "T2"


def test_audit_and_cost(db):
    db.audit("cost", json.dumps({"task_id": 1, "usd": 0.42}))
    assert db.today_cost_usd() == pytest.approx(0.42)
    db.audit("cost", json.dumps({"task_id": 2, "usd": 0.08}))
    assert db.today_cost_usd() == pytest.approx(0.50)
