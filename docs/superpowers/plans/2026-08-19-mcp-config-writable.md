# M3 余项 A：/mcp 启停与 /config 写入 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** /mcp 支持 on/off 启停（mcp.json 平台无关化 + 顶层 disabled 标记 + runner 合并层平台展开与过滤），/config 支持七键白名单 set 写入；顺带清偿「生产 Linux MCP 从未跑通」欠账。

**Architecture:** 静态 `claude/mcp.json` 改平台无关形态（command 直写 npx/uvx）+ 顶层 `"disabled": []` 启停标记；runner 写临时 mcp config 时（既有 `_write_daoyu_mcp_config` 合并层）过滤 disabled、按平台展开（Windows 白名单命令包 `cmd /c`）。proxy 层 `/mcp on/off` 与 `/config set` 走原子写（抽公共 `_atomic_write_json`）+ audit。

**Tech Stack:** Python 3.11 asyncio（gateway/worker 既有栈）、pytest + pytest-asyncio、aioresponses（既有）。

**Spec:** [docs/superpowers/specs/2026-08-19-mcp-config-writable-design.md](../specs/2026-08-19-mcp-config-writable-design.md)

## Global Constraints

- mcp.json 条目**平台无关**：command 直写 `npx` / `uvx`（无 `cmd /c`）；启停状态存**顶层 `"disabled": []`**（与 mcpServers 平级），缺省视为空。
- 平台展开**白名单 `{"npx", "uvx"}`**，仅 Windows（`sys.platform == "win32"`）包装 `cmd /c <command> <args...>`；其他 command（含 `sys.executable`）两平台直传。展开函数是纯函数、平台由参数传入（可测）。
- disabled 条目在 runner 合并层过滤（临时 mcp config 不含 → claude 视为不存在）；disabled 数组里的残留名（不在 mcpServers）静默忽略，不报错不清理。
- 系统条目（daoyu 审批/发图，由 runner 注入）**不受 disabled 过滤**（注入在静态清单过滤之后）。
- `/config set` 键白名单与范围（类型, 约束）：
  - `throttle.min_send_interval_s`（float, > 0）
  - `throttle.progress_window_s`（float, > 0）
  - `throttle.page_char_limit`（int, ≥ 200）
  - `throttle.daily_send_limit`（int, ≥ 1）
  - `budget.max_turns`（int, ≥ 1）
  - `budget.max_usd`（float, > 0）
  - `worker.concurrency`（int, 1 ~ 10）
  - 白名单外的键（whitelist / default_cwd / claude_bin / reconnect.* 等）set 时拒绝并提示改文件；whitelist 从微信改 = 放别人进服务器，安全不开放。
- 写回一律**原子写**（同目录 mkstemp + `os.replace`，失败清理临时文件——照 proxy.py `_save_settings` 现有模式）；config.json 写回必须**读原文改键整体写回**（保留 whitelist 等其他键原样）。
- audit_log 记 `config_change`（照 /permissions 先例，detail 形如 `mcp off web-reader` / `config set throttle.page_char_limit=1500`）。
- 生效口径（回执文案如实）：/mcp on/off → 「下一任务生效」；/config set → 「重启生效（systemctl restart daoyu）」。
- 序号 1-based 与列表显示一致（照 /permissions del 先例）；目标解析**名字精确匹配优先**，否则按序号。
- 基线：`python -m pytest` 253 个测试全绿，每个任务完成后不得有回归。
- Windows 开发机 Git Bash 下 venv 解释器为 `.venv/Scripts/python`。

---

### Task 1: cli_builder 平台展开函数 + mcp.json 平台无关化

**Files:**
- Modify: `worker/cli_builder.py`（文件尾部追加常量与函数）
- Modify: `claude/mcp.json`（改平台无关形态）
- Test: `tests/test_cli_builder.py`（追加）

**Interfaces:**
- Consumes: 无（纯新增）
- Produces: `expand_platform(servers: dict, windows: bool) -> dict`——Task 2 的 `_write_daoyu_mcp_config` 调用；`_WINDOWS_WRAP: set[str]` 常量（文档用，Task 2 不直接用）

- [ ] **Step 1: Write the failing test**

在 `tests/test_cli_builder.py` 追加（该文件现有测试直接调用 `build_argv`，本任务新增独立测试组）：

