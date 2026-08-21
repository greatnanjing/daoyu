"""桥命令执行器与 /help 生成测试。"""
import json

from gateway.bridge import build_help, execute_bridge, execute_ilink_op
from gateway.router import Route


class FakeCfg:
    def __init__(self):
        self.reconnect = {"session_duration_s": 86400}
        self.default_cwd = "/repo"
        self.throttle = {"page_char_limit": 2000}


class FakePool:
    """WorkerPool 最小替身。running_session_ids 为 brief 夹具遗漏、
    真实 WorkerPool 公开接口（execute_bridge /status 分支依赖），此处补齐。"""

    def __init__(self, tasks):
        self._tasks = tasks

    def snapshot(self):
        return self._tasks

    def running_session_ids(self):
        return set()

    async def cancel(self, task_id):
        return f"已取消任务 #{task_id}。"


def _route(cmd, args="", kind="bridge"):
    return Route(kind=kind, command=cmd, args=args, detail={})


async def test_cd_switches_session(db, tmp_path):
    # brief 原用 "/new" 字面量；/cd 有 os.path.isdir 校验，Windows 测试环境下
    # "/new" 解析为当前盘根目录不存在 → 改用 tmp_path 真实目录，断言语义不变。
    new = tmp_path / "new"
    new.mkdir()
    db.get_or_create_session("u@im.wechat", "/old")
    db.set_active_cwd("u@im.wechat", "/old")
    reply = await execute_bridge(db, FakePool([]), _route("cd", str(new)),
                                 "u@im.wechat", FakeCfg())
    assert str(new) in reply
    assert db.get_active_cwd("u@im.wechat", "/d") == str(new)
    assert db.get_or_create_session("u@im.wechat", str(new)).id != \
           db.get_or_create_session("u@im.wechat", "/old").id


async def test_cd_rejects_missing_dir(db):
    db.get_or_create_session("u@im.wechat", "/repo")
    reply = await execute_bridge(db, FakePool([]), _route("cd", "/no/such/dir"),
                                 "u@im.wechat", FakeCfg())
    assert "目录不存在" in reply
    assert db.get_active_cwd("u@im.wechat", "/d") == "/d"  # 未切换


async def test_policy_switch(db):
    s = db.get_or_create_session("u@im.wechat", "/repo")
    db.set_active_cwd("u@im.wechat", "/repo")
    reply = await execute_bridge(db, FakePool([]), _route("policy", "strict"),
                                 "u@im.wechat", FakeCfg())
    assert "strict" in reply
    assert db.get_session(s.id).policy == "strict"


async def test_policy_invalid(db):
    db.get_or_create_session("u@im.wechat", "/repo")
    reply = await execute_bridge(db, FakePool([]), _route("policy", "yolo"),
                                 "u@im.wechat", FakeCfg())
    assert "auto/strict/bypass/plan" in reply


async def test_tasks_listing(db):
    s = db.get_or_create_session("u@im.wechat", "/repo")
    db.create_task(None, s.id, "/review", kind="command")
    reply = await execute_bridge(db, FakePool(db.active_tasks()), _route("tasks"),
                                 "u@im.wechat", FakeCfg())
    assert "#1" in reply and "review" in reply


async def test_status(db):
    # 真造一个 pending task（此前版本插 outbox 行——对 queue_depth 显示毫无影响，
    # "排队" 字样恒在回复模板里，断言恒真）
    s = db.get_or_create_session("u@im.wechat", "/repo")
    db.create_task(None, s.id, "/review", kind="command")
    reply = await execute_bridge(db, FakePool([]), _route("status"),
                                 "u@im.wechat", FakeCfg())
    assert "队列：1 排队" in reply and "死信：0" in reply


async def test_cancel(db):
    reply = await execute_bridge(db, FakePool([]), _route("cancel", "3"),
                                 "u@im.wechat", FakeCfg())
    assert "3" in reply


