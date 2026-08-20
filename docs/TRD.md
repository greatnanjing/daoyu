# 刀鱼 (daoyu) 技术需求文档 (TRD)

- **版本**: v1.0
- **日期**: 2026-08-15
- **状态**: 已确认（与 [PRD.md](./PRD.md) 配套）
- **技术选型结论来源**: 本 session 调研与官方文档核实（Claude Code headless 文档、weixin-ClawBot-API 源码调研）

---

## 1. 总体架构

三个组件 + 一条持久化脊柱，全部运行于单台 Linux 服务器（`/home/<user>`）：

```
微信个人号 ──扫码──▶ ┌────────────────────────────────────────────┐
                     │  Linux 服务器 (~/proj/daoyu)                │
                     │                                             │
  ① gateway 前端     │  ClawBot 适配层 (Python)                    │
     (ClawBot)       │  iLink 长轮询收 / sendmessage 发 / sendtyping│
       │             │  统一命令路由 / 连接守护（被动重连）              │
       ▼             │      │ 入站落盘+去重                          │
  ② SQLite (WAL)    │  messages / tasks / outbox / sessions 表    │
       │             │      │ 派发                                   │
       ▼             │      ▼                                       │
  ③ worker 后端     │  worker 池 → 子进程调 claude CLI (headless)  │
                     │  流式解析 → 节流回推 → outbox                 │
                     └─────────────────────────────────────────────┘
```

**核心原则**（对应 PRD 两条硬约束的技术本质）：

1. **gateway 永不阻塞**：收消息 → 落盘 → 入队 → 秒回 ACK，绝不等 Claude。Agent 慢不影响微信端。
2. **一切先落盘**：入站消息、任务状态、出站消息全部以 SQLite 为唯一事实源，任何进程崩溃后可完整恢复。

**重要澄清**（"后端 Agent 是什么"）：后端没有独立的 agent 框架。**智能本体就是 Claude Code CLI 本身**（headless 形态）。worker 只是几十行"保姆"代码：取任务 → 按官方 CLI 规范拼命令行 → 起子进程 → 解析输出流 → 回推结果。工具、MCP、skills、上下文管理全部由 Claude Code 原生提供，worker 一概不重新实现。

## 2. 组件与职责

| 组件 | 形态 | 职责 |
|---|---|---|
| gateway | Python asyncio，fork `weixin-ClawBot-API` 收发层 | iLink 长轮询；入站落盘去重；命令路由（本地秒回 / 入队）；出站发送（含重试、分页、节流）；sendtyping；连接守护（token 失效自动重连 + 长周期主动续期） |
| worker | 同一服务内 asyncio task 池（并发 2~3） | 取任务；组装 claude 命令行；子进程执行；解析 stream-json；节流推进度；写 outbox；审批 MCP 宿主 |
| claude CLI | 子进程 `claude -p ...` / `claude --bg` | 全部智能行为 |
| SQLite | 单文件 `data/daoyu.db`，WAL 模式 | 唯一事实源 |

**并发约束**：同一 Claude 会话（同 session UUID）的任务必须串行（`--resume` 同会话并发会冲突）；不同会话可并行。任务队列按 session 分组串行、跨 session 并行。

## 3. 微信接入层（ClawBot / iLink）

### 3.1 通道事实（调研结论）

- 腾讯 2026 年经 OpenClaw 平台官方开放的个人微信 Bot API（微信 ClawBot 插件功能），协议 iLink，域名 `ilinkai.weixin.qq.com`，官方 sanctioned、无逆向封号风险。
- 长轮询 `getupdates`（服务器 hold 35s）——**无公众号式 5 秒必须返回的死线**，天然适配慢 agent 任务。
- 支持 `sendtyping`（"正在输入"状态）。
- 连接（bot_token）生命周期：**无官方 TTL**（官方 README 仅定义 `errcode -14 = session timeout`，不承诺时长；实证活跃存活 >2.6 天——2026-08-19 本机实例，"24h 过期"系社区讹传）；每次扫码登录 Bot ID 会变化（平台设计）；协议无免扫码续期路径（`binded_redirect` 是扫码时已绑定状态）。
- 媒体消息（图片）M3 已支持：CDN AES-128-ECB 加密上传/下载（协议细节见
  `docs/superpowers/specs/2026-08-19-m3-media-design.md` §2，源：官方
  @tencent-weixin/openclaw-weixin v2.4.6 dist 源码）；语音/文件/视频未实现。
- 腾讯保留内容过滤与限速权利，无 SLA。

### 3.2 已知陷阱与对策（丢消息防线之一）