```python
# ---- expand_platform：静态 mcp.json 平台无关 → 实际拉起形态 ----

def _svc(command="npx", args=None):
    return {"type": "stdio", "command": command, "args": args or [], "env": {}}


def test_expand_platform_windows_wraps_npx():
    from worker.cli_builder import expand_platform
    servers = {"context7": _svc("npx", ["-y", "@upstash/context7-mcp"]),
               "web-reader": _svc("uvx", ["--with", "mcp~=1.0", "mcp-server-fetch"])}
    out = expand_platform(servers, windows=True)
    assert out["context7"]["command"] == "cmd"
    assert out["context7"]["args"] == ["/c", "npx", "-y", "@upstash/context7-mcp"]
    assert out["web-reader"]["command"] == "cmd"
    assert out["web-reader"]["args"][0] == "/c" and out["web-reader"]["args"][1] == "uvx"


def test_expand_platform_linux_passes_through():
    from worker.cli_builder import expand_platform
    servers = {"context7": _svc("npx", ["x"])}
    out = expand_platform(servers, windows=False)
    assert out["context7"]["command"] == "npx"
    assert out["context7"]["args"] == ["x"]


def test_expand_platform_non_whitelist_command_untouched():
    # sys.executable / 自定义二进制等白名单外命令：两平台都不包装（Windows 也不）
    from worker.cli_builder import expand_platform
    servers = {"daoyu": _svc("C:/venv/Scripts/python.exe", ["worker/approval_mcp.py"])}
    for win in (True, False):
        out = expand_platform(servers, windows=win)
        assert out["daoyu"]["command"] == "C:/venv/Scripts/python.exe", win
        assert out["daoyu"]["args"] == ["worker/approval_mcp.py"], win


def test_expand_platform_does_not_mutate_input():
    # 原始 dict 不被就地修改（调用方是读文件所得，但防御拷贝语义要显式）
    from worker.cli_builder import expand_platform
    servers = {"context7": _svc("npx", ["x"])}
    expand_platform(servers, windows=True)
    assert servers["context7"]["command"] == "npx"
    assert servers["context7"]["args"] == ["x"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_cli_builder.py -k expand_platform -v`
Expected: FAIL with `ImportError` / `cannot import name 'expand_platform'`

- [ ] **Step 3: Write minimal implementation**

`worker/cli_builder.py` 文件末尾追加：

```python
# 静态 mcp.json 平台无关命令的 Windows 包装白名单：npm 系命令在 Windows 是
# .cmd shim，asyncio create_subprocess_exec 直启会 FileNotFoundError → 包
# cmd /c。Linux 直传。白名单外（sys.executable 等绝对路径）两平台都直传。
_WINDOWS_WRAP = {"npx", "uvx"}


def expand_platform(servers: dict, windows: bool) -> dict:
    """静态 mcpServers → 实际拉起形态（纯函数，平台由参数传入可测）。
    不就地修改入参（浅拷贝条目）；非 dict 条目原样透传（防御坏文件）。"""
    if not windows:
        return servers
    out = {}
    for name, svc in servers.items():
        if isinstance(svc, dict) and svc.get("command") in _WINDOWS_WRAP:
            svc = {**svc, "command": "cmd",
                   "args": ["/c", svc["command"], *svc.get("args", [])]}
        out[name] = svc
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python -m pytest tests/test_cli_builder.py -v`
Expected: 全 PASS（既有测试零回归）

- [ ] **Step 5: mcp.json 改平台无关形态**

`claude/mcp.json` 整体替换为：

```json
{
  "mcpServers": {
    "chrome-devtools": {
      "type": "stdio",
      "command": "npx",
      "args": ["chrome-devtools-mcp@latest"],
      "env": {}
    },
    "context7": {
      "type": "stdio",
      "command": "npx",
      "args": ["-y", "@upstash/context7-mcp"],
      "env": {}
    },
    "web-reader": {
      "type": "stdio",
      "command": "uvx",
      "args": ["--with", "mcp~=1.0", "mcp-server-fetch"],
      "env": {}
    }
  },
  "disabled": []
}
```

（注意：本步骤只改文件，runner 展开在 Task 2 接线——本任务结束时本机 Windows 下真实任务暂不能用 MCP，Task 2 完成即恢复。测试不受影响：测试全部用 tmp_path 自建 mcp.json，唯一例外是下述 schema 测试，本步骤一并更新。）

- [ ] **Step 5b: 更新 tests/test_mcp_config.py（直测静态 mcp.json 的 schema 测试）**

该文件 docstring 现写「清单的 command 是 Windows 形态（cmd /c npx …）；Linux 部署时改为 npx/uvx 直连」——形态改后过时。docstring 替换为：

```python
"""claude/mcp.json 静态 MCP 清单 schema 测试：三个 server 键存在、stdio 传输、
command/args 非空。清单为平台无关形态（command 直写 npx/uvx），实际拉起形态
由 runner 合并层按平台展开（Windows 包 cmd /c，见 worker/cli_builder.py）。"""
```

并在文件末尾追加 disabled 键的 schema 断言：

```python
def test_mcp_disabled_key_is_optional_list_of_names():
    raw = json.loads(MCP_JSON.read_text(encoding="utf-8"))
    disabled = raw.get("disabled", [])
    assert isinstance(disabled, list), "disabled 应为 list（缺省视为空）"
    assert all(isinstance(d, str) and d for d in disabled)
```

Run: `.venv/Scripts/python -m pytest tests/test_mcp_config.py -v`
Expected: 全 PASS（既有 schema 断言对平台无关形态天然兼容——command "npx" 非空字符串）

- [ ] **Step 6: Commit**

```bash
git add worker/cli_builder.py tests/test_cli_builder.py claude/mcp.json tests/test_mcp_config.py
git commit -m "feat(m3-a): cli_builder 平台展开函数 + mcp.json 平台无关化（cmd /c 剥离）"
```

---

### Task 2: runner 合并层过滤 disabled + 平台展开接线

**Files:**
- Modify: `worker/runner.py`（`_write_daoyu_mcp_config`，现约 336-374 行）
- Test: `tests/test_runner.py`（追加）

