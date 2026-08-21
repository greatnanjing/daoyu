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

前提：Python ≥ 3.11；claude CLI 已安装且在 PATH（`claude --version` 可用）；微信账号用于扫码。巡检采样依赖 psutil（pyproject dependencies 已含 `psutil>=5.9`，`pip install -e .` 自动安装，无需单独装）。

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
#    填 ANTHROPIC_API_KEY（兜底层）。凭据/模型映射会动态跟随宿主
#    ~/.claude/settings.json 的 env 块（ANTHROPIC_* 逐键优先，secrets.env 兜底）
#    ——在宿主侧换 key/改模型，刀鱼每任务现场跟随，无需改这里。

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

**CLI 版本探测**：启动时自动跑 `claude --version` 与 `worker/version.py` 的 `EXPECTED_CLAUDE_VERSION`（当前 2.1.233，生产服务器 实测基线）比对——匹配记 audit、漂移/失败 audit+warning（fail-open 不阻断启动；flag/输出形态的实测假设随版本漂移失真，见各代码注释的版本锚点）。升级流程：改常量 → `python -m pytest` 全量回归 → 服务器 `npm i -g @anthropic-ai/claude-code@<版本>`。

**MCP 冷缓存预热**：Linux 首次调用时 npx/uvx 要现下载包（分钟级，期间 Claude 可能等不到 server 就绪）。部署后先手动各跑一次、等下载完成再 Ctrl+C 中断，即可把包缓存好：

```bash
npx chrome-devtools-mcp@latest --help
npx -y @upstash/context7-mcp
uvx --with "mcp~=1.0" mcp-server-fetch
npx -y @playwright/mcp@0.0.79 --headless --help
```

**Linux headless Chrome 装配**（chrome-devtools 与 playwright 共用，2026-08-19/20 实证）：runner 的 `inject_linux_chrome` / `inject_linux_playwright` 按约定路径自动注入 `--headless --executablePath|--executable-path` + `LD_LIBRARY_PATH`，装好即生效、未装则 no-op。playwright 复用同一 chrome-headless-shell 二进制（兼容性未真机证实时兜底：`PLAYWRIGHT_DOWNLOAD_HOST=<npmmirror 镜像> npx playwright install chromium` 自装并去掉注入）。手动装法（必须走 `@puppeteer/browsers`，直拼 URL 会 404；版本号查同镜像 `last-known-good-versions.json`）：

```bash
npx -y @puppeteer/browsers install chrome-headless-shell@<版本> \
    --path ~/.cache/puppeteer \
    --base-url https://cdn.npmmirror.com/binaries/chrome-for-testing
# 唯一缺失的系统库 libasound.so.2：rpm 解包免 sudo（路径换实际发行版镜像）
mkdir -p ~/chrome-libs && cd ~/chrome-libs
curl -sLO <镜像>/alsa-lib-<版本>.x86_64.rpm && rpm2cpio *.rpm | cpio -idmu
# runner 按 ~/chrome-libs/usr/lib64 自动注入 LD_LIBRARY_PATH
```

**中文字体（截图含中文时必装，否则豆腐块）**：服务器默认无 CJK 字体（`fc-list :lang=zh` 为空）。免 sudo 装法（yumdownloader 也要先 `unset` 死代理）：

```bash
unset http_proxy https_proxy
yumdownloader --destdir=/tmp google-noto-sans-cjk-ttc-fonts
mkdir -p ~/.local/share/fonts && cd ~/.local/share/fonts
rpm2cpio /tmp/google-noto-sans-cjk-ttc-fonts-*.rpm | cpio -idmu --quiet
mv usr/share/fonts/google-noto-cjk/NotoSansCJK-Regular.ttc . && rm -rf usr
fc-cache -f ~/.local/share/fonts && fc-list :lang=zh   # 应列出 Noto Sans CJK
```

`gateway/config.json` 主要键：