| 陷阱 | 对策 |
|---|---|
| `sendmessage` 字段不全 → HTTP 200 但**静默不投递** | 封装单一发送函数，字段全填；出站走 outbox，带投递确认校验与重试 |
| `context_token` 复用旧值 → 不投递 | token 随入站消息落盘，**只使用当前会话最新入站消息的 token**，绝不复用历史值 |
| token 失效（errcode -14 / HTTP 401） | **被动重连**：poll_loop 双路检测（应用层 `errcode`/`ret=-14` 连续 5 次防抖 ≈25s；HTTP 401/403 连续 5 次）清 token → 推 ⚠️+二维码链接到微信（点链接确认即恢复）→ 扫码/确认后 token 原子替换；主动续期周期默认 30 天（`session_duration_s`），续期时静默优先（`local_token_list` 带旧 token 轮询 `silent_grace_s` 30s，超窗才推码） |
| 重连后消息重投 | 按 `msg_id` 幂等去重 |

### 3.3 出站节流（风控防护）

最小发送间隔（如 1s + 抖动）、进度消息合并（2~3s 窗口）、每日出站上限、超限熔断暂停并告警。

## 4. 后端：claude CLI 调用规范

### 4.1 组装规则（严格遵循官方 CLI 定义）

命令行严格按官方三类语法组装，**每次调用全量传 flag**（`--resume` 不恢复 permission-mode / `--mcp-config` / `--add-dir`，必须重传）：

| 类别 | 用到的项 |
|---|---|
| **options** | `--resume <UUID>`（会话）；`--permission-mode <auto档: acceptEdits \| strict档: default+审批MCP \| bypass \| plan>`；`--allowedTools` / `--disallowedTools`（按 policy 档装配）；`--mcp-config`；`--max-turns` / `--max-budget-usd`（预算闸）；`--output-format stream-json --verbose --include-partial-messages`；工作目录 = 子进程 cwd（`--add-dir` 仅显式放行的额外目录）。**不带 `--bare`**（实测它剥离 WebFetch/WebSearch/Write/Glob/Grep 全部扩展工具——智谱端点 WebSearch 可用，2026-08-19；宿主隔离由 CLAUDE_CONFIG_DIR 承担，与 --bare 无关；bg 分支保守集保留） |
| **prompt** | 用户微信消息文本（含 `/命令`）经 **stdin** 传入（避免 shell 转义问题），`claude -p` 从 stdin 读 |
| **command** | 长任务用 `claude --bg` 后台执行 + `claude logs <id>` 轮询（`-p` 结束 5s 会杀后台 bash、subagent 默认上限 10min，长任务必须走此路） |

组装示例（strict 档 + review 场景）：

```bash
claude -p \
  --resume "$SESSION_UUID" \
  --permission-mode acceptEdits \
  --allowedTools "Read,Grep,Glob,Bash(git *)" \
  --permission-prompt-tool mcp__daoyu__approve \
  --mcp-config /path/to/daoyu/mcp.json \
  --bare --max-turns 50 --max-budget-usd 5 \   # 注：-p 路径现已不带 --bare（见上表）；此处示例为 bg 分支形态
  --output-format stream-json --verbose --include-partial-messages
# cwd = 绑定的工作目录; prompt 经 stdin 传入
```

### 4.2 会话管理

- `sessions` 表维护 (微信用户, cwd) → Claude session UUID 映射；
- 首次调用用 `--session-id <预生成UUID>` 固定，之后 `--resume`；
- **约束**：resume 必须在同一 cwd（Claude 按 cwd + git worktree 作用域）；`/cd` 切目录 = 换绑另一会话；
- 会话上下文跨进程重启保持（Claude 本地会话存储）；
- **`/adopt [uuid前缀]`**：收养终端 TUI 创建的外部会话为当前话题（终端须用 `CLAUDE_CONFIG_DIR=<repo>/data/claude-home claude` 创建；扫描 claude-home projects 未管理 transcript，无参取 mtime 最新、≥8 位唯一前缀指定；收养行置 `claude_session_inited:<uuid>` 走 `--resume`）——微信 ↔ 终端交叉接续同一话题。

### 4.3 流式进度

解析 NDJSON 流事件：

| 事件 | 用途 |
|---|---|
| `system/init` | 首事件：`session_id`、可用 `slash_commands` 清单（同步到 `/help`） |
| `content_block_delta` (`text_delta`) | 增量回答文本 → 节流推送 |
| `content_block_start` (`tool_use`) + `input_json_delta` | "正在读 src/auth.py" 类工具进度 |
| `result` | 最终结果、`total_cost_usd`、usage → 写 outbox + 记账 |