**Interfaces:**
- Consumes: `worker.cli_builder.expand_platform(servers: dict, windows: bool) -> dict`（Task 1 产出）
- Produces: `_write_daoyu_mcp_config` 行为变更——读静态清单后过滤 `disabled`、按平台展开；签名不变（Task 3/后续不依赖新签名）

- [ ] **Step 1: Write the failing test**

在 `tests/test_runner.py` 追加（fixture 与断言模式照 `test_daoyu_mcp_merged_all_policies`：cfg / tmp_path / monkeypatch + FAKE_CLAUDE_ARGS_LOG，fake_claude 子进程会快照 `--mcp-config` 文件内容）：

```python
async def test_mcp_disabled_filtered_and_platform_expanded(
        db, cfg, tmp_path, monkeypatch):
    """余项 A：静态 mcp.json 的 disabled 条目不进临时 mcp config；
    Windows 白名单命令（npx）在合并层包 cmd /c，Linux 直传。
    fake_claude 快照的是合并后文件内容——disabled 键本身绝不进临时文件。"""
    args_log = tmp_path / "mcp_a2.log"
    monkeypatch.setenv("FAKE_CLAUDE_ARGS_LOG", str(args_log))
    monkeypatch.setattr("worker.runner.sys.platform", "win32")
    claude_dir = cfg.repo_root / "claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    (claude_dir / "mcp.json").write_text(json.dumps({
        "mcpServers": {
            "context7": {"type": "stdio", "command": "npx",
                          "args": ["-y", "@upstash/context7-mcp"], "env": {}},
            "web-reader": {"type": "stdio", "command": "uvx",
                            "args": ["mcp-server-fetch"], "env": {}},
        },
        "disabled": ["web-reader", "ghost"],   # ghost：残留名静默忽略
    }), encoding="utf-8")
    s = db.get_or_create_session("u@im.wechat", str(cfg.repo_root))
    t = db.create_task(None, s.id, "hi", kind="chat")
    runner = TaskRunner(db, cfg, process_registry={})
    await runner.run(db.get_task(t), s)

    assert db.get_task(t).state == "done"
    log = json.loads(args_log.read_text(encoding="utf-8"))
    raw = log["mcp_config"]                      # fake_claude 快照的文件内容
    servers = raw["mcpServers"]
    assert "context7" in servers                 # 启用条目保留
    assert servers["context7"]["command"] == "cmd"          # Windows 包装
    assert servers["context7"]["args"][0] == "/c"
    assert "web-reader" not in servers           # disabled 过滤
    assert "ghost" not in servers                # 残留名忽略（本就不在清单）
    assert "disabled" not in raw                 # disabled 键不进临时文件
    assert "daoyu" in servers                    # 系统条目恒注入


async def test_mcp_disabled_absent_means_all_enabled(
        db, cfg, tmp_path, monkeypatch):
    """旧文件无 disabled 键 → 全部启用（幂等兼容）。"""
    args_log = tmp_path / "mcp_a2b.log"
    monkeypatch.setenv("FAKE_CLAUDE_ARGS_LOG", str(args_log))
    claude_dir = cfg.repo_root / "claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    (claude_dir / "mcp.json").write_text(json.dumps({"mcpServers": {
        "context7": {"type": "stdio", "command": "x", "args": []}}}),
        encoding="utf-8")
    s = db.get_or_create_session("u@im.wechat", str(cfg.repo_root))
    t = db.create_task(None, s.id, "hi", kind="chat")
    runner = TaskRunner(db, cfg, process_registry={})
    await runner.run(db.get_task(t), s)

    log = json.loads(args_log.read_text(encoding="utf-8"))
    assert "context7" in log["mcp_config"]["mcpServers"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_runner.py -k "mcp_disabled" -v`
Expected: 第一个 FAIL（`servers["context7"]["command"]` 是 `"npx"` 而非 `"cmd"`，且 `web-reader` 未被过滤）

- [ ] **Step 3: Write minimal implementation**

`worker/runner.py` 顶部 import 行改为（`expand_platform` 加入）：

```python
from worker.cli_builder import (APPROVAL_MCP_SERVER, BYPASS_DISALLOWED_TOOLS,
                                POLICY_MODE, build_argv, claude_config_dir,
                                expand_platform)
```

`_write_daoyu_mcp_config` 中，读取 static 后（现有 `static = {}` / `static = json.loads(...)` 逻辑不动），把合并段：

```python
        merged = {"mcpServers": {
            **static.get("mcpServers", {}),
```

改为：

```python
        # 余项 A：disabled 条目过滤（不进临时文件 = claude 视为不存在）；
        # disabled 为非 list（坏文件）按空处理，与 fail-open 策略一致。
        disabled = static.get("disabled")
        disabled = set(disabled) if isinstance(disabled, list) else set()
        servers = {k: v for k, v in static.get("mcpServers", {}).items()
                   if k not in disabled}
        # 平台无关条目 → 实际拉起形态（Windows 白名单命令包 cmd /c）
        servers = expand_platform(servers, sys.platform == "win32")
        merged = {"mcpServers": {
            **servers,
```

（其后 APPROVAL_MCP_SERVER 条目与临时文件写入逻辑不动——daoyu 系统条目在过滤之后注入，天然不受 disabled 管辖。）