async def test_cancel_without_args_cancels_latest_running(db):
    """PRD FR-2: /cancel 替代 Ctrl+C —— 无参数应取消当前会话最新运行中任务。"""
    s = db.get_or_create_session("u@im.wechat", "/repo")
    db.set_active_cwd("u@im.wechat", "/repo")
    db.create_task(None, s.id, "long-job")          # id=1
    db.claim_next_pending({s.id})                    # → running

    class PoolWithCancel(FakePool):
        def __init__(self, tasks):
            super().__init__(tasks)
            self.cancelled = None

        async def cancel(self, task_id):
            self.cancelled = task_id
            return f"已取消任务 #{task_id}。"

    pool = PoolWithCancel(db.active_tasks())
    reply = await execute_bridge(db, pool, _route("cancel", ""), "u@im.wechat", FakeCfg())
    assert pool.cancelled == 1                       # 取消了 running 的 #1
    assert "1" in reply


async def test_cancel_without_args_no_running(db):
    """无参数且当前会话无 running 任务 → 用法提示。"""
    s = db.get_or_create_session("u@im.wechat", "/repo")
    db.set_active_cwd("u@im.wechat", "/repo")
    db.create_task(None, s.id, "queued")             # pending 未领取
    reply = await execute_bridge(db, FakePool(db.active_tasks()),
                                 _route("cancel", ""), "u@im.wechat", FakeCfg())
    assert "用法" in reply


async def test_cancel_non_numeric(db):
    reply = await execute_bridge(db, FakePool([]), _route("cancel", "abc"),
                                 "u@im.wechat", FakeCfg())
    assert "用法" in reply


async def test_sessions_lists_multiple_sessions(db):
    """多话题两级列表：目录分组 + 组内全局序号 + 相对时间 + 最后任务摘要 + 当前 ▶。"""
    s1 = db.get_or_create_session("u@im.wechat", "/repo")
    s2 = db.get_or_create_session("u@im.wechat", "/other")
    db.create_task(None, s1.id, "修复登录 bug")
    db.create_task(None, s2.id, "部署到测试环境", kind="bg")
    db.set_active_session("u@im.wechat", s1.id)
    # 直接钉死 last_active_at（同秒创建无法靠时序区分）：/repo 组最新在前
    db._conn.execute("UPDATE sessions SET last_active_at=? WHERE id=?", (200, s1.id))
    db._conn.execute("UPDATE sessions SET last_active_at=? WHERE id=?", (100, s2.id))
    db._conn.commit()
    reply = await execute_bridge(db, FakePool([]), _route("sessions"),
                                 "u@im.wechat", FakeCfg())
    assert "/repo" in reply and "/other" in reply
    assert "修复登录 bug" in reply                       # 普通任务摘要
    assert "[bg] 部署到测试环境" in reply                # bg 任务带前缀
    assert reply.count("#1") == 1 and reply.count("#2") == 1  # 全局序号
    assert reply.index("/repo") < reply.index("/other")  # 组按最新活跃排序
    assert "▶" in reply
    marked = [ln for ln in reply.splitlines() if "▶" in ln]
    assert len(marked) == 1 and "修复登录 bug" in marked[0]  # 当前话题被标记且仅一个


async def test_cd_by_index_switches(db):
    """#n 按全局序号切话题（last_active_at DESC，#2 = 次新）。"""
    s1 = db.get_or_create_session("u@im.wechat", "/repo")
    s2 = db.get_or_create_session("u@im.wechat", "/other")
    # 直接钉死 last_active_at（同秒创建无法靠时序区分）：s2 最新排 #1，s1 排 #2
    db._conn.execute("UPDATE sessions SET last_active_at=? WHERE id=?", (200, s2.id))
    db._conn.execute("UPDATE sessions SET last_active_at=? WHERE id=?", (100, s1.id))
    db._conn.commit()
    reply = await execute_bridge(db, FakePool([]), _route("cd", "#2"),
                                 "u@im.wechat", FakeCfg())
    assert "已切换" in reply and "/repo" in reply
    assert db.get_active_cwd("u@im.wechat", "/d") == "/repo"
    # 切的是话题指针：#2 对应 s1 这一行；切回既有目录话题 = 绑回既有行（不新建）
    assert db.get_state("active_session:u@im.wechat") == str(s1.id)
    assert db.get_or_create_session("u@im.wechat", "/repo").id == s1.id