| 键 | 说明 |
|---|---|
| `whitelist` | 允许响应的微信 user id 列表，白名单外一律不响应 |
| `default_cwd` | 初始工作目录（也是默认 Claude 会话绑定的仓库） |
| `claude_bin` | claude 可执行文件（字符串或 argv 前缀列表）。Windows 下建议直接指向 npm shim 内的真实 `claude.exe`（`.cmd` 含空格路径有 cmd /c 剥引号坑，版本探测会自动解析 shim 但 spawn 不会） |
| `throttle` | 节流：最小发送间隔 / 进度窗口 / 单条分页字符上限 / 每日发送上限（**按实际发送页数计**、跨重启不清零——按已送达 outbox 行折算恢复）。分页另有字节硬闸（微信单条 16384 字节实测上限，超限 `errcode=0` 静默丢），`page_char_limit` 怎么调都安全 |
| `media_retention_days` | `data/media` 保留天数（默认 14；启动与日界时按三分规则清理：outbound/ 全量、inbound/ 按 img-/file-/voice-/vid- 前缀、根目录仅图片类——claude 写的非媒体文件不碰；未终态 outbox 行引用的受保护；0 = 关闭） |
| `budget` | 预算闸：`max_turns` + `max_usd`，与权限档位独立、恒生效 |
| `worker` | 任务池并发数与轮询间隔 |
| `reconnect` | 连接守护参数（`session_duration_s` 默认 30 天——token 长效实证；token 真失效时 401 自动触发重扫；`silent_grace_s`：重连先静默尝试再推二维码） |
| `cron` | M4 主动服务阈值（磁盘/CPU/内存阈值、持续分钟、证书预警天数与路径、告警静默小时、积压预警）；除 `cert_paths` 外数值键可经 `/config set` 改（重启生效） |

## 日常使用（微信里发）

| 类别 | 命令 | 说明 |
|---|---|---|
| 桥命令（本地秒回） | `/tasks` | 查看 running/pending 任务（后台任务带 `[bg]` 标记） |
| | `/status` | 队列深度、死信数、当日费用、今日已发送条数、连接剩余时间 |
| | `/cancel <任务号>` | 取消任务（无参 = 当前会话最新运行中任务；后台任务走 `claude stop`） |
| | `/bg <任务描述>` | 转入后台长任务（`claude --bg`）：秒回执，完成后自动分页推送结果 |
| | `/cd <目录\|#序号>` | 切目录（指向该目录最新话题，无则自动建）或按 `/sessions` 全局序号切话题；无参查看当前目录话题 |
| | `/new` | 在当前目录开新话题（新 Claude 会话，上下文从零开始） |
| | `/adopt [uuid前缀]` | 收养终端里创建的 Claude 会话为当前话题（无参 = 最新一个；终端会话用 `deploy/daoyu-tui.sh` 创建，微信 ↔ 终端 TUI 由此可交叉接续同一话题） |
| | `/sessions` | 按目录两级列出全部话题（全局序号 + ▶ 当前 + 最近任务摘要 + 活跃时间 + uuid 短码），`/cd #n` 切换 |
| | `/delete #<序号\|task 任务号>` | 删话题（连同其任务）或单删任务记录；均需回 `Y` 确认，当前话题/运行中任务拒删 |
| | `/policy <auto\|strict\|bypass\|plan>` | 查看或切换当前话题的权限档位（每话题独立） |
| | `/cron` | 定时任务管理：日报/巡检 开关、`time daily <HH:MM>`、`interval patrol <分钟>` |
| | `/alias add <名> <内容> \| del <名> \| list` | 自定义快捷命令（内置 `/t`=`/tasks`、`/s`=`/status`、`/c`=`/cancel`、`/cs`=`/sessions`） |
| 配置代理（改刀鱼专属配置，效果同 TUI） | `/permissions` | 查看 deny/allow/ask 列表；`/permissions deny add <规则>`、`/permissions deny del <序号>`、`/permissions allow add <规则>` 读写 `claude/settings.json` |
| | `/mcp` | 列出 `claude/mcp.json` 已装 MCP server（✅/⛔ 状态）；`/mcp off|on <序号|名字>` 启停（下一任务生效，停用不丢配置） |
| | `/config` | 查看 gateway 配置概要（节流/预算/白名单数，secret 只计个数不回显）；`set <键> <值>` 改白名单键（throttle/budget/worker.concurrency + cron 阈值七键，含 `throttle.merge_window_s`、`throttle.md_clean`，共 16 键，重启生效） |
| iLink 运维 | `/help` | 帮助：功能概述 + 分组命令清单 + 微信 ↔ 电脑接续指南（按实际能力动态生成） |
| | `/time` | 连接剩余时间 |
| | `/重新连接` | 立即重连（静默优先免扫码，需扫码时推二维码；Y/N 确认） |
| 转发 | `/review`、`/compact` 等 | Claude Code headless 可用的斜杠命令原样转发执行（可用集从 `system/init` 事件同步缓存） |
| 对话 | 任意文本 | 直接作为 prompt 发给当前会话的 Claude |