同时更新该方法 docstring 首段为（在「四档通用临时 mcp config：」后加一句）：

```python
        """四档通用临时 mcp config：静态 mcp.json 的 mcpServers 过滤 disabled、
        按平台展开（Windows npx/uvx 包 cmd /c）后合并 daoyu server 条目
        （tools 按档传 approve,send_image 或 send_image）。daoyu server 是
```

（其余 docstring 文字保留。）

- [ ] **Step 4: Run test to verify it passes + 全量回归**

Run: `.venv/Scripts/python -m pytest tests/test_runner.py -v`
Expected: 新增 2 个 PASS，既有零回归

Run: `.venv/Scripts/python -m pytest`
Expected: 255+ passed（253 基线 + 新增 2）

- [ ] **Step 5: Commit**

```bash
git add worker/runner.py tests/test_runner.py
git commit -m "feat(m3-a): runner 合并层过滤 disabled + 按平台展开静态 mcp 清单"
```

---

### Task 3: /mcp on/off 启停（proxy 层）

**Files:**
- Modify: `gateway/proxy.py`（`_mcp` 扩展 + `_atomic_write_json` 抽公共 + `_save_settings` 改为其调用方）
- Test: `tests/test_proxy.py`（追加）

**Interfaces:**
- Consumes: 无新依赖（json/os/tempfile 既有）
- Produces: `/mcp`（无参 → 列表带状态）、`/mcp on|off <序号|名字>`；`_atomic_write_json(path: Path, data: dict) -> None`（Task 4 复用）

- [ ] **Step 1: Write the failing test**

在 `tests/test_proxy.py` 追加（fixture 模式照现有 `_write_settings`——本组加 `_write_mcp` helper）：

```python
# ---- /mcp on/off 启停 ----

def _write_mcp(root, servers, disabled=None):
    (root / "claude").mkdir(exist_ok=True)
    doc = {"mcpServers": servers}
    if disabled is not None:
        doc["disabled"] = disabled
    (root / "claude" / "mcp.json").write_text(
        json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_mcp(root):
    return json.loads(
        (root / "claude" / "mcp.json").read_text(encoding="utf-8"))


_MCP_SRV = {
    "chrome-devtools": {"type": "stdio", "command": "npx",
                         "args": ["chrome-devtools-mcp@latest"], "env": {}},
    "web-reader": {"type": "stdio", "command": "uvx",
                    "args": ["mcp-server-fetch"], "env": {}},
}


async def test_mcp_off_by_name_then_on_by_index(db, tmp_path):
    _write_mcp(tmp_path, _MCP_SRV)
    reply = await execute_proxy(db, _route("mcp", "off web-reader"), FakeCfg(tmp_path))
    assert "已停用" in reply and "下一任务生效" in reply
    doc = _read_mcp(tmp_path)
    assert doc["disabled"] == ["web-reader"]
    assert doc["mcpServers"]["web-reader"]      # 条目保留不丢
    assert len(_audit_details(db, "config_change")) == 1

    reply = await execute_proxy(db, _route("mcp", "on 2"), FakeCfg(tmp_path))
    assert "已启用" in reply
    assert _read_mcp(tmp_path)["disabled"] == []


async def test_mcp_off_by_index(db, tmp_path):
    _write_mcp(tmp_path, _MCP_SRV)
    reply = await execute_proxy(db, _route("mcp", "off 1"), FakeCfg(tmp_path))
    assert "已停用" in reply and "chrome-devtools" in reply
    assert _read_mcp(tmp_path)["disabled"] == ["chrome-devtools"]


async def test_mcp_off_duplicate_idempotent_hint(db, tmp_path):
    _write_mcp(tmp_path, _MCP_SRV, disabled=["web-reader"])
    reply = await execute_proxy(db, _route("mcp", "off web-reader"), FakeCfg(tmp_path))
    assert "已是停用" in reply
    assert _read_mcp(tmp_path)["disabled"] == ["web-reader"]   # 文件未动


async def test_mcp_on_not_disabled_hint(db, tmp_path):
    _write_mcp(tmp_path, _MCP_SRV)
    reply = await execute_proxy(db, _route("mcp", "on chrome-devtools"), FakeCfg(tmp_path))
    assert "已处于启用" in reply


async def test_mcp_off_unknown_target(db, tmp_path):
    _write_mcp(tmp_path, _MCP_SRV)
    reply = await execute_proxy(db, _route("mcp", "off ghost"), FakeCfg(tmp_path))
    assert "没有这个 server" in reply and "chrome-devtools" in reply   # 提示当前清单


async def test_mcp_off_index_out_of_range(db, tmp_path):
    _write_mcp(tmp_path, _MCP_SRV)
    reply = await execute_proxy(db, _route("mcp", "off 9"), FakeCfg(tmp_path))
    assert "越界" in reply and "共 2" in reply
    assert "disabled" not in _read_mcp(tmp_path)     # 文件未动（无 disabled 键）


async def test_mcp_off_missing_target_shows_usage(db, tmp_path):
    _write_mcp(tmp_path, _MCP_SRV)
    for args in ("off", "on", "toggle web-reader", "off 1 2"):
        reply = await execute_proxy(db, _route("mcp", args), FakeCfg(tmp_path))
        assert "用法" in reply, args


async def test_mcp_list_marks_disabled(db, tmp_path):
    _write_mcp(tmp_path, _MCP_SRV, disabled=["web-reader"])
    reply = await execute_proxy(db, _route("mcp"), FakeCfg(tmp_path))
    assert "⛔" in reply and "✅" in reply
    assert reply.index("chrome-devtools") < reply.index("web-reader")


async def test_mcp_off_atomic_no_tmp_leftover(db, tmp_path):
    _write_mcp(tmp_path, _MCP_SRV)
    await execute_proxy(db, _route("mcp", "off web-reader"), FakeCfg(tmp_path))
    assert list((tmp_path / "claude").glob("*.tmp")) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_proxy.py -k mcp -v`
