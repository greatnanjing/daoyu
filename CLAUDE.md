# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

**刀鱼 (daoyu)**：把微信变成 Claude Code 的遥控器。用户在微信发消息 → 转发给服务器上 headless 模式运行的 Claude Code → 回答与执行进度实时回微信。单用户产品（仅作者本人），单台 Linux 服务器部署，systemd 托管。默认工作仓库即本仓库（自举/dogfood）。

## 当前状态

**M3 媒体收发（图片双向）真机验收通过**（2026-08-19，spec §5 五项全过；验收期实测修正：出站 aes_key 形态、bg watcher 三终态、bg 摘除 mcp-config，见 M3 清单），304 个测试全绿（`python -m pytest`）；M2 已实现（2026-08-16）。M3 全部完成、真机验收通过（2026-08-19，余项 B 四项全过：OCR 主链路 / /mcp 列表呈现系统条目 / /mcp off 系统条目拦截 / /bg 回归）：媒体收发（图片双向）+ /mcp 启停与 /config 写入（余项 A，spec 2026-08-19-mcp-config-writable-design）+ OCR MCP（余项 B，spec 2026-08-19-ocr-mcp-design）。设计与实现决策仍以下列文档为准，实现与 TRD 的已知偏差登记在 `docs/superpowers/plans/2026-08-15-m1-mvp.md` Self-Review 节与 `.superpowers/sdd/` 各审查记录：

- [docs/PRD.md](docs/PRD.md) — 产品需求（功能 FR-1~10、非功能需求、里程碑 M1/M2/M3、范围外）
- [docs/TRD.md](docs/TRD.md) — 技术设计（架构、SQLite 数据模型、claude CLI 调用规范、命令路由、安全设计、测试策略）
- [README.md](README.md) — 部署、使用命令表与 M2 边界

**M2 功能清单**（M1 收发/任务池/命令总线/崩溃恢复之上新增）：

- **strict 档审批**：`/policy strict` 后任务带 `--permission-prompt-tool mcp__daoyu__approve`；[worker/approval_mcp.py](worker/approval_mcp.py)（stdio JSON-RPC server，经临时合并 mcp config 由 claude 拉起、任务结束即删）写 approvals 行 + outbox 🔐 推微信；gateway `handle_inbound` 拦截 Y/N 单字 decide（300s 超时 = expired = 拒绝）。
- **`/bg` 长任务**：桥命令建 bg 任务 → runner `claude --bg` 启动分支（bg_id 落盘即回执）→ [worker/pool.py](worker/pool.py) `_bg_watcher` 轮询 `claude agents --json --all` 推进（真机 2.1.233 三终态：`done`=完成/`blocked`=等用户输入，均 fork 取结果推送完结、`failed`=失败重试；条目无输出字段，结果靠 `--fork-session` 回原会话取——直接 `--resume` 被 daemon 持有拒绝且 rc=0；消失取消）；`/cancel` 走 `claude stop`。
- **MCP 装载**：`claude/mcp.json` 已装 chrome-devtools / context7 / web-reader 三台（平台无关形态——command 直写 npx/uvx，runner 合并层 Windows 包 cmd /c（白名单 {npx,uvx}）+ 过滤 disabled；Linux 侧 chrome-devtools 由 `inject_linux_chrome` 按约定路径注入 headless Chrome 装配——`~/.cache/puppeteer/chrome-headless-shell/linux-*`（npmmirror 装，手动拼 URL 会 404 必须走 `@puppeteer/browsers`）+ `~/chrome-libs` 解包 libasound 免 sudo，注入 `--headless --isolated --executablePath` + LD_LIBRARY_PATH + 清死代理；2026-08-19 真机验证 Next.js SPA 完整渲染，未安装 no-op fail-open）；另有 daoyu-ocr 系统条目（RapidOCR 本地 OCR，runner 恒注入、不受 /mcp 启停管辖，余项 B）。
- **配置代理命令**：[gateway/proxy.py](gateway/proxy.py) — `/permissions`（列表 + deny add/del + allow add，写 `claude/settings.json`）、`/mcp`（列表 + on/off 启停，写 mcp.json 顶层 disabled）、`/config`（概览 + set 七键白名单写 gateway/config.json，重启生效）。
- **同目录多话题**：sessions 表 `UNIQUE(wechat_user, cwd, claude_uuid)`（ensure_schema 对旧表做无损迁移：建 v2 → 搬行 → 换名，幂等）。`/new` 当前目录开新话题；`/sessions` 两级展示（目录分组 + 组内全局序号，序号按 last_active_at DESC）；`/cd #n` 切话题、`/cd <路径>` 切目录（指向该目录最新话题，无则建）；当前话题指针在 state KV `active_session:<wechat_user>`（[common/db.py](common/db.py) `get_active_binding`，chat/policy/bg/cancel 均走它；老库无指针时经旧 `cwd:` 指针回退并回写）。`/policy` 每话题独立。
- **`/sessions`**：会话列表（目录 + 最近任务摘要 + uuid 短码）与 `/cd #n` 序号切换（见上一条：现为话题两级展示）。
- **`/adopt [uuid前缀]`**：收养终端 TUI 创建的 Claude 会话为当前话题（[gateway/bridge.py](gateway/bridge.py) 扫描 `data/claude-home/projects/*/*.jsonl` 未管理 transcript，mtime 降序无参取最新、≥8 位唯一前缀指定；从 transcript 首段提取 cwd 与首条 prompt 做标签；`db.adopt_session` 建 sessions 行并置 `claude_session_inited:<uuid>`——已存在 uuid 必须走 `--resume`，`--session-id` 会报错）。前提：终端会话用 `CLAUDE_CONFIG_DIR=<repo>/data/claude-home claude` 创建（微信 ↔ 终端交叉接续同一话题的反向通道；宿主 `~/.claude` 会话对 runner 不可见）。同会话并发 `--resume` 冲突硬约束不变——终端仍开着该会话时先退出。
- **监控告警**：死信 / 日限熔断 / 预算耗尽死信 / 连接失效清 token 四处自动推微信 ⚠️（复用出站通道，发全部白名单）。

