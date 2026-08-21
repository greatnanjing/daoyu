# 刀鱼 M5A：通知通道（事件接入）设计

- **日期**: 2026-08-21
- **状态**: 已实现（2026-08-21，400 测试全绿；真机验收另行）
- **配套文档**: [PRD.md](../../PRD.md) / [TRD.md](../../TRD.md)
- **背景**: M4 主动服务完成后，余下三方向（通知通道事件接入 / 媒体二期 / 输入体验增强）按「通知 → 媒体 → 输入」顺序立项。本 spec 覆盖通知通道全量；媒体二期、输入体验为后续独立 spec。

---

## 1. 背景与决策记录

| 问题 | 结论 |
|---|---|
| 事件源范围 | **四类全做**：shell CLI 入口、终端 Claude Code 会话事件（hooks）、headless 任务中通知（MCP 工具）、本机 HTTP 入口 |
| 通知语义 | **纯单向推送**：不建任务、不进任何会话、不影响 Claude 对话流（与 M2 告警 / M4 日报同定位） |
| 架构 | **outbox 直写复用**：所有入口写 outbox 行，现有出站协程照常投递——节流/重试/死信/分页/崩溃恢复全部自然继承。弃内存事件总线（进程重启丢通知，且 CLI/MCP 跨进程仍绕不开 DB）；弃仅 CLI 最小集（四入口已全选） |
| MCP notify 目标 | **任务属主**（`DAOYU_TO_USER` 注入，同 send_image 先例）——headless 任务是某用户发起的，通知回该用户，免白名单管道 |
| CLI / HTTP 目标 | **全部白名单广播**（先例四处：app.py / scheduler / outbound / reconnect 的告警广播） |
| 终端 hooks 接入 | **零代码**：`--hook` 模式的 CLI + deploy/ 配置片段示例，用户一次性配进宿主 `~/.claude/settings.json`；daoyu 不自动安装 hooks |
| HTTP 鉴权 | 绑定 `127.0.0.1`；`secrets.env` 设 `notify_token` 则要求 `Authorization: Bearer <token>`，不设则免鉴权（仅 localhost 可达） |
| 前缀约定 | 🔔 通用通知；✅ 终端任务完成（Stop）；❓ Claude 等待确认（Notification）——与现有 ⚠️（告警）/ 🔐（审批）同一前缀语言 |
| 日限熔断关系 | 通知行走 outbox，**日限熔断对其同样生效**（防滥用兜底）。代价：外部源刷爆会触发全局熔断暂停全部出站——单用户 localhost 场景可接受，README 明示 |

## 2. 总体架构

```
                        ┌─ CLI: daoyu-notify <标题> [正文…]（含 --hook 模式，独立进程）
                        ├─ MCP: mcp__daoyu__notify(title, body)（approval_mcp 子进程）
外部事件 ──→ common/notify.py ──→ outbox 行 ──→ 现有出站协程（节流/分页/重试/死信）
                        ├─ HTTP: POST 127.0.0.1:8417/notify（gateway 进程内 aiohttp）
                        └─ 终端 hooks: Claude Code Stop/Notification → 调 CLI --hook
```

关键事实依据（均已核实）：

- [gateway/outbound.py](../../../gateway/outbound.py) `run_forever` 每秒 `next_outbox_batch` 批读 outbox 表——**跨进程写入的行天然被下一轮拾取**（approval_mcp `send_image` 的裸 SQL 跨进程写即是运行中先例）。
- [common/config.py](../../../common/config.py) `load_config()` 默认 `repo_root = Path(__file__).resolve().parents[1]`——CLI 依赖包定位 repo，**无需任何环境变量**即可找到 DB 与白名单。
- aiohttp 已在 pyproject dependencies——HTTP 入口零新增依赖。
- [common/db.py](../../../common/db.py) `audit(kind, detail)`——每条通知记一行审计。

## 3. 组件设计

### 3.1 核心 `common/notify.py`（新模块）

| 函数 | 签名与行为 |
|---|---|
| `format_notification` | `(prefix: str, title: str, body: str = "") -> str`：`"{prefix} {title}"`，body 非空则换行追加。纯函数 |
| `push_notification` | `(conn: sqlite3.Connection, to_users: Iterable[str], title: str, body: str = "", *, source: str, prefix: str = "🔔") -> int`：拼格式 → 逐用户 INSERT outbox 行（task_id=None、kind 默认文本）→ INSERT audit_log（kind=`notify`、detail=`"{source}: {title[:40]}"`）→ commit，返回写入行数 |