async def test_cd_index_out_of_range(db):
    db.get_or_create_session("u@im.wechat", "/repo")
    db.get_or_create_session("u@im.wechat", "/other")
    reply = await execute_bridge(db, FakePool([]), _route("cd", "#5"),
                                 "u@im.wechat", FakeCfg())
    assert "序号超出范围" in reply and "共 2 个话题" in reply
    assert db.get_active_cwd("u@im.wechat", "/d") == "/d"  # 未切换


async def test_sessions_no_task_shows_placeholder(db):
    db.get_or_create_session("u@im.wechat", "/repo")
    reply = await execute_bridge(db, FakePool([]), _route("sessions"),
                                 "u@im.wechat", FakeCfg())
    assert "（无任务）" in reply


async def test_cd_no_args_shows_sessions_hint(db):
    db.get_or_create_session("u@im.wechat", "/repo")
    reply = await execute_bridge(db, FakePool([]), _route("cd", ""),
                                 "u@im.wechat", FakeCfg())
    assert "/sessions" in reply and "/cd #n" in reply


def test_sessions_routed_as_bridge():
    from gateway.router import route
    assert route("/sessions", set()).kind == "bridge"


def test_help_merges_three_layers(db):
    db.set_state("slash_commands", json.dumps(["review", "model"]))
    text = build_help(db)
    assert "/cancel" in text          # 桥命令层
    assert "/time" in text            # iLink 运维层
    assert "/help" in text            # /help 自身也在列（Minor #8：曾缺失）
    assert "/review" in text          # headless 转发层


def test_help_includes_implemented_proxy_commands(db):
    """I2：/help 必须列出已实现的 proxy 命令（PRD：与实际能力一致）。"""
    text = build_help(db)
    assert "/permissions" in text and "deny add" in text
    assert "/mcp" in text
    assert "/config" in text
    # 未实现的 proxy 命令不列（只列当前实际可用）
    assert "/hooks" not in text and "/login" not in text


async def test_ilink_ops(db):
    cfg = FakeCfg()
    help_text = await execute_ilink_op(db, _route("help", kind="ilink"),
                                       "u@im.wechat", cfg, None)
    assert "/cancel" in help_text
    time_text = await execute_ilink_op(db, _route("time", kind="ilink"),
                                       "u@im.wechat", cfg, None)
    assert "剩余" in time_text
    reconn = await execute_ilink_op(db, _route("重新连接", kind="ilink"),
                                    "u@im.wechat", cfg, None)
    assert "确认" in reconn
    assert db.get_state("reconnect_confirm") == "u@im.wechat"


# ---- /adopt：收养终端创建的外部会话 ----

class AdoptCfg(FakeCfg):
    """FakeCfg 补 repo_root（/adopt 扫描 data/claude-home/projects 用）。"""

    def __init__(self, repo_root):
        super().__init__()
        self.repo_root = repo_root


def _write_transcript(tmp_path, uid: str, cwd: str, prompt: str, mtime=None):
    import os as _os
    import time as _time
    d = tmp_path / "data/claude-home/projects/-some-slug"
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{uid}.jsonl"
    lines = [
        '{"type":"system","subtype":"init","cwd":"%s","session_id":"%s"}' % (cwd, uid),
        '{"type":"user","message":{"role":"user","content":"%s"},"cwd":"%s"}' % (prompt, cwd),
        '{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"ok"}]}}',
    ]
    f.write_text("\n".join(lines), encoding="utf-8")
    if mtime is not None:
        _os.utime(f, (mtime, mtime))
    return f