典型流程：发「你好」→ 秒回「✅ 收到，正在合并后续消息（2s 内无新增即开始处理）」→ 工具执行时推送「🔧 工具名」进度 → 最终回复（超长自动分页）。

**连发消息自动合并（M5C1）**：连发几条纯文本会合并成**一个 prompt**（Claude 一轮看全上下文，而非逐段各答）——首条即时 ACK「正在合并」、窗口内（默认 2s）追加的消息静默、到点「已合并 N 条，开始处理」。语音转写文字同样合并。`/config set throttle.merge_window_s <秒>` 可调（0 = 禁用，退回逐条即建任务）。任务排队时 ACK 显示「排在第 M 位（当前任务完成后接上）」——追加输入会作为下一轮接上（`claude -p` stdin 一次性关闭，无法中途注入运行中回合，诚实告知队列位次）。

**出站 Markdown 清洗（M5C2，默认关闭）**：Claude 的回复常带 Markdown（标题/粗体/代码围栏/表格）。2026-08-21 真机实测发现**微信新版手机+PC 双端原生渲染 Markdown**（`##`→黑体标题、`**`→粗体、表格可读、代码块吞围栏保留内容）——故默认**原文直发**享受原生渲染；`throttle.md_clean` 开关默认 false。老客户端/某类消息渲染异常时可 `/config set throttle.md_clean true` 开启清洗（投递前转写为纯文本可读形态：标题转【】、围栏代码缩进块、表格转竖排列表等；outbox 恒存**原文**，清洗只在发送侧、幂等可重放），重启生效。

### strict 档审批（M2）

发 `/policy strict` 后，Claude 遇到需要批准的工具调用时会推微信：

```
🔐 审批请求 #3：允许执行 Bash？
{"command":"rm -rf /tmp/x"}
回复 Y 允许 / N 拒绝
```

回 **Y** 允许（Claude 继续执行）、回 **N** 拒绝（Claude 收到拒绝后自行调整）；**5 分钟不回自动拒绝**（fail-safe）。一次只审最早的一条（超过 5.5 分钟的陈旧请求不再劫持回复），其余文本不拦截、照常当聊天处理。注意：`/bg` 后台任务不走微信审批（`--permission-prompt-tool` 与 `--bg` 的组合已真机实测落定：bg 不传审批工具）；strict 档下后台任务的需审批工具会被直接拒绝（仅适合只读任务，详见下文边界）。

### 监控告警（M2）

以下异常自动推微信 ⚠️（发全部白名单账号，复用出站通道）：出站死信（重试 ≥5 次仍失败）、日发送上限熔断、任务预算/回合耗尽死信、微信连接失效（连续 401/403，自动重连）。

### 通知通道（M5A：事件接入）

外部事件经刀鱼出站通道推微信（🔔 纯通知，不进对话流）。四个入口：