### 4.4 审批（strict 档）

worker 内宿主一个本地 MCP server，暴露 `approve` 工具，配 `--permission-prompt-tool`。Claude 触发未授权操作 → worker 收到审批请求 → 推微信 "允许执行 `xxx`？Y/N" → 用户回复 → MCP 返回结果 → Claude 继续。审批请求带超时（默认 5 分钟，超时视为拒绝）。

## 5. 统一命令总线

**一套语法（官方 CLI 格式）、一个命名空间**。路由算法：

```
收到 "/xxx args"
  1. 若 xxx ∈ 桥命令(/cancel /tasks /status /cd /sessions /policy /bg /new /adopt)
     → gateway 本地执行, 秒回
     (另有继承自 ClawBot 库的 iLink 运维命令 /time /重新连接, 在 gateway 处理)
  2. 若 xxx ∈ headless 可用命令集(启动时从 system/init slash_commands 同步,
     含官方命令/skills/自定义命令) → 原样作为 prompt 转发给 claude
  3. 若 xxx ∈ TUI 交互专属集(/permissions /hooks /plugins /login 等官方定义) →
     gateway/worker 代理执行: 同名同参数格式, 读改同一底层配置, 文字版输出
  4. 都不是 → 按"未知命令"提示, 并给出最接近的命令建议
```

- 第 3 步的"TUI 交互专属集"为静态维护清单（源自官方 commands 文档），Claude Code 升级时对照更新；
- 代理命令操作的是刀鱼专属配置文件（见 §7），而非用户 `~/.claude`（因 `CLAUDE_CONFIG_DIR` 隔离）——效果等价、配置可版本化。
- `/help` 由三层合并动态生成，与实际能力永一致。

## 6. 数据模型（SQLite）

```sql
messages(            -- 入站
  id INTEGER PK, msg_id TEXT UNIQUE,   -- 微信 msg_id, 幂等去重键
  from_user TEXT, text TEXT,
  context_token TEXT,                  -- 仅存最新, 发送回包用
  received_at INTEGER, state TEXT      -- received/queued
)
tasks(               -- 任务
  id INTEGER PK, message_id INTEGER, session_id INTEGER,
  prompt TEXT, kind TEXT,              -- chat/command/bg
  state TEXT,                          -- pending/running/done/failed/dead/canceled
  attempts INTEGER, max_attempts INTEGER,
  claude_bg_id TEXT,                   -- --bg 任务 id
  created_at INTEGER, updated_at INTEGER
)
outbox(              -- 出站(发件箱)
  id INTEGER PK, task_id INTEGER, to_user TEXT,
  text TEXT, seq INTEGER,              -- 分页序号
  state TEXT,                          -- pending/sent/failed/dead
  attempts INTEGER DEFAULT 0, max_attempts INTEGER DEFAULT 5,  -- 对齐 PRD NFR-2: 至少重试 5 次
  last_error TEXT, created_at INTEGER
)
sessions(            -- 会话绑定
  id INTEGER PK, wechat_user TEXT, cwd TEXT,
  claude_uuid TEXT, policy TEXT DEFAULT 'auto',
  created_at INTEGER, last_active_at INTEGER,
  UNIQUE(wechat_user, cwd)
)
audit_log(           -- 审计: 命令/配置变更/审批记录/费用
  id INTEGER PK, ts INTEGER, kind TEXT, detail TEXT
)
```

**恢复流程**（进程启动时）：`tasks` 中 `running` → 重置为 `pending` 重跑（幂等由 Claude 会话语义兜底）；`outbox` 中 `pending/failed` → 重新投递。

## 7. 配置体系

刀鱼专属、与用户 `~/.claude` 隔离（`CLAUDE_CONFIG_DIR` 重定向 + 显式传入）：

```
~/proj/daoyu/
├── docs/                  # 本文档
├── gateway/  worker/  common/   # 源码
├── claude/
│   ├── settings.json      # 刀鱼 Claude 实例持久配置(permissions 等) — 进 git
│   ├── mcp.json           # MCP server 清单 — 进 git
│   └── secrets.env        # API key 等 — gitignore
├── gateway/config.json    # 白名单微信号、节流参数、告警渠道 — gitignore(含敏感项)
├── data/daoyu.db          # SQLite — gitignore
└── deploy/daoyu.service   # systemd 单元
```

`claude/mcp.json` 默认装载（与 PRD FR-5 对应）：