Expected: 新增测试 FAIL（现有 `_mcp` 无 on/off；列表无状态标记）

- [ ] **Step 3: Write minimal implementation**

`gateway/proxy.py` 改三处。

① 抽公共原子写（`_save_settings` 下方新增，`_save_settings` 改为调用它）：

```python
def _atomic_write_json(path, data) -> None:
    """原子写 JSON：同目录临时文件 + os.replace。截断式 write_text 中途崩溃
    会留半写文件，下次读取方（claude / gateway 启动）读失败。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _save_settings(config, data) -> None:
    """原子写 claude/settings.json（见 _atomic_write_json）。"""
    _atomic_write_json(_settings_path(config), data)
```

（原 `_save_settings` 函数体删除，调用方不变。）

② 常量区（`PERMISSIONS_USAGE` 旁）加：

```python
MCP_USAGE = "用法：/mcp — 列表；/mcp off <序号|名字> 停用；/mcp on <序号|名字> 启用"
```

③ `_mcp` 整体替换（`execute_proxy` 的调用处同步改为 `_mcp(db, config, route.args.strip())`）：

```python
# ---- /mcp：列 claude/mcp.json + on/off 启停（顶层 disabled 标记）----

def _load_mcp(config):
    """读 mcp.json；返回 (path, raw dict)。文件缺失返回 (path, None)。"""
    path = config.repo_root / "claude" / "mcp.json"
    if not path.is_file():
        return path, None
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise NotJsonObjectError(path)
    return path, raw


def _mcp(db, config, args: str) -> str:
    path, raw = _load_mcp(config)
    if raw is None:
        return "未找到 claude/mcp.json。"
    servers = raw.get("mcpServers") or {}
    disabled = raw.get("disabled")
    disabled = disabled if isinstance(disabled, list) else []

    parts = args.split()
    if parts and parts[0] in ("on", "off"):
        if len(parts) != 2:
            return MCP_USAGE
        return _mcp_toggle(db, path, raw, servers, disabled,
                           parts[0], parts[1])

    if not servers:
        return "claude/mcp.json 中没有配置 MCP server。"
    lines = ["🔌 mcpServers（claude/mcp.json；启停下一任务生效）："]
    for i, (name, svc) in enumerate(servers.items(), 1):
        cmd = svc.get("command", "?") if isinstance(svc, dict) else "?"
        first_arg = f" {svc['args'][0]}" if isinstance(svc, dict) and svc.get("args") else ""
        mark = "⛔" if name in disabled else "✅"
        lines.append(f"  {i}. {name} {mark} — {cmd}{first_arg}")
    lines.append(MCP_USAGE)
    return "\n".join(lines)


def _mcp_toggle(db, path, raw, servers, disabled, op, target) -> str:
    """on/off 单个 server：名字精确匹配优先，否则 1-based 序号（与列表一致）。"""
    name = None
    if target in servers:
        name = target
    elif target.isascii() and target.isdigit():
        n = int(target)
        keys = list(servers)
        if 1 <= n <= len(keys):
            name = keys[n - 1]
        else:
            return f"序号越界：共 {len(keys)} 个 server。"
    if name is None:
        return (f"没有这个 server：{target}（当前：{', '.join(servers) or '（空）'}）")

    if op == "off":
        if name in disabled:
            return f"{name} 已是停用状态。"
        disabled = [*disabled, name]
        raw["disabled"] = disabled
        _atomic_write_json(path, raw)
        db.audit("config_change", f"mcp off {name}")
        return f"已停用 {name}，下一任务生效（配置保留，/mcp on {name} 可再启）。"
    # on
    if name not in disabled:
        return f"{name} 已处于启用状态。"
    disabled = [d for d in disabled if d != name]
    raw["disabled"] = disabled      # 空数组也留键（与静态 mcp.json 初始形态一致）
    _atomic_write_json(path, raw)
    db.audit("config_change", f"mcp on {name}")
    return f"已启用 {name}，下一任务生效。"
```

`execute_proxy` 中 mcp 分支改为：

```python
    if cmd == "mcp":
        try:
            return _mcp(db, config, route.args.strip())
        except NotJsonObjectError as e:
            return f"配置文件格式异常（顶层不是对象）：{e}"
        except ValueError as e:
            return f"claude/mcp.json 解析失败：{e}"
```