**M3 功能清单**（媒体收发，图片双向；**真机验收通过 2026-08-19**，spec §5 五项全过。验收期实测修正三处：出站 `media.aes_key` 形态 = base64(hex32 ASCII)（传 base64(raw16B) 微信端空白图）；MCP 工具需 settings allow（acceptEdits 不放行 MCP 工具、headless 无确认通道直接 deny）；bg 摘除 `--mcp-config`（daemon 异步读与临时文件即删竞态，见硬性约束））：

- **入站发图即对话**：[gateway/app.py](gateway/app.py) 遍历 `item_list`（`message_type==1` 不变，图片 `type==2`）→ [gateway/media.py](gateway/media.py) CDN 下载 + AES-128-ECB 解密（aeskey 双形态：`image_item.aeskey` hex 优先 / `media.aes_key` base64）→ 随机名落盘 `data/media/inbound/`（magic bytes 白名单 PNG/JPEG/GIF/WebP、20MB 上限）→ 纯图建 chat 任务（prompt 模板"[用户发来图片，已保存到 {p}，请查看并回应]"）、图文拼 prompt；下载失败 ⚠️ 回执、不建任务。
- **出站 `send_image`**：Claude 调 MCP 工具 `send_image(path, caption)`（[worker/approval_mcp.py](worker/approval_mcp.py) 现为 daoyu 统一 stdio server，`DAOYU_TOOLS` 装配：strict="approve,send_image"、其余档="send_image"；经临时合并 mcp config **-p 四档恒装配**（`/bg` 不带，见硬性约束）；工具本身需 `claude/settings.json` allow `mcp__daoyu__send_image`）→ 校验复制到 `data/media/outbound/` → 写 outbox `kind=image` 行（与 [common/db.py](common/db.py) 的 `db.enqueue_media` 同构的裸 SQL，跨进程写入）→ 出站协程整链路现做（getuploadurl → CDN 密文 POST 取 `x-encrypted-param` → caption 文本条 → 图片条），失败整行重试（不缓存 downloadParam）。协议细节见 [docs/superpowers/specs/2026-08-19-m3-media-design.md](docs/superpowers/specs/2026-08-19-m3-media-design.md) §2。
- **schema**：messages 加 `media_path`、outbox 加 `kind` / `media_path` / `caption`（幂等 ALTER），入站图片路径随 messages 行落盘。
- **OCR MCP**：daoyu-ocr 独立 stdio server（[worker/ocr_mcp.py](worker/ocr_mcp.py)，工具 ocr(path)——本地 RapidOCR 中英混识、引擎 lazy、PNG/JPEG 白名单、bytes 直传实测 API）；runner 恒注入系统条目（disabled 不管辖）、settings allow `mcp__daoyu-ocr__ocr`；/mcp 列表呈现系统条目行。bg 无 MCP 口径不变。

