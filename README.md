# 刀鱼 (daoyu)

把微信变成 Claude Code 的遥控器：在微信里发消息 → 服务器上 headless 模式运行的 Claude Code 执行 → 回答与实时进度回微信。单用户产品，单台 Linux 服务器部署，systemd 托管。

设计文档：[docs/PRD.md](docs/PRD.md)（产品需求）｜[docs/TRD.md](docs/TRD.md)（技术设计）

## 架构

三组件 + 一条 SQLite 持久化脊柱，gateway 与 worker 同进程：

```
      微信 ⇄ iLink 长轮询
             │
  ┌──────────┴────────────┐   SQLite（WAL）唯一事实源 data/daoyu.db
  │ gateway   asyncio 收发 │◄─► messages / tasks / outbox / sessions /
  │ 入站去重→命令路由→出站  │   audit_log + state KV（一切先落盘）
  │ worker    同进程任务池 │
  │ 按 Claude 会话串行     │
  └──────────┬────────────┘
             ▼
     claude -p 子进程（--bare、stream-json、cwd=会话目录）
```

智能本体就是 Claude Code CLI 本身——刀鱼只负责收发、路由、子进程保姆与进度推送；工具、MCP、skills、上下文管理全部由 Claude Code 原生提供。

## 首次部署（Linux 服务器）

前提：Python ≥ 3.11；claude CLI 已安装且在 PATH（`claude --version` 可用）；微信账号用于扫码。

```bash
# 1. clone 到与 deploy/daoyu.service 一致的路径
git clone <repo> /home/<user>/proj/daoyu
cd /home/<user>/proj/daoyu

# 2. venv + 安装（dev=pytest，qr=终端渲染二维码）
python3.11 -m venv .venv
.venv/bin/python -m pip install -e ".[dev,qr]"

# 3. 配置
cp gateway/config.example.json gateway/config.json
#    编辑 whitelist（微信 user id，形如 xxx@im.wechat）与 default_cwd
cp claude/secrets.env.example claude/secrets.env
#    填 ANTHROPIC_API_KEY

# 4. 扫码登录（token 写入 DB 后退出；直接启动 daoyu 也会在无 token 时引导扫码）
.venv/bin/daoyu-login

# 5. systemd 常驻
sudo cp deploy/daoyu.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now daoyu
journalctl -u daoyu -f        # 看到「刀鱼已启动（gateway+worker 同进程）」即成功
```

Windows 开发机（Git Bash）仅 venv 内路径不同：`.venv/Scripts/python`、`.venv/Scripts/daoyu-login`（Linux 生产为 `.venv/bin/…`），其余步骤一致。