async def test_adopt_newest_external(db, tmp_path):
    cfg = AdoptCfg(tmp_path)
    _write_transcript(tmp_path, "11111111-aaaa-bbbb-cccc-000000000001",
                      "/home/u/proj", "帮我看看这个报错", mtime=1000)
    _write_transcript(tmp_path, "11111111-aaaa-bbbb-cccc-000000000002",
                      "/home/u/other", "更早的一个话题", mtime=500)
    db.get_or_create_session("u@im.wechat", "/repo")
    reply = await execute_bridge(db, FakePool([]), _route("adopt", ""),
                                 "u@im.wechat", cfg)
    assert "帮我看看这个报错" in reply and "/home/u/proj" in reply
    # 当前话题已切到收养行；inited 状态已置（runner 将走 --resume）
    s = db.get_active_binding("u@im.wechat", "/repo", touch=False)
    assert s.claude_uuid == "11111111-aaaa-bbbb-cccc-000000000001"
    assert s.cwd == "/home/u/proj"
    assert db.get_state("claude_session_inited:11111111-aaaa-bbbb-cccc-000000000001") == "1"
    assert db.get_active_cwd("u@im.wechat", "/d") == "/home/u/proj"


async def test_adopt_by_unique_prefix(db, tmp_path):
    cfg = AdoptCfg(tmp_path)
    _write_transcript(tmp_path, "99999999-aaaa-bbbb-cccc-000000000001",
                      "/home/u/proj", "最新话题", mtime=2000)
    _write_transcript(tmp_path, "abcdef12-3333-4444-5555-666666666666",
                      "/home/u/old", "指定要这个", mtime=100)
    reply = await execute_bridge(db, FakePool([]), _route("adopt", "ABCDEF12"),
                                 "u@im.wechat", cfg)
    assert "指定要这个" in reply
    assert db.get_active_binding("u@im.wechat", "/repo", touch=False).claude_uuid \
        == "abcdef12-3333-4444-5555-666666666666"


async def test_adopt_prefix_ambiguous(db, tmp_path):
    cfg = AdoptCfg(tmp_path)
    _write_transcript(tmp_path, "abcdef12-0000-0000-0000-000000000001",
                      "/a", "第一个", mtime=100)
    _write_transcript(tmp_path, "abcdef12-0000-0000-0000-000000000002",
                      "/b", "第二个", mtime=200)
    reply = await execute_bridge(db, FakePool([]), _route("adopt", "abcdef12"),
                                 "u@im.wechat", cfg)
    assert "匹配到 2 个" in reply


async def test_adopt_already_managed(db, tmp_path):
    cfg = AdoptCfg(tmp_path)
    s = db.get_or_create_session("u@im.wechat", "/repo")
    reply = await execute_bridge(db, FakePool([]), _route("adopt", s.claude_uuid),
                                 "u@im.wechat", cfg)
    assert "已是刀鱼话题" in reply


async def test_adopt_no_candidates(db, tmp_path):
    cfg = AdoptCfg(tmp_path)
    db.get_or_create_session("u@im.wechat", "/repo")
    reply = await execute_bridge(db, FakePool([]), _route("adopt", ""),
                                 "u@im.wechat", cfg)
    assert "CLAUDE_CONFIG_DIR" in reply


async def test_adopt_short_prefix_rejected(db, tmp_path):
    # <8 位前缀不启用匹配（误命中面太大），走未找到路径
    cfg = AdoptCfg(tmp_path)
    _write_transcript(tmp_path, "abcdef12-0000-0000-0000-000000000001",
                      "/a", "第一个", mtime=100)
    reply = await execute_bridge(db, FakePool([]), _route("adopt", "abcd"),
                                 "u@im.wechat", cfg)
    assert "未找到" in reply