注意：现有只读测试 `test_mcp_lists_servers` 断言 `"cmd /c" in reply` 与 `"uvx mcp-server-fetch" in reply`——新列表格式 `  {i}. {name} {mark} — {cmd}{first_arg}` 下，cmd 形态 fixture 显示 `cmd /c`（args[0] 是 "/c"）✓、uvx 显示 `uvx mcp-server-fetch` ✓，标题行从「（claude/mcp.json，只读；启停 M3 提供）」变为「（claude/mcp.json；启停下一任务生效）」——该测试未断言标题文字，不回归。

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python -m pytest tests/test_proxy.py -v`
Expected: 全 PASS（含既有 /mcp 只读与 /permissions 组零回归）

- [ ] **Step 5: Commit**

```bash
git add gateway/proxy.py tests/test_proxy.py
git commit -m "feat(m3-a): /mcp on/off 启停（顶层 disabled 标记 + 原子写 + audit）"
```

---

### Task 4: /config set 七键白名单写入

**Files:**
- Modify: `gateway/proxy.py`（`_config` 扩展）
- Test: `tests/test_proxy.py`（追加）

**Interfaces:**
- Consumes: `_atomic_write_json(path, data)`（Task 3 产出）
- Produces: `/config set <键> <值>`；`CONFIG_KEYS` 白名单表（文档用，无外部消费）

- [ ] **Step 1: Write the failing test**

在 `tests/test_proxy.py` 追加：

```python
# ---- /config set 白名单写入 ----

def _read_gateway_config(root):
    return json.loads(
        (root / "gateway" / "config.json").read_text(encoding="utf-8"))


async def test_config_set_all_seven_keys(db, tmp_path):
    _write_gateway_config(tmp_path)
    cases = [
        ("throttle.min_send_interval_s", "0.5", 0.5),
        ("throttle.progress_window_s", "3", 3.0),
        ("throttle.page_char_limit", "1500", 1500),
        ("throttle.daily_send_limit", "300", 300),
        ("budget.max_turns", "30", 30),
        ("budget.max_usd", "2.5", 2.5),
        ("worker.concurrency", "2", 2),
    ]
    for key, val, expect in cases:
        reply = await execute_proxy(db, _route("config", f"set {key} {val}"),
                                    FakeCfg(tmp_path))
        assert "已写入" in reply and "重启生效" in reply, key
    doc = _read_gateway_config(tmp_path)
    assert doc["throttle"]["min_send_interval_s"] == 0.5
    assert doc["throttle"]["progress_window_s"] == 3.0
    assert doc["throttle"]["page_char_limit"] == 1500
    assert doc["throttle"]["daily_send_limit"] == 300
    assert doc["budget"]["max_turns"] == 30
    assert doc["budget"]["max_usd"] == 2.5
    assert doc["worker"]["concurrency"] == 2
    # 白名单外原样保留
    assert doc["whitelist"] == ["u@im.wechat"]
    assert doc["default_cwd"] == "/srv/proj"
    assert len(_audit_details(db, "config_change")) == 7


async def test_config_set_creates_missing_section(db, tmp_path):
    # worker 节在原文件缺失 → set 自动建节，其余键保留
    (tmp_path / "gateway").mkdir()
    (tmp_path / "gateway" / "config.json").write_text(
        '{"whitelist": ["u@im.wechat"]}', encoding="utf-8")
    reply = await execute_proxy(
        db, _route("config", "set worker.concurrency 4"), FakeCfg(tmp_path))
    assert "已写入" in reply
    doc = _read_gateway_config(tmp_path)
    assert doc["worker"]["concurrency"] == 4
    assert doc["whitelist"] == ["u@im.wechat"]


async def test_config_set_rejects_non_whitelist_key(db, tmp_path):
    _write_gateway_config(tmp_path)
    for key in ("whitelist", "claude_bin", "reconnect.session_duration_s",
                "default_cwd"):
        reply = await execute_proxy(db, _route("config", f"set {key} 1"),
                                    FakeCfg(tmp_path))
        assert "不开放" in reply and "直接改 gateway/config.json" in reply, key
    assert "whitelist" in _read_gateway_config(tmp_path)   # 文件未动


async def test_config_set_rejects_bad_type(db, tmp_path):
    _write_gateway_config(tmp_path)
    reply = await execute_proxy(
        db, _route("config", "set budget.max_turns 1.5"), FakeCfg(tmp_path))
    assert "整数" in reply
    reply = await execute_proxy(
        db, _route("config", "set throttle.min_send_interval_s fast"), FakeCfg(tmp_path))
    assert "数值" in reply


async def test_config_set_rejects_out_of_range(db, tmp_path):
    _write_gateway_config(tmp_path)
    bad = [("throttle.min_send_interval_s", "0"),        # > 0
           ("throttle.progress_window_s", "-1"),
           ("throttle.page_char_limit", "199"),          # ≥ 200
           ("throttle.daily_send_limit", "0"),           # ≥ 1
           ("budget.max_turns", "0"),                    # ≥ 1
           ("budget.max_usd", "0"),
           ("worker.concurrency", "11"),                 # 1~10
           ("worker.concurrency", "0")]
    for key, val in bad:
        reply = await execute_proxy(db, _route("config", f"set {key} {val}"),
                                    FakeCfg(tmp_path))
        assert "范围" in reply, (key, val)
        assert _read_gateway_config(tmp_path)["budget"]["max_turns"] == 50  # 未动