**1. 命令行**（脚本收尾 / cron / SSH 远程）：

```bash
daoyu-notify "备份完成" "耗时 3 分钟"
longjob && daoyu-notify "任务成功" || daoyu-notify "任务失败"
```

**2. 终端 Claude Code 会话**（hooks，零代码）：把
[deploy/notify-hooks.example.json](deploy/notify-hooks.example.json) 的 `hooks`
节合并进服务器 **`data/claude-home/settings.json`**（TUI 经
`deploy/daoyu-tui.sh` 启动、CLAUDE_CONFIG_DIR 指此目录——2026-08-21 真机落定：
宿主 `~/.claude/settings.json` 不被 TUI 读取，hooks 必须放 claude-home）——
终端 TUI 每轮回复结束（Stop）
推 ✅、等待输入/权限确认（Notification）推 ❓。注意 Stop 是**每轮回复结束都
触发**（多轮对话的中间轮次也各推一条，并非仅任务完成），消息较密——嫌吵或
怕刷爆日限熔断可只合并 Notification 节、或自行限频。示例命令为绝对路径形态
（按本文部署约定 `/home/<user>/proj/daoyu/.venv/bin/daoyu-notify`），合并前
按实际安装位置调整。

**3. headless 任务中**（MCP 工具）：Claude 可调 `notify(title, body)` 推送
阶段性通知（后台任务无 MCP，不可用——同 send_image）。

**4. HTTP**（本机其他系统）：

```bash
curl -X POST http://127.0.0.1:8417/notify \
     -H 'Content-Type: application/json' \
     -d '{"title": "构建完成", "body": "全部通过"}'
# secrets.env 设 notify_token 时再加 -H 'Authorization: Bearer <token>'
```

注意：通知与对话共用出站通道与日限熔断——外部源高频推送会触发熔断暂停全部
出站（含对话回复），接入方自行限频。升级部署需 `pip install -e .` 重装（获得
daoyu-notify 命令）。

### 微信 ↔ 终端 TUI 交叉接续同一话题

微信与服务器终端可以交替续写**同一个** Claude 话题：

```bash
# 终端侧：一键入口（清死代理 + 注入凭据 + 指向刀鱼隔离配置目录）
deploy/daoyu-tui.sh          # 聊完 /exit 退出
# 自检（不启动 claude，只打印解析后的环境）：DAOYU_TUI_DRYRUN=1 deploy/daoyu-tui.sh
```

```
微信侧：/adopt            ← 收养刚退出的终端会话为当前话题（无参取最新）
        继续发消息          ← 终端里聊的上下文都在
```