async def test_sessions_shows_uuid_hint(db, tmp_path):
    db.get_or_create_session("u@im.wechat", "/repo")
    reply = await execute_bridge(db, FakePool([]), _route("sessions", ""),
                                 "u@im.wechat", FakeCfg())
    assert "·" in reply and "/adopt" in reply


# ---------------- M5C3：/alias 自定义快捷命令 ----------------

def _load_aliases(db, user="u@im.wechat"):
    return json.loads(db.get_state(f"alias:{user}") or "{}")


async def test_alias_add_and_list(db):
    reply = await execute_bridge(db, FakePool([]), _route("alias"),
                                 "u@im.wechat", FakeCfg())
    assert "暂无自定义别名" in reply and "/t=/tasks" in reply
    reply = await execute_bridge(
        db, FakePool([]), _route("alias", "add go 跑全量测试并总结"),
        "u@im.wechat", FakeCfg())
    assert "已定义 /go" in reply
    assert _load_aliases(db) == {"go": "跑全量测试并总结"}
    reply = await execute_bridge(db, FakePool([]), _route("alias", "list"),
                                 "u@im.wechat", FakeCfg())
    assert "/go → 跑全量测试并总结" in reply
    assert any(r["kind"] == "alias_add"
               for r in db._conn.execute("SELECT kind FROM audit_log"))


async def test_alias_del(db):
    await execute_bridge(db, FakePool([]), _route("alias", "add go x"),
                         "u@im.wechat", FakeCfg())
    reply = await execute_bridge(db, FakePool([]), _route("alias", "del go"),
                                 "u@im.wechat", FakeCfg())
    assert "已删除别名 /go" in reply
    assert _load_aliases(db) == {}
    reply = await execute_bridge(db, FakePool([]), _route("alias", "del go"),
                                 "u@im.wechat", FakeCfg())
    assert "没有别名 /go" in reply


async def test_alias_add_validation(db):
    # 系统命令撞名拒绝（桥/运维/代理/alias 自身）
    for bad in ("tasks", "time", "config", "alias"):
        reply = await execute_bridge(
            db, FakePool([]), _route("alias", f"add {bad} x"),
            "u@im.wechat", FakeCfg())
        assert "系统命令" in reply, bad
    # 名超长
    reply = await execute_bridge(
        db, FakePool([]), _route("alias", f"add {'n' * 17} x"),
        "u@im.wechat", FakeCfg())
    assert "1~16" in reply
    # 值超长
    reply = await execute_bridge(
        db, FakePool([]), _route("alias", f"add ok {'v' * 2001}"),
        "u@im.wechat", FakeCfg())
    assert "1~2000" in reply
    # 用法缺参
    reply = await execute_bridge(db, FakePool([]), _route("alias", "add onlyname"),
                                 "u@im.wechat", FakeCfg())
    assert "用法" in reply


async def test_alias_can_override_builtin_and_warns_slash(db):
    # 内置别名可覆盖（t/s/c/cs 不在禁止集）——附注提示
    reply = await execute_bridge(
        db, FakePool([]), _route("alias", "add t /status"), "u@im.wechat",
        FakeCfg())
    assert "已定义 /t" in reply and "覆盖内置" in reply
    # 撞 Claude 动态命令：允许但提示
    db.set_state("slash_commands", json.dumps(["review"]))
    reply = await execute_bridge(
        db, FakePool([]), _route("alias", "add review 看代码"), "u@im.wechat",
        FakeCfg())
    assert "已定义 /review" in reply and "重名" in reply


async def test_alias_count_limit(db):
    for i in range(50):
        await execute_bridge(db, FakePool([]), _route("alias", f"add a{i} v"),
                             "u@im.wechat", FakeCfg())
    reply = await execute_bridge(
        db, FakePool([]), _route("alias", "add overflow v"), "u@im.wechat",
        FakeCfg())
    assert "上限" in reply
