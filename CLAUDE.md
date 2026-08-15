# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

**刀鱼 (daoyu)**：把微信变成 Claude Code 的遥控器。用户在微信发消息 → 转发给服务器上 headless 模式运行的 Claude Code → 回答与执行进度实时回微信。单用户产品（仅作者本人），单台 Linux 服务器部署，systemd 托管。默认工作仓库即本仓库（自举/dogfood）。

## 当前状态

**M1（MVP）已实现**（2026-08-16），94 个测试全绿（`python -m pytest`）。设计与实现决策仍以下列文档为准，实现与 TRD 的已知偏差登记在 `docs/superpowers/plans/2026-08-15-m1-mvp.md` Self-Review 节与 `.superpowers/sdd/` 各审查记录：

- [docs/PRD.md](docs/PRD.md) — 产品需求（功能 FR-1~10、非功能需求、里程碑 M1/M2/M3、范围外）
- [docs/TRD.md](docs/TRD.md) — 技术设计（架构、SQLite 数据模型、claude CLI 调用规范、命令路由、安全设计、测试策略）
- [README.md](README.md) — 部署、使用命令表与 M1 边界

组件清单（入口文件）：

- **入口**：`daoyu` console script → [gateway/app.py](gateway/app.py) `start()`（读 `gateway/config.json` + `claude/secrets.env`，崩溃恢复后常驻 poll / outbound / reconnect / worker-pool 四协程）；`daoyu-login` → [gateway/login.py](gateway/login.py)（终端扫码，token 写 DB state 后退出）。
- **gateway**：[gateway/ilink.py](gateway/ilink.py)（iLink 协议封装）、[gateway/router.py](gateway/router.py)（命令总线路由）、[gateway/bridge.py](gateway/bridge.py)（桥命令 + 三层 /help）、[gateway/outbound.py](gateway/outbound.py)（outbox 投递/重试/死信/节流/typing）、[gateway/reconnect.py](gateway/reconnect.py)（24h 连接过期守护）。
- **worker**：[worker/pool.py](worker/pool.py)（按 session 串行调度池）、[worker/cli_builder.py](worker/cli_builder.py)（claude argv 组装）、[worker/runner.py](worker/runner.py)（子进程执行/流式进度/费用记账）、[worker/stream.py](worker/stream.py)（stream-json 解析 + 节流器）。
- **common**：[common/db.py](common/db.py)（SQLite 五表 + state KV）、[common/config.py](common/config.py)（配置加载契约）、[common/models.py](common/models.py)、[common/text.py](common/text.py)（长文本分页）。
- **配置**：`gateway/config.example.json`（实例 config.json 进 gitignore）；`claude/settings.json` + `claude/mcp.json`（进 git，`--bare` 隔离宿主配置）；`claude/secrets.env`（gitignore）；`deploy/daoyu.service`（systemd 单元）。

## 常用命令

```bash
python -m pytest                        # 全量测试（94 个）
python -m pytest tests/test_e2e.py -v   # E2E（fake iLink + fake claude 子进程）
daoyu-login                             # 终端扫码登录（token 落盘后退出）
python -m gateway.app                   # 前台调试运行（不进 systemd）
```

Windows 开发机（Git Bash）下 venv 解释器在 `.venv/Scripts/python`，Linux 生产在 `.venv/bin/python`。

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
- **长任务必须走 `claude --bg` + `claude agents --json` 轮询**（M2 项；实测当前 CLI 无 `claude logs` 子命令，后台任务管理是 `claude agents`）：`-p` 结束 5s 会杀后台 bash，subagent 默认上限 10min。
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

- **硬 deny 清单**（`//etc/**`、`~/.ssh/**`、`~/.claude/**`、`//**/data/daoyu.db` 等；注意官方 permissions 语义：单前导 `/` 锚定 settings 来源目录而非绝对路径，**绝对路径必须 `//`**）：auto/strict/plan 档恒生效；bypass 档下 deny 是否被尊重以实测为准，无论结果如何都叠加 `--disallowedTools` 工具级兜底。
- **预算闸**（`--max-turns` + `--max-budget-usd`）与权限档位独立、恒生效，bypass 下仍限费；预算/回合耗尽的失败**不重试**（直接死信，防 3× 上限放大）。
- `/policy` 四档：auto（默认全放）/ strict（M1 期与 auto 同基线 acceptEdits，微信 Y/N 审批为 M2 项）/ bypass / plan。
- secret 只放 `claude/secrets.env`（gitignore）+ 环境变量注入，日志脱敏。
- gateway 仅响应白名单微信账号，白名单外一律不响应。

## 实现顺序（勿颠倒依赖）

- **M1（MVP）**：SQLite schema → gateway 收发+落盘去重 → worker 调 `claude -p`（会话绑定、stream 解析、节流推送）→ 命令总线 → 崩溃恢复 → E2E。
- **M2**：审批（⚠️ 原 TRD 方案依赖的 `--permission-prompt-tool` 已从当前 CLI 移除，需重选方案——如 `--permission-mode manual` + hooks/MCP 组合）→ `--bg` 长任务（`claude agents --json` 轮询）→ MCP 装载（chrome-devtools/tesseract-ocr/ai-vision/web-reader/context7）→ 配置代理命令全套 → `/policy` strict 档审批 → 监控告警。另移交：kill 需进程组（MCP 孙进程继承管道）、出站按页计数熔断。
- **M3（二期）**：媒体收发（ClawBot CDN 加密上传）。

## 开放问题（涉及前先实测，勿凭假设实现）

TRD §11 登记的未决项：`/init` 在 headless 下的确切行为、bypass 档下 `permissions.deny` 是否生效、微信文本单条长度上限（分页阈值依据）、OCR MCP 选型、Claude Code 版本漂移（对策：固定版本 + 升级前跑 E2E 回归）。