反方向同理：微信聊到一半，终端 `deploy/daoyu-tui.sh` 进 TUI 用 `claude --resume` 选同一会话接着聊。约束：同一话题不能两端同时开着（并发 `--resume` 冲突），终端先退出再在微信发消息。

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
python -m pytest                        # 全量测试（489 个）
python -m pytest tests/test_e2e.py -v   # E2E：fake iLink + fake claude 子进程全链路
python -m gateway.app                   # 前台调试运行（不进 systemd）
```

```
├── gateway/   # app 入口 / ilink 协议 / router 命令路由 / bridge 桥命令 /
│              # proxy 配置代理命令 / outbound 出站节流重试 / media 媒体 CDN AES 上传下载解密（图片+文件/语音/视频） /
│              # reconnect 连接守护（被动重连）/ login 扫码 / scheduler 定时日报+巡检 /
│              # notify_http 通知 HTTP 入口 / notify_cli 通知命令行
├── worker/    # pool 会话串行调度+bg 后台监视 / cli_builder argv 组装 / runner 子进程执行 /
│              # stream 解析 / approval_mcp daoyu MCP server（审批+发图+发文件+发通知，stdio） / ocr_mcp 本地 OCR（daoyu-ocr）
├── common/    # db（SQLite 五表+approvals+state KV）/ config / models / text（分页）/ notify（通知入队）
├── claude/    # settings.json + mcp.json（进 git）、secrets.env（gitignore）
├── tests/     # 单测 + E2E（fixtures/ 模拟 claude 子进程：-p 流回放与 --bg 两种形态）
├── deploy/    # daoyu.service（systemd 单元）
└── docs/      # PRD / TRD
```

## M2 边界（当前版本不包含，勿过度期待）

- **strict 档 `/bg` 不走审批且更严**：`--bg` 不传审批工具；strict 档权限模式为 default——后台任务中需审批的工具（Bash/写文件）会被**直接拒绝**（fail-safe），仅适合只读任务。deny 清单经 `--settings` 照常生效（与 `-p` 一致，真机已验），回执会明示。长任务要审批就先 `-p` 同步跑，或切 auto/bypass 档再 `/bg`。
- **`/bg` 不装载 MCP 工具**（真机实证）：`--mcp-config` 与 `--bg` 结构性不兼容（后台 daemon 异步读配置与临时文件即删竞态），已摘除——后台任务无 `send_image` 等 MCP 能力，需要时同步跑；回执明示。
- **bypass 档 `/bg` 带 `--disallowedTools` 工具级兜底**（与 `-p` 同源常量；`--bg` 下 acceptEdits 与 Bash 正常放行已真机实证）。
- **OCR**：daoyu-ocr（RapidOCR 本地封装，中英混识）随任务恒装载（系统条目，不受 /mcp 启停管辖）；视觉理解由 Claude 模型原生视觉承担（Read 看图）。静态三台 chrome-devtools / context7 / web-reader 可经 /mcp 启停。
- **`/mcp`、`/config`**：/mcp 列表 + on/off 启停（下一任务生效，停用不丢配置）；/config 概览 + set 改常用键（throttle/budget/concurrency，M4 起 cron 阈值七键并入白名单，重启生效）。whitelist 等不开放，改 gateway/config.json。
- **语音/视频出站专用条**：不做——音频/视频文件经 `send_file` 走文件条/视频条（官方同款模式，见下节）；语音 SILK 解码亦不做（转写缺失时存档 + 回执兜底）。

## M3+M5B 媒体收发（图片双向真机验收 2026-08-19；文件/语音/视频真机验收 2026-08-21）

- **发图即对话**（M3）：微信里直接发图片即进入当前对话——刀鱼从 CDN 下载解密落盘后转成 prompt（"[用户发来图片，已保存到 …，请查看并回应]"）发给当前会话的 Claude；图文混发拼接为同一条 prompt。下载失败回 ⚠️ 提示、不建任务。
- **Claude 回图**（M3）：Claude 调 MCP 工具 `send_image(path, caption)` 把图片经 CDN 加密上传发回微信（caption 作为单独文本条先发）；工具 `-p` 四档恒装配（`/bg` 不带，见上），图片须为 PNG/JPEG/GIF/WebP 且 ≤20MB。
- **发文件即对话**（M5B）：微信发任意文件（≤100MB）同样下载落盘建任务，prompt 带原始文件名与大小（"[用户发来文件 报表.xlsx（0.0MB），已保存到 …，请查看处理]"），Claude 可直接读文件处理。
- **Claude 回文件 `send_file(path, caption)`**（M5B）：按扩展名三路由——图片扩展名转 `send_image` 原图发送；视频扩展名（mp4/mov/webm/mkv/avi）发视频条（微信端可直接播放）；其余发文件条（保留原文件名，微信端可下载）。≤100MB；工具 `-p` 四档恒装配（`/bg` 不带，见上）。
- **语音入站**（M5B）：服务端转写非空时，转写文字直接当用户消息对话（零解码成本）；无转写时下载存档并回 ⚠️ 提示补发文字（Claude 解不了 SILK，不建任务）。
- **视频入站**（M5B）：下载存盘（.mp4）建任务，prompt 提示可用 ffmpeg 抽帧查看内容（服务器未装 ffmpeg 则如实告知）。