`claude/mcp.json`（MCP server 清单，进 git）为平台无关形态（各条目 `command` 直写 `npx` / `uvx`）：Windows 下由 runner 合并层自动包一层 `cmd /c`（白名单 {npx,uvx}），Linux 直传、部署无需手改清单；只需确认已装 Node.js（含 npx）与 [uv](https://docs.astral.sh/uv/)（提供 uvx）。

**MCP 冷缓存预热**：Linux 首次调用时 npx/uvx 要现下载包（分钟级，期间 Claude 可能等不到 server 就绪）。部署后先手动各跑一次、等下载完成再 Ctrl+C 中断，即可把包缓存好：

```bash
npx chrome-devtools-mcp@latest --help
npx -y @upstash/context7-mcp
uvx --with "mcp~=1.0" mcp-server-fetch
```

`gateway/config.json` 主要键：

| 键 | 说明 |
|---|---|
| `whitelist` | 允许响应的微信 user id 列表，白名单外一律不响应 |
| `default_cwd` | 初始工作目录（也是默认 Claude 会话绑定的仓库） |
| `claude_bin` | claude 可执行文件（字符串或 argv 前缀列表） |
| `throttle` | 节流：最小发送间隔 / 进度窗口 / 单条分页字符上限 / 每日发送上限 |
| `budget` | 预算闸：`max_turns` + `max_usd`，与权限档位独立、恒生效 |
| `worker` | 任务池并发数与轮询间隔 |
| `reconnect` | iLink 24h 连接过期的预警/强制重连参数 |

## 日常使用（微信里发）

| 类别 | 命令 | 说明 |
|---|---|---|
| 桥命令（本地秒回） | `/tasks` | 查看 running/pending 任务（后台任务带 `[bg]` 标记） |
| | `/status` | 队列深度、死信数、当日费用、连接剩余时间 |
| | `/cancel <任务号>` | 取消任务（无参 = 当前会话最新运行中任务；后台任务走 `claude stop`） |
| | `/bg <任务描述>` | 转入后台长任务（`claude --bg`）：秒回执，完成后自动分页推送结果 |
| | `/cd <目录\|#序号>` | 切目录（指向该目录最新话题，无则自动建）或按 `/sessions` 全局序号切话题；无参查看当前目录话题 |
| | `/new` | 在当前目录开新话题（新 Claude 会话，上下文从零开始） |
| | `/sessions` | 按目录两级列出全部话题（全局序号 + ▶ 当前 + 最近任务摘要 + 活跃时间），`/cd #n` 切换 |
| | `/policy <auto\|strict\|bypass\|plan>` | 查看或切换当前话题的权限档位（每话题独立） |
| 配置代理（改刀鱼专属配置，效果同 TUI） | `/permissions` | 查看 deny/allow/ask 列表；`/permissions deny add <规则>`、`/permissions deny del <序号>`、`/permissions allow add <规则>` 读写 `claude/settings.json` |
| | `/mcp` | 列出 `claude/mcp.json` 已装 MCP server（✅/⛔ 状态）；`/mcp off|on <序号|名字>` 启停（下一任务生效，停用不丢配置） |
| | `/config` | 查看 gateway 配置概要（节流/预算/白名单数，secret 只计个数不回显）；`set <键> <值>` 改七键白名单（throttle/budget/worker.concurrency，重启生效） |
| iLink 运维 | `/help` | 全部可用命令（按实际能力动态生成） |
| | `/time` | 连接剩余时间 |
| | `/重新连接` | 立即重新扫码连接（Y/N 确认） |
| 转发 | `/review`、`/compact` 等 | Claude Code headless 可用的斜杠命令原样转发执行（可用集从 `system/init` 事件同步缓存） |
| 对话 | 任意文本 | 直接作为 prompt 发给当前会话的 Claude |

典型流程：发「你好」→ 秒回「✅ 收到，处理中」→ 工具执行时推送「🔧 工具名」进度 → 最终回复（超长自动分页）。

### strict 档审批（M2）

发 `/policy strict` 后，Claude 遇到需要批准的工具调用时会推微信：

```
🔐 审批请求 #3：允许执行 Bash？
{"command":"rm -rf /tmp/x"}
回复 Y 允许 / N 拒绝
```

回 **Y** 允许（Claude 继续执行）、回 **N** 拒绝（Claude 收到拒绝后自行调整）；**5 分钟不回自动拒绝**（fail-safe）。一次只审最早的一条（超过 5.5 分钟的陈旧请求不再劫持回复），其余文本不拦截、照常当聊天处理。注意：`/bg` 后台任务不走微信审批（`--bg` 与审批 flag 组合未实测，保守不传）；strict 档下后台任务的需审批工具会被直接拒绝（仅适合只读任务，详见下文边界）。

### 监控告警（M2）

以下异常自动推微信 ⚠️（发全部白名单账号，复用出站通道）：出站死信（重试 ≥5 次仍失败）、日发送上限熔断、任务预算/回合耗尽死信、微信连接失效（连续 401/403，自动重连）。

## 运维

- **状态**：微信发 `/status`（队列、死信、当日费用、连接剩余）。
- **崩溃恢复**：一切先落盘 SQLite。`kill -9` 后 systemd `Restart=always`（5s）自动拉起，running 任务重置为 pending 重跑、未送达消息重新投递、入站按 `message_id` 幂等去重不重复处理。
- **日志**：`journalctl -u daoyu -f`。
- **DB 每日备份**（WAL 下 `.backup` 在线安全，加 crontab -e）：

```cron
17 4 * * * sqlite3 /home/<user>/proj/daoyu/data/daoyu.db ".backup '/home/<user>/proj/daoyu/data/daoyu-$(date +\%F).db'"
```

## 开发

```bash
python -m pytest                        # 全量测试（249 个）
python -m pytest tests/test_e2e.py -v   # E2E：fake iLink + fake claude 子进程全链路
python -m gateway.app                   # 前台调试运行（不进 systemd）
```

```
├── gateway/   # app 入口 / ilink 协议 / router 命令路由 / bridge 桥命令 /
│              # proxy 配置代理命令 / outbound 出站节流重试 / media 媒体 CDN AES 上传下载解密 /
│              # reconnect 24h 连接守护 / login 扫码
├── worker/    # pool 会话串行调度+bg 后台监视 / cli_builder argv 组装 / runner 子进程执行 /
│              # stream 解析 / approval_mcp daoyu MCP server（审批+发图，stdio）
├── common/    # db（SQLite 五表+approvals+state KV）/ config / models / text（分页）
├── claude/    # settings.json + mcp.json（进 git）、secrets.env（gitignore）
├── tests/     # 单测 + E2E（fixtures/ 模拟 claude 子进程：-p 流回放与 --bg 两种形态）
├── deploy/    # daoyu.service（systemd 单元）
└── docs/      # PRD / TRD
```

## M2 边界（当前版本不包含，勿过度期待）

- **strict 档 `/bg` 不走审批且更严**：`--bg` 不传审批工具；strict 档权限模式为 default——后台任务中需审批的工具（Bash/写文件）会被**直接拒绝**（fail-safe），仅适合只读任务。deny 清单经 `--settings` 照常生效（与 `-p` 一致，真机已验），回执会明示。长任务要审批就先 `-p` 同步跑，或切 auto/bypass 档再 `/bg`。
- **`/bg` 不装载 MCP 工具**（真机实证）：`--mcp-config` 与 `--bg` 结构性不兼容（后台 daemon 异步读配置与临时文件即删竞态），已摘除——后台任务无 `send_image` 等 MCP 能力，需要时同步跑；回执明示。
- **bypass 档 `/bg` 带 `--disallowedTools` 工具级兜底**（与 `-p` 同源常量；`--bg` 下 acceptEdits 与 Bash 正常放行已真机实证）。
- **OCR / 视觉 MCP**（tesseract-ocr / ai-vision）：媒体入站打通后 Claude 用 Read 原生看图，按实际体验再评估；当前已装 chrome-devtools / context7 / web-reader 三台。
- **`/mcp`、`/config`**：/mcp 列表 + on/off 启停（下一任务生效，停用不丢配置）；/config 概览 + set 改常用键（throttle/budget/concurrency 七键，重启生效）。whitelist 等不开放，改 gateway/config.json。
- **语音/文件/视频收发**：仍为二期（图片收发 M3 已实现，见下节）。

## M3 媒体收发（图片双向，已真机验收 2026-08-19）

- **发图即对话**：微信里直接发图片即进入当前对话——刀鱼从 CDN 下载解密落盘后转成 prompt（"[用户发来图片，已保存到 …，请查看并回应]"）发给当前会话的 Claude；图文混发拼接为同一条 prompt。下载失败回 ⚠️ 提示、不建任务。
- **Claude 回图**：Claude 调 MCP 工具 `send_image(path, caption)` 把图片经 CDN 加密上传发回微信（caption 作为单独文本条先发）；工具 `-p` 四档恒装配（`/bg` 不带，见上），图片须为 PNG/JPEG/GIF/WebP 且 ≤20MB。
