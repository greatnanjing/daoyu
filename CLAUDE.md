# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

**刀鱼 (daoyu)**：把微信变成 Claude Code 的遥控器。用户在微信发消息 → 转发给服务器上 headless 模式运行的 Claude Code → 回答与执行进度实时回微信。单用户产品（仅作者本人），单台 Linux 服务器部署，systemd 托管。默认工作仓库即本仓库（自举/dogfood）。

## 当前状态

**设计完成、代码未开始**（截至 2026-08-15）。仓库现有内容仅 `docs/` 下两份已确认的权威文档，所有实现决策以它们为准：

- [docs/PRD.md](docs/PRD.md) — 产品需求（功能 FR-1~10、非功能需求、里程碑 M1/M2/M3、范围外）
- [docs/TRD.md](docs/TRD.md) — 技术设计（架构、SQLite 数据模型、claude CLI 调用规范、命令路由、安全设计、测试策略）

尚无构建/测试系统与 git 仓库。代码落地后应在本文件补充常用命令。

## 核心架构（写代码前必读）

三组件 + 一条持久化脊柱，gateway 与 worker 同进程（单个 systemd 服务）：

1. **gateway**（Python asyncio，fork weixin-ClawBot-API 收发层）：iLink 长轮询收微信消息 → 入站落盘去重 → 命令路由（本地命令秒回 / 其余入队）→ 出站发送（重试、分页、节流）。**永不阻塞、绝不等 Claude**——agent 慢不影响微信端。
2. **SQLite**（`data/daoyu.db`，WAL 模式）：唯一事实源。messages / tasks / outbox / sessions / audit_log 五表。**一切先落盘**，任何进程崩溃后可完整恢复（启动时 running 任务重置重跑、pending 消息重投）。
3. **worker**（同进程 asyncio task 池，并发 2~3）：取任务 → 按官方规范组装 claude 命令行 → 子进程执行 → 解析 stream-json → 节流推进度 → 写 outbox。

**关键认知**：后端没有独立的 agent 框架——**智能本体就是 Claude Code CLI 本身**（`claude -p` headless 子进程）。worker 只是"保姆"代码：取任务、拼命令行、起子进程、解析输出流、回推结果。工具、MCP、skills、上下文管理全部由 Claude Code 原生提供，worker 一概不重新实现。

## 硬性技术约束（违反即 bug）

- **每次调用 claude CLI 全量传 flag**：`--resume` 不恢复 `--permission-mode` / `--mcp-config` / `--add-dir`，必须每次重传。
- **同一 Claude 会话（同 session UUID）的任务必须串行**（`--resume` 同会话并发会冲突）；不同会话可并行。任务队列按 session 分组串行。
- **resume 必须在同一 cwd**（Claude 按 cwd + git worktree 作用域）；`/cd` 切目录 = 换绑另一会话。
- **用户 prompt 经 stdin 传入** `claude -p`，避免 shell 转义问题；子进程 cwd = 会话绑定的工作目录。
- **长任务必须走 `claude --bg` + `claude logs <id>` 轮询**：`-p` 结束 5s 会杀后台 bash，subagent 默认上限 10min。
- **`context_token` 只使用当前会话最新入站消息的**，绝不复用历史值（复用旧 token 会 HTTP 200 但静默不投递）。
- **入站按 `msg_id` 幂等去重**（iLink 重连后消息会重投）；出站走 outbox 发件箱，失败重试，至少 5 次后才进死信并告警。
- **`--bare` 隔离宿主配置**：刀鱼 Claude 实例的持久配置在 `claude/settings.json` 与 `claude/mcp.json`（进 git），而非用户 `~/.claude`。代理命令（/permissions /config /mcp 等）改的是刀鱼专属配置文件，效果等价、天然版本化。

## 统一命令总线（产品核心）

微信命令与 Claude Code CLI **同一套语法、同一个命名空间**，不发明第二套命令体系。路由顺序：

1. **桥命令**（仅 `/cancel` `/tasks` `/status` `/cd` `/policy`）→ gateway 本地执行，秒回。另有 iLink 运维命令 `/time` `/重新连接`（管连接本身，与 Claude 无关）。
2. **转发**：headless 可用命令集（启动时从 `system/init` 事件的 `slash_commands` 同步）→ 原样作为 prompt 传给 claude。
3. **代理**：TUI 交互专属命令（静态维护清单：/permissions /hooks /plugins /login 等）→ 拦截后以相同命令名与参数格式操作同一底层配置，输出文字版。
4. 都不是 → 未知命令提示 + 最接近命令建议。

`/help` 由三层合并动态生成，永远与实际能力一致。

## 计划目录结构（TRD §7）

```
├── docs/                       # PRD / TRD
├── gateway/ worker/ common/    # 源码
├── claude/
│   ├── settings.json           # 刀鱼 Claude 实例持久配置（permissions 等）— 进 git
│   ├── mcp.json                # MCP server 清单 — 进 git
│   └── secrets.env             # API key 等 — gitignore
├── gateway/config.json         # 白名单微信号、节流参数 — gitignore
├── data/daoyu.db               # SQLite — gitignore
└── deploy/daoyu.service        # systemd 单元
```

## 安全底线

- **硬 deny 清单**（`/`、`/etc`、`~/.ssh`、`~/.claude`、`data/daoyu.db` 等）：auto/strict/plan 档恒生效；bypass 档下 deny 是否被尊重以实测为准，无论结果如何都叠加 `--disallowedTools` 工具级兜底。
- **预算闸**（`--max-turns` + `--max-budget-usd`）与权限档位独立、恒生效，bypass 下仍限费。
- `/policy` 四档：auto（默认全放）/ strict（shell 与仓外操作推微信 Y/N 审批，5 分钟超时视为拒绝）/ bypass / plan。
- secret 只放 `claude/secrets.env`（gitignore）+ 环境变量注入，日志脱敏。
- gateway 仅响应白名单微信账号，白名单外一律不响应。

## 实现顺序（勿颠倒依赖）

- **M1（MVP）**：SQLite schema → gateway 收发+落盘去重 → worker 调 `claude -p`（会话绑定、stream 解析、节流推送）→ 命令总线 → 崩溃恢复 → E2E。
- **M2**：审批 MCP（`--permission-prompt-tool`）→ `--bg` 长任务 → MCP 装载（chrome-devtools/tesseract-ocr/ai-vision/web-reader/context7）→ 配置代理命令全套 → `/policy` 四档 → 监控告警。
- **M3（二期）**：媒体收发（ClawBot CDN 加密上传）。

## 开放问题（涉及前先实测，勿凭假设实现）

TRD §11 登记的未决项：`/init` 在 headless 下的确切行为、bypass 档下 `permissions.deny` 是否生效、微信文本单条长度上限（分页阈值依据）、OCR MCP 选型、Claude Code 版本漂移（对策：固定版本 + 升级前跑 E2E 回归）。