| server | 用途 | 启用 |
|---|---|---|
| chrome-devtools | 浏览器操控/截屏/网络/性能 | 默认 |
| daoyu-ocr | 图片文字提取（RapidOCR 本地封装；runner 恒注入，不受 /mcp 启停管辖） | 系统（恒装载） |
| web-reader | 网页抓取阅读 | 默认 |
| context7 | 库文档实时查询 | 默认 |
| playwright | 浏览器自动化备选 | 可选，默认关 |

- 会话级变更（`/model` 等）：转发官方命令，只影响当前会话；
- 持久级变更（`/config` `/permissions` `/mcp`）：写 `claude/settings.json` / `mcp.json`，下次调用生效；文件在 git 内 → 天然版本化、可回滚、可审计（配合 audit_log）。/mcp on/off 启停（写 mcp.json 顶层 disabled、下一任务生效）与 /config set 七键白名单写入（写 gateway/config.json、重启生效）已提供（2026-08-19 余项 A，spec `2026-08-19-mcp-config-writable-design`）。daoyu-ocr 为系统条目恒注入（2026-08-19 余项 B，spec `2026-08-19-ocr-mcp-design`）。

## 8. 安全设计

| 层 | 机制 |
|---|---|
| 权限档位 | `/policy auto/strict/bypass/plan`，逐任务翻译为 `--permission-mode` + `--allowedTools` |
| 硬 deny | `permissions.deny` 规则锁 `/`、`/etc`、`~/.ssh`、`~/.claude`、`data/daoyu.db` 等；**auto/strict/plan 档恒生效**。bypass 档下 deny 是否仍被尊重**以实测为准**（登记于 §11）；无论实测结果如何，worker 在 bypass 档额外叠加 `--disallowedTools`（工具级拒绝）兜底 |
| 预算闸（恒生效） | `--max-turns` + `--max-budget-usd`，与权限模式独立，bypass 下仍限费 |
| 审批 | strict 档经 `--permission-prompt-tool` 走微信 Y/N，5 分钟超时拒绝 |
| 白名单 | gateway 仅响应白名单微信账号 |
| secret | `secrets.env`（gitignore）+ 环境变量注入；日志脱敏 |
| 进程边界 | claude 子进程 cwd 锁定绑定目录；`CLAUDE_CONFIG_DIR` 重定向防误加载宿主配置 |

注：bypass 档即官方 `bypassPermissions`；预算闸与其独立、恒生效；deny 兜底见上表"硬 deny"行与 §11 开放问题。

## 9. 部署与运维

- **systemd**：`daoyu.service` 单服务（gateway + worker 同进程，`Restart=always`）。
- **首次/重连扫码**：终端 CLI 模式运行展示二维码（`qrcode[pil]` 终端渲染）；服务器无人值守时的重连二维码经备用渠道推送（邮件/server酱，部署时选定）。
- **监控**：`/status` 输出（队列深度、死信数、连接剩余时间、当日费用）；死信与预算超限触发告警（同备用渠道）。
- **备份**：`data/daoyu.db` 每日快照（cron + sqlite `.backup`）。

## 10. 测试策略

| 层 | 内容 |
|---|---|
| 单元 | 命令路由三分支；msg_id 去重；outbox 重试/死信状态机；命令行组装（policy→flags 映射）；长文分页 |
| 集成 | mock iLink HTTP（getupdates/sendmessage/sendtyping，模拟静默丢失/token 过期/重投）；mock claude 子进程（回放录制好的 stream-json） |
| E2E | 真机微信：多轮对话、`/review` 全流程、崩溃恢复（kill -9 后重启验证任务恢复与消息不丢）、审批往返、长任务 `--bg` |
| 混沌 | 进程各时点 kill；SQLite 锁竞争；iLink 断连重连 |

## 11. 风险与开放问题

| 项 | 状态 | 计划 |
|---|---|---|
| `/init` 在 headless 下的确切行为 | 官方文档未明说 | M1 期间实测；以 `system/init` 的 `slash_commands` 实际清单为准 |
| bypass 档下 `permissions.deny` 是否仍生效 | 未实测 | M2 实测；若不生效则以 `--disallowedTools` + 文件系统权限双兜底 |
| ClawBot 媒体（CDN 加密上传） | 已实现（M3 图片双向，真机验收 2026-08-19） | — |
| 重连二维码的无人值守推送渠道 | 待部署时选定（邮件/server酱） | M2 |
| 微信文本单条长度上限 | **已实测（2026-08-20）：16384 字节 UTF-8**（16384 ✓ / 16385 ✗ `prepare failed`；按**字节**计——中文 5450 字≈16350B ✓ / 5500 字≈16500B ✗、ASCII 12000 字 ✓；超限 `errcode=0` **静默不投递**） | [common/text.py](common/text.py) `split_text` 双上限：字符（`page_char_limit`）+ 字节硬闸 `MAX_PAGE_BYTES=15000`，无论 limit 配多大都防越线 |
| OCR MCP 具体 server 选型/封装 | 已落定：RapidOCR 本地封装（daoyu-ocr，rapidocr-onnxruntime 1.4.4，模型随包、bytes 直传） | 已实现（2026-08-19 余项 B） |
| Claude Code 版本漂移（flag 行为随版本变） | 持续风险 | 固定版本 + 升级前跑 E2E 回归 |