组件清单（入口文件）：

- **入口**：`daoyu` console script → [gateway/app.py](gateway/app.py) `start()`（读 `gateway/config.json` + `claude/secrets.env`，崩溃恢复后常驻 poll / outbound / reconnect / worker-pool 四协程）；`daoyu-login` → [gateway/login.py](gateway/login.py)（终端扫码，token 写 DB state 后退出）。
- **gateway**：[gateway/ilink.py](gateway/ilink.py)（iLink 协议封装）、[gateway/router.py](gateway/router.py)（命令总线路由）、[gateway/bridge.py](gateway/bridge.py)（桥命令 + /help 多层合并）、[gateway/proxy.py](gateway/proxy.py)（TUI 配置命令微信代理）、[gateway/outbound.py](gateway/outbound.py)（outbox 投递/重试/死信/节流/typing + 图片 CDN 上传链路）、[gateway/reconnect.py](gateway/reconnect.py)（24h 连接过期守护）、[gateway/media.py](gateway/media.py)（媒体 CDN AES-128-ECB 上传/下载/解密）。
- **worker**：[worker/pool.py](worker/pool.py)（按 session 串行调度池 + bg 后台监视 watcher）、[worker/cli_builder.py](worker/cli_builder.py)（claude argv 组装）、[worker/runner.py](worker/runner.py)（子进程执行/流式进度/费用记账/bg 启动分支）、[worker/stream.py](worker/stream.py)（stream-json 解析 + 节流器）、[worker/approval_mcp.py](worker/approval_mcp.py)（daoyu MCP server：审批 approve + 发图 send_image）、[worker/ocr_mcp.py](worker/ocr_mcp.py)（daoyu-ocr：本地 OCR）。
- **common**：[common/db.py](common/db.py)（SQLite 五表 + approvals + state KV；M3 加 messages.media_path 与 outbox.kind/media_path/caption）、[common/config.py](common/config.py)（配置加载契约）、[common/models.py](common/models.py)、[common/text.py](common/text.py)（长文本分页）。
- **配置**：`gateway/config.example.json`（实例 config.json 进 gitignore）；`claude/settings.json` + `claude/mcp.json`（进 git，宿主隔离靠 CLAUDE_CONFIG_DIR，见硬性约束）；`claude/secrets.env`（gitignore）；`deploy/daoyu.service`（systemd 单元）。

## 常用命令