async def test_config_set_bad_usage(db, tmp_path):
    _write_gateway_config(tmp_path)
    for args in ("set", "set throttle.page_char_limit", "bump x y"):
        reply = await execute_proxy(db, _route("config", args), FakeCfg(tmp_path))
        assert "用法" in reply, args


async def test_config_set_atomic_no_tmp_leftover(db, tmp_path):
    _write_gateway_config(tmp_path)
    await execute_proxy(
        db, _route("config", "set worker.concurrency 2"), FakeCfg(tmp_path))
    assert list((tmp_path / "gateway").glob("*.tmp")) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_proxy.py -k config_set -v`
Expected: FAIL（`_config` 无 set 分支，回执是概要不含「已写入」）

- [ ] **Step 3: Write minimal implementation**

`gateway/proxy.py`：

① 常量区追加（`MCP_USAGE` 旁）：

```python
CONFIG_USAGE = ("用法：/config — 概览；/config set <键> <值>（可改键："
                "throttle.min_send_interval_s/progress_window_s/"
                "page_char_limit/daily_send_limit、budget.max_turns/max_usd、"
                "worker.concurrency；重启生效）")
```

② 白名单表（`_THROTTLE_LABELS` 旁）：

```python
# /config set 白名单：key -> (解析器, 校验器, 类型名)。范围外的键拒绝（whitelist
# 从微信改 = 放别人进服务器，安全不开放——其余提示改文件）。
def _is_int(s: str) -> bool:
    return s.lstrip("-").isdigit()


def _is_float(s: str) -> bool:
    try:
        float(s)
        return True
    except ValueError:
        return False


CONFIG_KEYS = {
    "throttle.min_send_interval_s": (float, lambda v: v > 0, "数值"),
    "throttle.progress_window_s": (float, lambda v: v > 0, "数值"),
    "throttle.page_char_limit": (int, lambda v: v >= 200, "整数"),
    "throttle.daily_send_limit": (int, lambda v: v >= 1, "整数"),
    "budget.max_turns": (int, lambda v: v >= 1, "整数"),
    "budget.max_usd": (float, lambda v: v > 0, "数值"),
    "worker.concurrency": (int, lambda v: 1 <= v <= 10, "整数"),
}
```

③ `_config` 改造（`execute_proxy` 的调用处同步改为 `_config(config, route.args.strip())`）：

```python
def _config(config, args: str) -> str:
    path = config.repo_root / "gateway" / "config.json"
    if not path.is_file():
        return "未找到 gateway/config.json。"
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise NotJsonObjectError(path)

    parts = args.split()
    if parts:
        if parts[0] != "set":
            return f"未知子命令：{parts[0]}\n{CONFIG_USAGE}"
        return _config_set(config, path, raw, parts[1:])

    # 概览（现状不变，仅标题改写 + 尾行加用法）
    throttle = raw.get("throttle") or {}
    thr = " · ".join(f"{label} {throttle.get(key, '默认')}"
                     for key, label in _THROTTLE_LABELS)
    budget = raw.get("budget") or {}
    n = _secrets_count(config)
    secrets_line = (f"secrets：已配置 {n} 项（claude/secrets.env，值不回显）" if n
                    else "secrets：未配置（claude/secrets.env）")
    return "\n".join([
        "🛠 gateway/config.json（概览；set 可改常用键，重启生效）：",
        f"白名单：{len(raw.get('whitelist') or [])} 个账号",
        f"默认目录：{raw.get('default_cwd') or str(config.repo_root)}",
        f"预算：max_turns={budget.get('max_turns', '未设置')} / "
        f"max_usd=${budget.get('max_usd', '未设置')}",
        f"节流：{thr}",
        secrets_line,
        "Claude 实例配置：claude/settings.json · claude/mcp.json",
        CONFIG_USAGE,
    ])


def _config_set(config, path, raw, rest) -> str:
    """set <键> <值>：白名单 + 类型 + 范围校验，读原文改键整体原子写回。
    成功时回执以「已写入」开头——execute_proxy 据此记 audit。"""
    if len(rest) != 2:
        return CONFIG_USAGE
    key, val = rest
    spec = CONFIG_KEYS.get(key)
    if spec is None:
        return (f"键 {key} 不开放微信修改，请直接改 gateway/config.json"
                f"（可改键见 /config 用法行）")
    parser, check, type_name = spec
    if not (_is_int(val) if parser is int else _is_float(val)):
        return f"值 {val} 不是合法{type_name}。"
    v = parser(val)
    if not check(v):
        return f"值 {v} 超出允许范围（{key} 的合法范围见 /config 用法行与文档）。"

    section, _, leaf = key.partition(".")
    raw.setdefault(section, {})
    if not isinstance(raw[section], dict):
        return f"配置节 {section} 不是对象，请直接改 gateway/config.json。"
    raw[section][leaf] = v
    _atomic_write_json(path, raw)
    return (f"已写入 {key}={v}，重启生效（systemctl restart daoyu）。"
            f"当前运行中的旧值继续使用。")
