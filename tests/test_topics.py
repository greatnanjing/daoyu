"""同目录多话题会话：schema 无损迁移 / /new / /sessions 两级展示 / 话题级切换
与档位独立 / chat 路由走话题指针 / 老库兼容。"""
import time

from common.db import Database
from gateway.app import handle_inbound
from gateway.bridge import execute_bridge
from gateway.router import Route

USER = "u@im.wechat"


class FakeCfg:
    def __init__(self):
        self.reconnect = {"session_duration_s": 86400}
        self.default_cwd = "/repo"
        self.whitelist = {USER}


class InboundCfg:
    """handle_inbound 最小配置（whitelist / default_cwd）。"""

    def __init__(self):
        self.whitelist = {USER}
        self.default_cwd = "/repo"


class FakePool:
    def snapshot(self):
        return []

    def running_session_ids(self):
        return set()


def _route(cmd, args="", kind="bridge"):
    return Route(kind=kind, command=cmd, args=args, detail={})


def _pin(db, session_id: int, ts: int) -> None:
    """钉死 last_active_at（同秒创建无法靠时序区分）。"""
    db._conn.execute("UPDATE sessions SET last_active_at=? WHERE id=?", (ts, session_id))
    db._conn.commit()


def _inbound(msg_id, text):
    return {"message_id": msg_id, "seq": msg_id, "from_user_id": USER,
            "message_type": 1, "context_token": "CTX",
            "item_list": [{"type": 1, "text_item": {"text": text}}]}


def _old_db(path):
    """手工建 M1 旧库：sessions 表 UNIQUE(wechat_user, cwd) + 两行数据 + 一条任务
    （任务行验证迁移后 tasks.session_id 外键不漂移）。"""
    db = Database(path)
    db._conn.executescript("""
    CREATE TABLE sessions (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      wechat_user TEXT NOT NULL,
      cwd TEXT NOT NULL,
      claude_uuid TEXT NOT NULL,
      policy TEXT NOT NULL DEFAULT 'auto',
      created_at INTEGER NOT NULL,
      last_active_at INTEGER NOT NULL,
      UNIQUE(wechat_user, cwd)
    );
    CREATE TABLE tasks (
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
    """)
    now = int(time.time())
    db._conn.execute(
        "INSERT INTO sessions(wechat_user, cwd, claude_uuid, policy, created_at, last_active_at) "
        "VALUES(?,?,?,?,?,?)", (USER, "/repo", "uuid-repo", "strict", now - 200, now - 200))
    db._conn.execute(
        "INSERT INTO sessions(wechat_user, cwd, claude_uuid, policy, created_at, last_active_at) "
        "VALUES(?,?,?,?,?,?)", (USER, "/other", "uuid-other", "auto", now - 100, now - 100))
    db._conn.execute(
        "INSERT INTO tasks(session_id, prompt, created_at, updated_at) "
        "VALUES(1, '旧任务', ?, ?)", (now, now))
    db._conn.commit()
    return db


# ---------------- 1. schema 迁移 ----------------

def test_schema_migration_preserves_data_and_relaxes_unique(tmp_path):
    db = _old_db(tmp_path / "old.db")
    db.ensure_schema()
    rows = db._conn.execute("SELECT * FROM sessions ORDER BY id").fetchall()
    assert [r["claude_uuid"] for r in rows] == ["uuid-repo", "uuid-other"]
    assert rows[0]["cwd"] == "/repo" and rows[0]["policy"] == "strict"   # 全字段保留
    # 老约束已放宽：同 (user, cwd) 可插第二行
    db._conn.execute(
        "INSERT INTO sessions(wechat_user, cwd, claude_uuid, policy, created_at, last_active_at) "
        "VALUES(?,?,?,?,?,?)", (USER, "/repo", "uuid-repo-2", "auto", 1, 1))
    db._conn.commit()
    assert db._conn.execute("SELECT COUNT(*) c FROM sessions").fetchone()["c"] == 3
    # 幂等：二次 ensure_schema 不重复迁移、数据不动
    db.ensure_schema()
    assert db._conn.execute("SELECT COUNT(*) c FROM sessions").fetchone()["c"] == 3
    # 外键完好：任务仍指向既有行，foreign_key_check 无违例
    assert db.get_task(1) is not None and db.get_task(1).session_id == 1
    assert db._conn.execute("PRAGMA foreign_key_check").fetchall() == []
    sql = db._conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='sessions'"
    ).fetchone()["sql"]
    assert "UNIQUE(wechat_user, cwd, claude_uuid)" in sql