```bash
python -m pytest                        # 全量测试（290 个）
python -m pytest tests/test_e2e.py -v   # E2E（fake iLink + fake claude 子进程；M2 含审批往返/bg 冒烟；M3 媒体 E2E 在 tests/test_media_e2e.py）
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
- **strict 审批 flag 语义**：strict = `--permission-mode default` + `--permission-prompt-tool mcp__daoyu__approve`（实测 acceptEdits 下不触发 prompt-tool、default 才触发，TRD §4.1 "strict=acceptEdits" 假设已被实测推翻）；审批 server 条目经**临时合并 mcp config**（静态 mcp.json + daoyu 条目，含任务级 env，`daoyu-mcp-` 前缀）传入，任务结束（成功/失败/取消）即删、启动时清扫 kill 残留。server 键 `daoyu` 与工具引用必须严格一致（不一致 = Claude 找不到审批工具 = 该次工具调用被 deny，fail-safe）。**审批工具的返回必须是 behavior JSON**（`{"behavior":"allow","updatedInput":{...}}` / `{"behavior":"deny","message":...}`）——纯文本会被 claude 判 invalid permission result，决策从未生效。
- **`--bg` flag 集（真机实测 2026-08-19）**：`--bare` + 预算 + `--permission-mode` + `--settings`（硬 deny 清单与 `-p` 一致生效）+ bypass 档 `--disallowedTools`（与 `-p` 同源常量）；不传 `--permission-prompt-tool`——strict 档 `/bg` 在 default 模式下需审批的工具（Bash/写文件）被直接拒绝（fail-safe，仅适合只读任务），回执/文档已如实明示；prompt 以 `-` 开头时前置空格防 flag 解析。**不传 `--mcp-config`**：daemon 异步拉起 worker（客户端返回 ~1s 后才读 mcp config），临时文件在 run() 返回即删 → daemon "exit 1 before init" 100% 复现；bg 会话因此无 MCP 工具（send_image 不可用），回执明示。
- **bg 三终态与取结果（真机实测 2.1.233）**：`claude agents --json --all` 条目终态 `done`/`blocked`/`failed`（默认过滤 failed，**必须带 `--all`**）；done 条目十字段（pid/id/cwd/kind/startedAt/sessionId/name/status/state）**无输出/cost 字段** → 取结果靠回原会话（`--fork-session`，直接 `--resume` 被 daemon 持有拒绝且 **rc=0**、错误只在输出——静默空结果）；`blocked` = 会话等用户后续输入（Claude 结尾反问是常态），bg 无输入通道即永久挂起 → 首次观察即 fork 取结果完结。
- **长任务必须走 `claude --bg` + `claude agents --json` 轮询**（后台任务管理是 `claude agents`；停止是 `claude stop <id>`；`claude logs <id>` 实测 2.1.233 存在——TUI 流含 ANSI 转义、人读可但不宜程序解析）：`-p` 结束 5s 会杀后台 bash，subagent 默认上限 10min。
- **`context_token` 只使用当前会话最新入站消息的**，绝不复用历史值（复用旧 token 会 HTTP 200 但静默不投递）。
- **入站按 `msg_id` 幂等去重**（iLink 重连后消息会重投）；出站走 outbox 发件箱，失败重试，至少 5 次后才进死信并告警。
- **宿主配置隔离靠 `CLAUDE_CONFIG_DIR`（机制化）**：实测 `--bare`/`--settings` 均不能隔离宿主 `~/.claude`（宿主 defaultMode/allow/trustAllFiles/插件全部穿透生效，直接架空 strict 审批与硬 deny 清单）；runner 与 pool 给每个 claude 子进程注入 `CLAUDE_CONFIG_DIR=<repo>/data/claude-home/`（调用即 mkdir）。**`-p` 路径已不带 `--bare`**（2026-08-19 实测：`--bare` 剥离 WebFetch/WebSearch/Write/Glob/Grep 全部扩展工具只留 Bash/Edit/Read+MCP；去掉后 WebSearch 经智谱端点适配 `web_search_prime` 完全可用、真机查证带 Sources 验证通过，WebFetch 因抓取前的 claude.ai 域名验证国内不可达而失败但模型会 fallback 到 web-reader MCP；bg 分支保守集保留 `--bare`）。凭据不受影响：仍经 secrets env 注入（ANTHROPIC_API_KEY 等）；MCP 清单经 `--mcp-config` 显式传。刀鱼持久配置在 `claude/settings.json` 与 `claude/mcp.json`（进 git），代理命令（/permissions /config /mcp）改的就是这些文件。
- **媒体出站走 outbox kind=image 行**：投递时整链路现做（上传→caption→图），
  失败整行重试（不缓存 downloadParam）；caption 与图分两条 sendmessage（官方模式）。
  MCP server 键 `daoyu` 统一装配（approve 仅 strict + send_image 四档，`DAOYU_TOOLS`）。
- **入站图片消息 `message_type==1` 不变、`item_list[].type==2`**；aeskey 双形态
  （`image_item.aeskey` hex 优先 / `media.aes_key` base64）；magic bytes 白名单 +
  20MB 上限，随机名落盘 `data/media/inbound|outbound/`。

## 统一命令总线（产品核心）

微信命令与 Claude Code CLI **同一套语法、同一个命名空间**，不发明第二套命令体系。路由顺序：

1. **桥命令**（`/cancel` `/tasks` `/status` `/cd` `/sessions` `/policy` `/bg` `/new` `/adopt`）→ gateway 本地执行，秒回。另有 iLink 运维命令 `/time` `/重新连接`（管连接本身，与 Claude 无关）。
2. **代理**：TUI 交互专属命令（静态维护清单：/permissions /hooks /plugins /login /config /mcp /vim /terminal-setup）→ 拦截后以相同命令名与参数格式操作同一底层配置，输出文字版。已实现 /permissions（读写）、/mcp（列表 + on/off 启停）、/config（概览 + set 白名单键）；其余提示暂未提供。**代理先于转发判定**——实测 init `slash_commands` 含 `config`/`mcp`，若转发优先会把代理命令截走原样发给 headless claude。
3. **转发**：headless 可用命令集（启动时从 `system/init` 事件的 `slash_commands` 同步）→ 原样作为 prompt 传给 claude。
4. 都不是 → 未知命令提示 + 最接近命令建议。

`/help` 由桥/运维/代理（已实现项）/转发多层合并动态生成，永远与实际能力一致。

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
- `/policy` 四档：auto（默认全放）/ strict（default + 审批 MCP：需批准的工具调用推微信 Y/N，5 分钟超时视为拒绝；`/bg` 任务无审批通道、需审批工具被直接拒绝，仅适合只读）/ bypass / plan。
- secret 只放 `claude/secrets.env`（gitignore）+ 环境变量注入，日志脱敏。
- gateway 仅响应白名单微信账号，白名单外一律不响应。

## 实现顺序（勿颠倒依赖）

- **M1（MVP）✅**：SQLite schema → gateway 收发+落盘去重 → worker 调 `claude -p`（会话绑定、stream 解析、节流推送）→ 命令总线 → 崩溃恢复 → E2E。
- **M2 ✅**：审批（`--permission-prompt-tool` **实测在 2.1.233 仍存在可用**——注意 `--help` 不列全 flag，勿以 help 缺失判断移除）→ `--bg` 长任务（启动 `claude --bg "<prompt>"` 返回任务 id；轮询 `claude agents --json`；停止 `claude stop <id>`；`claude logs` 是 TUI 流不可解析）→ MCP 装载（chrome-devtools/context7/web-reader；tesseract-ocr/ai-vision 推迟 M3）→ 配置代理命令全套（/permissions 读写、/mcp 列表+启停、/config 概览+set）→ `/policy` strict 档审批 → `/sessions` → 监控告警。已移交：kill 需进程组（MCP 孙进程继承管道）、出站按页计数熔断。
- **M3 ✅**：媒体收发（图片双向，CDN AES-128-ECB）代码完成并**真机验收通过**（2026-08-19，spec §5 五项全过；协议源 @tencent-weixin/openclaw-weixin v2.4.6 dist）；验收期修正：出站 aes_key 形态、bg watcher 三终态、bg 摘除 mcp-config、resume 恒 fork、取结果 prompt 逐项列出。余项 A（/mcp 启停 + /config 写入）与余项 B（daoyu-ocr）均已完成（2026-08-19），M3 闭环。

## 开放问题（涉及前先实测，勿凭假设实现）

TRD §11 登记的未决项：`/init` 在 headless 下的确切行为、bypass 档下 `permissions.deny` 是否生效、微信文本单条长度上限（分页阈值依据）、Claude Code 版本漂移（对策：固定版本 + 升级前跑 E2E 回归）。M2 新登记的 bg 三项已随 M3 验收（2026-08-19）全部落定：

- ~~**bg completed 条目字段名未采样**~~ → 已采样：终态值是 `done`（非 completed）、条目十字段无输出/cost 字段，取结果靠 `--fork-session` 回原会话（常态路径而非兜底）。
- ~~**`--bg` 与 `--permission-prompt-tool` 组合未实测**~~ → 已落定：bg 不传审批工具（strict 档回执明示）；`--settings` 硬 deny 与 acceptEdits 下 Bash 正常放行均实证；**`--mcp-config` 与 `--bg` 结构性不兼容**（daemon 异步读竞态，已摘除，bg 无 MCP）。
- ~~**bg 停机竞态**~~ → 已落定：按"先落终态者胜"处理 cancel/watcher 双向，真机验收通过（`/cancel` 与 watcher 完结均正常）。