```

`execute_proxy` 中 config 分支改为（audit 落在此处——它持有 db；detail 形如 `config set throttle.page_char_limit=1500`，与 spec §3.1 一致）：

```python
    if cmd == "config":
        try:
            reply = _config(config, route.args.strip())
            if reply.startswith("已写入"):
                db.audit("config_change",
                         f"config set {'='.join(route.args.split()[1:3])}")
            return reply
        except NotJsonObjectError as e:
            return f"配置文件格式异常（顶层不是对象）：{e}"
        except ValueError as e:
            return f"gateway/config.json 解析失败：{e}"
```

（`route.args.split()[1:3]` 取 set 后的键与值、以 `=` 连接——`set throttle.page_char_limit 1500` → `throttle.page_char_limit=1500`。）

注意既有测试 `test_config_overview_redacts_secrets` 等断言概览行文字——标题行从「（只读，改文件后重启生效）」变为「（概览；set 可改常用键，重启生效）」、尾行新增用法：现有断言（`"白名单：1 个" in reply` 等）均不涉标题全文比对，不回归。

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python -m pytest tests/test_proxy.py -v`
Expected: 全 PASS

Run: `.venv/Scripts/python -m pytest`
Expected: 全量绿（263+ passed）

- [ ] **Step 5: Commit**

```bash
git add gateway/proxy.py tests/test_proxy.py
git commit -m "feat(m3-a): /config set 七键白名单写入（类型+范围校验 + 原子写 + audit）"
```

---

### Task 5: /help 更新 + 文档勘误

**Files:**
- Modify: `gateway/bridge.py`（`PROXY_HELP` 两行，现约 23-27 行）
- Modify: `README.md`（M2 边界 /mcp /config 行）
- Modify: `CLAUDE.md`（代理命令行、M2 清单 MCP 装载行）
- Modify: `docs/TRD.md`（§9 表 /207 行附近「只读」口径）
- Test: `tests/test_help.py` 若有 proxy 用法断言则同步（先 grep）

**Interfaces:**
- Consumes: Task 3/4 的最终命令语法
- Produces: 无代码接口（文案与文档）

- [ ] **Step 1: grep 现有断言**

Run: `grep -rn "只读" tests/ gateway/bridge.py | grep -i -e mcp -e config`
确认 /help 相关测试是否断言「只读」字样；有则同步改。

- [ ] **Step 2: 更新 PROXY_HELP**

`gateway/bridge.py` 的 `PROXY_HELP` dict 两行改为：

```python
PROXY_HELP = {
    "permissions": "/permissions — 查看权限规则；deny add/del、allow add 读写",
    "mcp": "/mcp — 列出 MCP server；off/on <序号|名字> 启停（下一任务生效）",
    "config": "/config — gateway 配置概览；set <键> <值> 改常用键（重启生效）",
}
```

- [ ] **Step 3: 文档三处勘误**

`README.md` M2 边界节「`/mcp`、`/config` 只读」行改为：

```markdown
- **`/mcp`、`/config`**：/mcp 列表 + on/off 启停（下一任务生效，停用不丢配置）；/config 概览 + set 改常用键（throttle/budget/concurrency 七键，重启生效）。whitelist 等不开放，改 gateway/config.json。
```

`CLAUDE.md`：
- 「配置代理命令」行（M2 功能清单）：`/permissions`（列表 + deny add/del + allow add，写 `claude/settings.json`）、`/mcp`（列表 + on/off 启停，写顶层 disabled）、`/config`（概览 + set 七键白名单，重启生效）。
- 「MCP 装载」行补：`claude/mcp.json` 平台无关形态（command 直写 npx/uvx），runner 合并层 Windows 包 `cmd /c`、过滤 disabled。

`docs/TRD.md` §9 持久级变更行（「/config /permissions /mcp 写 settings/mcp.json」处）补一句：/mcp on/off 与 /config set 已提供（引用 spec 余项 A）。

- [ ] **Step 4: 全量回归 + Commit**

Run: `.venv/Scripts/python -m pytest`
Expected: 全量绿

```bash
git add gateway/bridge.py README.md CLAUDE.md docs/TRD.md
git commit -m "docs(m3-a): /help 与三文档勘误——/mcp 启停 + /config set 口径"
```

---

## 部署与真机验收（plan 外、controller 执行）

1. `git archive HEAD | ssh <user>@<server-ip> "tar x -C ~/proj/daoyu"` 部署。
2. 生产服务器 冷缓存预热：`npx chrome-devtools-mcp@latest --help`（或启动一次）、`npx -y @upstash/context7-mcp`、`uvx --with 'mcp~=1.0' mcp-server-fetch` 各跑一次确保缓存（spec §5.2）。
3. 重启 daoyu，微信验：`/mcp` 列表（三台 ✅）→ 发一个任务问 MCP 可用性（connected）→ `/mcp off web-reader` → `/mcp`（⛔）→ 下一任务 web-reader 缺席 → `/mcp on web-reader`；`/config set throttle.page_char_limit 1500` → 重启 → `/config` 显示 1500。
4. Windows 直启 npx 确认（spec §5.1）：git checkout 临时去掉展开层跑一次任务观察 FileNotFoundError——**可选**，仅登记实证；不做也可（白名单包装无害）。