# ---------------- 2. db 层：话题指针与最新话题 ----------------

def test_get_or_create_session_returns_latest_topic(db):
    s1 = db.get_or_create_session(USER, "/repo")
    s2 = db.create_topic(USER, "/repo")
    _pin(db, s1.id, 100)
    _pin(db, s2.id, 200)
    assert db.get_or_create_session(USER, "/repo").id == s2.id     # 多行时返回最新
    assert db.latest_topic_in(USER, "/repo").id == s2.id
    assert db.latest_topic_in(USER, "/nope") is None
    _pin(db, s1.id, 150)
    _pin(db, s2.id, 150)
    assert db.get_or_create_session(USER, "/repo").id == s2.id     # 同刻 id DESC 决胜


def test_get_active_binding_pointer_touch_and_fallback(db):
    s1 = db.get_or_create_session(USER, "/repo")
    db.set_active_session(USER, s1.id)
    _pin(db, s1.id, 100)
    assert db.get_active_binding(USER, "/dft", touch=False).id == s1.id
    assert db.get_session(s1.id).last_active_at == 100             # touch=False 不动活跃时间
    b = db.get_active_binding(USER, "/dft")                        # 默认 touch
    assert b.id == s1.id
    assert db.get_session(s1.id).last_active_at > 100              # 已 touch 到当前

    # 失效指针 → 经旧 cwd 指针回退 + 回写新指针
    db.set_active_cwd(USER, "/repo")
    db.set_active_session(USER, 9999)
    b2 = db.get_active_binding(USER, "/dft", touch=False)
    assert b2.cwd == "/repo"
    assert db.get_state(f"active_session:{USER}") == str(b2.id)

    # 无任何指针 → default_cwd 建新行并回写
    u2 = "u2@im.wechat"
    b3 = db.get_active_binding(u2, "/dft2", touch=False)
    assert b3.cwd == "/dft2"
    assert db.get_state(f"active_session:{u2}") == str(b3.id)

    # 指向他人的行（脏数据）不算有效指针
    db.set_active_session(u2, s1.id)
    b4 = db.get_active_binding(u2, "/dft3", touch=False)
    assert b4.cwd == "/dft3" and b4.id != s1.id


# ---------------- 3. /new 开新话题 ----------------

async def test_new_opens_topic_in_same_dir(db):
    s1 = db.get_or_create_session(USER, "/repo")
    db.set_active_session(USER, s1.id)
    db.set_policy(s1.id, "strict")
    reply = await execute_bridge(db, FakePool(), _route("new"), USER, FakeCfg())
    assert "新话题" in reply and "/repo" in reply and "从零" in reply
    rows = db._conn.execute(
        "SELECT * FROM sessions WHERE cwd='/repo' ORDER BY id").fetchall()
    assert len(rows) == 2                                          # 同目录两行
    assert rows[0]["claude_uuid"] != rows[1]["claude_uuid"]        # 各自独立会话
    assert db.get_state(f"active_session:{USER}") == str(rows[1]["id"])  # 指针切新行
    assert db.get_session(rows[1]["id"]).policy == "auto"          # 新话题档位重置
    assert db.get_session(s1.id).policy == "strict"                # 原话题不受影响


def test_new_routed_as_bridge():
    from gateway.router import route
    assert route("/new", set()).kind == "bridge"


# ---------------- 4. /sessions 两级渲染 ----------------

async def test_sessions_two_level_rendering(db):
    a1 = db.get_or_create_session(USER, "/repo")
    a2 = db.create_topic(USER, "/repo")
    b1 = db.get_or_create_session(USER, "/stocks")
    db.create_task(None, a1.id, "修复登录bug")
    db.create_task(None, a2.id, "用500字总结架构", kind="bg")
    db.create_task(None, b1.id, "分析持仓")
    _pin(db, a2.id, 300)          # 全局序：#1=a2（当前）、#2=b1、#3=a1
    _pin(db, b1.id, 200)
    _pin(db, a1.id, 100)
    db.set_active_session(USER, a2.id)
    reply = await execute_bridge(db, FakePool(), _route("sessions"), USER, FakeCfg())
    lines = reply.splitlines()
    assert reply.index("📂 /repo") < reply.index("📂 /stocks")     # 组按最新活跃排序
    assert "▶ #1" in reply and "[bg] 用500字总结架构" in reply     # 当前话题 ▶ + 摘要
    assert "#3" in reply and "修复登录bug" in reply                # 同组另一话题全局序号
    assert "#2" in reply and "分析持仓" in reply                   # 他组话题
    assert "/new" in reply                                          # 尾部提示
    marked = [ln for ln in lines if "▶" in ln]
    assert len(marked) == 1 and "用500字总结架构" in marked[0]     # ▶ 恰一个