取裸 `sqlite3.Connection` 而非 Database 类：gateway/CLI 传 `Database._conn`、approval_mcp 传自己的 `sqlite3.connect` 连接（[worker/approval_mcp.py:32](../../../worker/approval_mcp.py#L32) 现状），两类调用方零适配。

### 3.2 CLI `daoyu-notify`（新 `gateway/notify_cli.py`，pyproject 第三个 console script）

```
daoyu-notify <标题> [正文…]            # 正文多段以空格拼接
daoyu-notify --hook stop               # 从 stdin 读 Claude Code hooks JSON
daoyu-notify --hook notification
```

- 流程：`load_config()` → `Database(cfg.db_path)` + **显式 `ensure_schema()`**（幂等——构造器不跑它，[common/db.py](../../../common/db.py) 现状；DB 未建则现建，CLI 先于 gateway 启动也不炸）→ `push_notification`（`to_users = sorted(cfg.whitelist)` 广播）。成功 exit 0；失败（DB 不可达等）stderr 一行 + exit 1，**不静默**。
- `--hook stop`：读 stdin JSON，格式 `✅ 终端任务完成` + 换行 `📁 {cwd}`（字段缺席则降级为仅标题——hooks JSON 字段名以真机实测为准，容错优先）。
- `--hook notification`：格式 `❓ Claude 等待确认` + 换行 `{message}`（message 字段缺席降级同上）。
- `--hook` 模式对 JSON 解析失败容错：整段 stdin 文本（截 200 字）作正文照推，exit 0——hooks 场景通知失败不应阻塞宿主会话流。

### 3.3 MCP 工具 `notify`（[worker/approval_mcp.py](../../../worker/approval_mcp.py) 扩展）

- 工具名 `notify`，参数 `title: str, body: str = ""`；返回纯文本确认（普通工具返回，非审批）。
- `DAOYU_TOOLS` 装配：strict 档 = `approve,send_image,notify`，其余档 = `send_image,notify`（四档恒装，同 send_image 口径）。
- 写入走 `push_notification(conn, [DAOYU_TO_USER], ..., source="mcp")`（复用核心，不再另写裸 SQL）。
- `claude/settings.json` 加 allow `mcp__daoyu__notify`。
- 已知限制（回执/文档明示）：bg 任务摘除 `--mcp-config`，**bg 内 notify 不可用**（同 send_image）。

### 3.4 HTTP 入口（[gateway/app.py](../../../gateway/app.py) `main_async` 内新协程）

- aiohttp `web.AppRunner` 监听 `notify.listen`（默认 `127.0.0.1:8417`），单路由 `POST /notify`：
  - 请求体 `{"title": str, "body": str = ""}`；title 缺失 → 400。
  - `secrets.env` 有 `notify_token` → 校验 `Authorization: Bearer <token>`，不符 401。
  - 通过 → `push_notification(db._conn, sorted(cfg.whitelist), ..., source="http")` → 响应 `{"queued": <行数>}`。
- 协程自保护：整协程 try/except 记 audit 不杀进程（同 scheduler 模式）——HTTP 挂 ≠ 通道死。`notify.http_enabled=false` 时不启动监听。

### 3.5 终端 hooks 配置（deploy/ 示例 + README，零代码）

- 新增 [deploy/notify-hooks.example.json](../../../deploy/)：Stop 与 Notification 两事件的 hooks JSON 片段，命令均为 `daoyu-notify --hook <event>`。
- README 新增「通知通道」小节：粘贴方法（宿主 `~/.claude/settings.json` 的 `hooks` 节）、CLI 直用示例、HTTP curl 示例。

## 4. 配置与安全

| 项 | 位置 | 说明 |
|---|---|---|
| `notify.listen` | `config.json` 新增 `notify` 节（默认 `{"listen": "127.0.0.1:8417", "http_enabled": true}`），config.example.json 同构 | 低频运维键，直接改文件，**不进 /config set 白名单** |
| `notify_token` | `claude/secrets.env`（可选） | 设则 HTTP 强制 Bearer；不设则仅 localhost 绑定兜底 |
| 审计 | audit_log kind=`notify` | source ∈ {cli, hook:stop, hook:notification, mcp, http} |
| 攻击面 | HTTP 仅 127.0.0.1（+可选 token）；CLI/MCP 仅本机进程 | 一切出站仍受 `daily_send_limit` 熔断约束——外部源刷爆会暂停全部出站（含对话回复），README 明示此代价 |

## 5. 测试策略

| 层 | 内容 |
|---|---|
| 单测 | `format_notification` 纯函数；`push_notification`（内存 DB：逐用户行数、audit 行、kind/task_id 断言）；CLI（参数拼装、`--hook` stdin JSON 正常/畸形/字段缺席降级、失败 exit 1）；`DAOYU_TOOLS` 装配三档 |
| MCP | `notify` 协议往返（仿 approval 现有测试）：成功写 outbox 行、DAOYU_TO_USER 定向、title 缺失 400 类错误 |
| HTTP | aiohttp test utils：鉴权关/开（401）、title 缺失 400、广播白名单行断言、`http_enabled=false` 不监听 |
| E2E | tests/test_e2e.py 加一幕：CLI 子进程真写 outbox → 出站协程拾取 → fake iLink sendmessage 断言 🔔 前缀文本 |

## 6. 明确不做

- 通知跳队（outbox FIFO 够用，延迟秒级）
- 可回复/可执行动作的通知（YAGNI，通知 ≠ 对话）
- 出站 webhook（daoyu → 外部系统，方向相反，非本项）
- bg 任务内 notify（bg 无 MCP，结构性限制）
- 通知去重 / 按源限速（日限熔断已兜底；真出现滥用再加）
- daoyu 自动安装/管理宿主 hooks 配置（单用户一次性手配，示例文件 + README 足够）