## 12. 实现顺序建议（对应 PRD 里程碑）

1. **M1**：SQLite schema → gateway 收发+落盘去重 → worker 调 `claude -p`（会话绑定、stream 解析、节流推送）→ 命令总线（转发/代理/桥命令）→ 崩溃恢复 → E2E。
2. **M2**：审批 MCP → `--bg` 长任务 → MCP 装载（chrome-devtools/OCR/AI 视觉/web-reader/context7）→ 配置代理命令全套 → `/policy` 四档完整 → 监控告警。
3. **M3（二期）**：媒体收发。

---

## 13. 实测勘误（2026-08-16，M2 实现期探针证实）

以下三处本文件原假设被真实 claude 2.1.233 实测推翻，实现以勘误为准（原文保留供追溯）：

1. **§4.1 "strict档: acceptEdits+审批MCP"** —— acceptEdits 权限模式下 `--permission-prompt-tool` **不触发**（需批准工具直接放行）；**default 模式才触发审批**。实现：`POLICY_MODE["strict"] = "default"`。另：审批工具必须返回 `{behavior: "allow", updatedInput?}` / `{behavior: "deny", message}` JSON（纯文本被判 invalid，决策不生效）。
2. **§5 路由顺序"转发（2）先于代理（3）"** —— 实测 `system/init` 的 `slash_commands` 含 `config`/`mcp`，若转发在前则代理命令永不可达。实现顺序改为：桥 → iLink 运维 → **代理 → 转发**。
3. **§7/§8 "--bare 隔离宿主配置"** —— 实测 `--bare` 与 `--settings` 均**不能**隔离宿主 `~/.claude`（宿主 defaultMode/allow/trustAllFiles 穿透生效，可架空审批与硬 deny）。实现：env 注入 `CLAUDE_CONFIG_DIR=<repo>/data/claude-home/` 机制化隔离（凭据仍经 secrets env 注入）。

## 14. 实测勘误（2026-08-19，M3 真机验收期证实）

以下本文件原假设被真机（claude 2.1.233 + 生产服务器 + 微信端）推翻或落定，实现以勘误为准（原文保留供追溯）：

1. **§4.1 command 行 "claude --bg 后台执行 + `claude logs <id>` 轮询"** —— 轮询应为 `claude agents --json --all`（**必须带 `--all`**，默认过滤 failed 条目）；停止是 `claude stop <id>`。`claude logs <id>` 实测**存在**（M2 曾记"无此子命令"亦不准确）但是 TUI 流、含 ANSI 转义，人读可、不宜程序解析。
2. **§4.1 bg 结果获取口径** —— bg 条目终态实测三值：`done` / `blocked` / `failed`（M2 写码假设的 `completed` 从未出现）；done 条目十字段**无输出/cost 字段**，取结果靠 `--fork-session` 回原会话要结果（直接 `--resume` 被 daemon 持有拒绝且 **rc=0**、错误只在输出——会静默空结果）；`blocked` = 会话等用户后续输入（Claude 结尾反问是常态），bg 无输入通道即永久挂起 → 首次观察即 fork 取结果完结。取结果 prompt 原"≤500 字总结"口径不可用（清单被压缩成统计）→ 改"逐项列出、1500 字内"。
3. **§4.1 "`--bg` 与 `--mcp-config` 组合"** —— 结构性不兼容：daemon 异步拉起 worker（客户端返回 ~1s 后才读 mcp config），临时文件在 run() 返回即删 → daemon "exit 1 before init" 100% 复现。实现：bg 摘除 `--mcp-config`，bg 会话无 MCP 工具（回执明示）。
4. **§11 "ClawBot 媒体（CDN 加密上传）未实现"** —— M3 已实现并真机验收（图片双向）。出站 `media.aes_key` 形态实测 = **base64(hex32 ASCII)**（spec 原写 base64(raw16B) 有误：真机传 raw16B 形态微信端收空白图）；MCP 工具 `send_image` 需 `claude/settings.json` allow（acceptEdits 不放行 MCP 工具、headless 无确认通道直接 deny）。协议细节见 M3 spec §2。