# ---------------- 5. /cd 三形态 ----------------

async def test_cd_index_switches_topic_policy_independent(db):
    s1 = db.get_or_create_session(USER, "/repo")
    s2 = db.create_topic(USER, "/repo")
    _pin(db, s1.id, 100)          # 全局序：#1=s2、#2=s1
    _pin(db, s2.id, 200)
    db.set_policy(s2.id, "strict")
    reply = await execute_bridge(db, FakePool(), _route("cd", "#2"), USER, FakeCfg())
    assert "已切换" in reply and "/repo" in reply
    assert db.get_state(f"active_session:{USER}") == str(s1.id)    # 指针变
    assert db.get_session(s1.id).policy == "auto"                  # 档位各话题独立
    assert db.get_session(s2.id).policy == "strict"
    await execute_bridge(db, FakePool(), _route("cd", "#1"), USER, FakeCfg())
    assert db.get_state(f"active_session:{USER}") == str(s2.id)


async def test_cd_path_points_to_latest_topic(db, tmp_path):
    d = tmp_path / "proj"
    d.mkdir()
    s1 = db.get_or_create_session(USER, str(d))
    s2 = db.create_topic(USER, str(d))
    _pin(db, s1.id, 100)
    _pin(db, s2.id, 200)
    db.set_active_session(USER, s1.id)                             # 故意指旧话题
    reply = await execute_bridge(db, FakePool(), _route("cd", str(d)),
                                 USER, FakeCfg())
    assert "已切换" in reply and "最新话题" in reply
    assert db.get_state(f"active_session:{USER}") == str(s2.id)    # 指向该目录最新
    # 目录无话题 → 自动建
    d2 = tmp_path / "fresh"
    d2.mkdir()
    reply2 = await execute_bridge(db, FakePool(), _route("cd", str(d2)),
                                  USER, FakeCfg())
    assert "从零开始" in reply2
    n = db._conn.execute("SELECT COUNT(*) c FROM sessions WHERE cwd=?",
                         (str(d2),)).fetchone()["c"]
    assert n == 1


async def test_cd_no_args_shows_current_topic_and_hint(db):
    s1 = db.get_or_create_session(USER, "/repo")
    db.set_active_session(USER, s1.id)
    db.create_task(None, s1.id, "修复登录bug")
    reply = await execute_bridge(db, FakePool(), _route("cd", ""), USER, FakeCfg())
    assert "/repo" in reply and "修复登录bug" in reply
    assert "/sessions" in reply and "/cd #n" in reply and "/new" in reply
    assert "▶" in reply                                             # 当前话题标记


# ---------------- 6. chat 路由走话题指针 ----------------

async def test_chat_task_attaches_to_pointer_topic(db):
    s1 = db.get_or_create_session(USER, "/repo")
    s2 = db.create_topic(USER, "/repo")                             # 指针切到 s2
    await handle_inbound(db, InboundCfg(), None, None, _inbound(1, "你好"))
    row = db._conn.execute("SELECT * FROM tasks").fetchone()
    assert row["prompt"] == "你好"
    assert row["session_id"] == s2.id                               # 挂到指针话题而非 s1
    assert db.get_state(f"active_session:{USER}") == str(s2.id)


# ---------------- 7. 老库兼容（无话题指针） ----------------

async def test_legacy_no_pointer_first_message_writes_back(db):
    s1 = db.get_or_create_session(USER, "/repo")
    db.set_active_cwd(USER, "/repo")                                # 老库只有 cwd 指针
    assert db.get_state(f"active_session:{USER}") is None
    await handle_inbound(db, InboundCfg(), None, None, _inbound(1, "hi"))
    row = db._conn.execute("SELECT * FROM tasks").fetchone()
    assert row["session_id"] == s1.id                               # 落到既有话题（不另建）
    assert db.get_state(f"active_session:{USER}") == str(s1.id)     # 指针已回写
