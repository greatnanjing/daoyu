# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

**刀鱼 (daoyu)**：把微信变成 Claude Code 的遥控器。用户在微信发消息 → 转发给服务器上 headless 模式运行的 Claude Code → 回答与执行进度实时回微信。单用户产品（仅作者本人），单台 Linux 服务器部署，systemd 托管。默认工作仓库即本仓库（自举/dogfood）。

> ⚠️ **图片回传铁律（最高优先级）**：任何截图 / 生成图片的操作（`take_screenshot` / `browser_take_screenshot` / `browser_run_code_unsafe` 出图 / Write 落盘 png/jpg 等），**紧接着必须调用 `mcp__daoyu__send_image(path, caption)` 把原图回传微信**——否则用户在微信端收不到图（存盘文件用户看不到）。这是硬性要求，不是建议。详见下方「图片回传约定」节。

## 当前状态

**M4 主动服务（定时日报 + 巡检告警）已实现并真机验收（2026-08-21，383 个测试全绿；真机闭环：日报首推即达、异常升级→分析→结论推送全链路三证——outbox 393/409-411；首 bug ops 话题中断重入已修 9bc627f）**——scheduler 第五常驻协程、/cron 命令族、日报模板+异常升 Claude、巡检四项+静默期、ops 话题（见下方 M4 清单）。**M5A 通知通道（事件接入）已实现（2026-08-21，400 个测试全绿；真机验收通过）**——CLI daoyu-notify（--hook 模式）/ MCP notify（四档恒装、任务属主定向）/ HTTP 127.0.0.1:8417（notify 配置节）/ 终端 hooks 零代码（deploy 示例），统一 common/notify.py 写 outbox+audit。**M5B 媒体二期（文件双向 + 语音入站 + 视频入站）已实现（2026-08-21，419 个测试全绿；真机验收通过）**——send_file 三路由、语音转写即文字、视频存盘+抽帧提示、media 清理三分规则（见 M5B 清单）。**M5C1 入站文本体验（连发合并 + 队列感知 ACK）已实现（2026-08-21，434 个测试全绿；真机验收通过）**——纯文本 chat 连发进 per-user KV 合并窗口（默认 2s，可调）、首条 ACK 合并意图、flush「已合并 N 条」+ 队列位次；B 运行中追加仅 ACK 语义（真中途注入结构不可）；崩溃恢复扫 KV；spec §3.2 收窄 flush-first（见 M5C1 清单）。**M5C2 出站 Markdown 清洗 + M5C3 快捷命令已实现并真机验收通过（2026-08-21，487 个测试全绿；五项全过——验收期实测发现：**微信新版手机+PC 双端原生渲染 Markdown**（`##`→黑体、`**`→黑体、表格可读、代码块吞围栏保留内容），M5C2「微信不渲染」立项前提被推翻，`md_clean` 默认翻转为 false——清洗降级为逃生通道）**——md_clean 纯函数 + 投递前清洗（outbox 恒存原文）、折算四处口径一致、`throttle.md_clean` 开关（/config set 首个 bool 键）；内置别名 /t /s /c /cs + /alias 用户自定义 + app 层双层展开（见 M5C2/M5C3 清单）。**M3 媒体收发（图片双向）真机验收通过**（2026-08-19，spec §5 五项全过；验收期实测修正：出站 aes_key 形态、bg watcher 三终态、bg 摘除 mcp-config，见 M3 清单）。**2026-08-20 收尾批**（开放问题与技术债全清、真机验收全绿）：bypass deny 实测落定（不生效，**Windows+Linux 双复证**；npm auto-update 运行期中途漂移 2.1.233→2.1.235 亦实证——启动探测的已知局限）、CLI 版本探测机制化（[worker/version.py](worker/version.py)）、/cancel 进程组/整树 kill（真机零残留实证）、出站熔断按页计数+跨重启（outbox.sent_at）、data/media 定期清理（`media_retention_days`，覆盖 media 根目录的 claude 自定义名图片）、/permissions 畸形结构防护（PermStructureError，不再静默吞写）、playwright MCP 装载（默认启用，真机全链路通——含 `ignoreHTTPSErrors` 绕自签证书、服务器 CJK 字体装配）、截图回传铁律落地为 runner prompt 注入（`_PROMPT_SUFFIX`，CLAUDE.md 软指令三次实证不够）。M2 已实现（2026-08-16）。M3 全部完成、真机验收通过（2026-08-19，余项 B 四项全过：OCR 主链路 / /mcp 列表呈现系统条目 / /mcp off 系统条目拦截 / /bg 回归）：媒体收发（图片双向）+ /mcp 启停与 /config 写入（余项 A，spec 2026-08-19-mcp-config-writable-design）+ OCR MCP（余项 B，spec 2026-08-19-ocr-mcp-design）。设计与实现决策仍以下列文档为准，实现与 TRD 的已知偏差登记在 `docs/superpowers/plans/2026-08-15-m1-mvp.md` Self-Review 节与 `.superpowers/sdd/` 各审查记录：

- [docs/PRD.md](docs/PRD.md) — 产品需求（功能 FR-1~10、非功能需求、里程碑 M1/M2/M3、范围外）
- [docs/TRD.md](docs/TRD.md) — 技术设计（架构、SQLite 数据模型、claude CLI 调用规范、命令路由、安全设计、测试策略）
- [README.md](README.md) — 部署、使用命令表与 M2 边界

**M2 功能清单**（M1 收发/任务池/命令总线/崩溃恢复之上新增）：

- **strict 档审批**：`/policy strict` 后任务带 `--permission-prompt-tool mcp__daoyu__approve`；[worker/approval_mcp.py](worker/approval_mcp.py)（stdio JSON-RPC server，经临时合并 mcp config 由 claude 拉起、任务结束即删）写 approvals 行 + outbox 🔐 推微信；gateway `handle_inbound` 拦截 Y/N 单字 decide（300s 超时 = expired = 拒绝）。
- **`/bg` 长任务**：桥命令建 bg 任务 → runner `claude --bg` 启动分支（bg_id 落盘即回执）→ [worker/pool.py](worker/pool.py) `_bg_watcher` 轮询 `claude agents --json --all` 推进（真机 2.1.233 三终态：`done`=完成/`blocked`=等用户输入，均 fork 取结果推送完结、`failed`=失败重试；条目无输出字段，结果靠 `--fork-session` 回原会话取——直接 `--resume` 被 daemon 持有拒绝且 rc=0；消失取消）；`/cancel` 走 `claude stop`。
- **MCP 装载**：`claude/mcp.json` 已装 chrome-devtools / context7 / web-reader / playwright 四台（平台无关形态——command 直写 npx/uvx，runner 合并层 Windows 包 cmd /c（白名单 {npx,uvx}）+ 过滤 disabled；Linux 侧 chrome-devtools 与 playwright 共用 headless Chrome 装配——`inject_linux_chrome` / `inject_linux_playwright` 按约定路径注入 `~/.cache/puppeteer/chrome-headless-shell/linux-*`（npmmirror 装，手动拼 URL 会 404 必须走 `@puppeteer/browsers`）+ `~/chrome-libs` 解包 libasound 免 sudo，注入 `--headless --isolated --executablePath|--executable-path` + LD_LIBRARY_PATH + 清死代理；chrome-devtools 2026-08-19/20 真机验证 Next.js SPA 完整渲染（直连与微信全链路双验证，进度条 `mcp__chrome-devtools__new_page` 实证），playwright 2026-08-20 装载（钉版 `@playwright/mcp@0.0.79` 默认启用；chrome-headless-shell × playwright `--executable-path` 兼容性已真机确认（2026-08-20：headless-shell 152 + `LD_LIBRARY_PATH=~/chrome-libs/usr/lib64` + 不加 --no-sandbox——启动/导航/截图实证，`browser_run_code_unsafe` 可开 `ignoreHTTPSErrors` context）；未安装 no-op fail-open）；另有 daoyu-ocr 系统条目（RapidOCR 本地 OCR，runner 恒注入、不受 /mcp 启停管辖，余项 B）。
- **配置代理命令**：[gateway/proxy.py](gateway/proxy.py) — `/permissions`（列表 + deny add/del + allow add，写 `claude/settings.json`）、`/mcp`（列表 + on/off 启停，写 mcp.json 顶层 disabled）、`/config`（概览 + set 白名单键写 gateway/config.json，重启生效；M2 时七键，M4 起 cron 阈值并入、M5C1/M5C2 再入 merge_window_s/md_clean 共 16 键）。
- **同目录多话题**：sessions 表 `UNIQUE(wechat_user, cwd, claude_uuid)`（ensure_schema 对旧表做无损迁移：建 v2 → 搬行 → 换名，幂等）。`/new` 当前目录开新话题；`/sessions` 两级展示（目录分组 + 组内全局序号，序号按 last_active_at DESC）；`/cd #n` 切话题、`/cd <路径>` 切目录（指向该目录最新话题，无则建）；当前话题指针在 state KV `active_session:<wechat_user>`（[common/db.py](common/db.py) `get_active_binding`，chat/policy/bg/cancel 均走它；老库无指针时经旧 `cwd:` 指针回退并回写）。`/policy` 每话题独立。
- **`/sessions`**：会话列表（目录 + 最近任务摘要 + uuid 短码）与 `/cd #n` 序号切换（见上一条：现为话题两级展示）。
- **`/adopt [uuid前缀]`**：收养终端 TUI 创建的 Claude 会话为当前话题（[gateway/bridge.py](gateway/bridge.py) 扫描 `data/claude-home/projects/*/*.jsonl` 未管理 transcript，mtime 降序无参取最新、≥8 位唯一前缀指定；从 transcript 首段提取 cwd 与首条 prompt 做标签；`db.adopt_session` 建 sessions 行并置 `claude_session_inited:<uuid>`——已存在 uuid 必须走 `--resume`，`--session-id` 会报错）。前提：终端会话用 [deploy/daoyu-tui.sh](deploy/daoyu-tui.sh) 创建（清死代理 + source secrets.env + CLAUDE_CONFIG_DIR 指向 daoyu-home——宿主 shell 无 ANTHROPIC_* 凭据，直接 `claude` 会连不上且宿主会话对 runner 不可见）。同会话并发 `--resume` 冲突硬约束不变——终端仍开着该会话时先退出。
- **`/delete #<序号>` / `/delete task <任务号>`**：删话题（连同其 tasks/outbox/approvals）或单删任务记录；预置 `delete_confirm:<user>` 确认门（回 Y 才真删，app.py 拦截执行）。三闸防误删：序号/任务号合法性、当前话题拒删、pending/running 任务拒删（先 /cancel）。
- **监控告警**：死信 / 日限熔断 / 预算耗尽死信 / 连接失效清 token 四处自动推微信 ⚠️（复用出站通道，发全部白名单）。

**M3 功能清单**（媒体收发，图片双向；**真机验收通过 2026-08-19**，spec §5 五项全过。验收期实测修正三处：出站 `media.aes_key` 形态 = base64(hex32 ASCII)（传 base64(raw16B) 微信端空白图）；MCP 工具需 settings allow（acceptEdits 不放行 MCP 工具、headless 无确认通道直接 deny）；bg 摘除 `--mcp-config`（daemon 异步读与临时文件即删竞态，见硬性约束））：

- **入站发图即对话**：[gateway/app.py](gateway/app.py) 遍历 `item_list`（`message_type==1` 不变，图片 `type==2`）→ [gateway/media.py](gateway/media.py) CDN 下载 + AES-128-ECB 解密（aeskey 双形态：`image_item.aeskey` hex 优先 / `media.aes_key` base64）→ 随机名落盘 `data/media/inbound/`（magic bytes 白名单 PNG/JPEG/GIF/WebP、20MB 上限）→ 纯图建 chat 任务（prompt 模板"[用户发来图片，已保存到 {p}，请查看并回应]"）、图文拼 prompt；下载失败 ⚠️ 回执、不建任务。
- **出站 `send_image`**：Claude 调 MCP 工具 `send_image(path, caption)`（[worker/approval_mcp.py](worker/approval_mcp.py) 现为 daoyu 统一 stdio server，`DAOYU_TOOLS` 装配：strict="approve,send_image"、其余档="send_image"（M5A 起追加 notify、M5B 起追加 send_file，见各清单）；经临时合并 mcp config **-p 四档恒装配**（`/bg` 不带，见硬性约束）；工具本身需 `claude/settings.json` allow `mcp__daoyu__send_image`）→ 校验复制到 `data/media/outbound/` → 写 outbox `kind=image` 行（与 [common/db.py](common/db.py) 的 `db.enqueue_media` 同构的裸 SQL，跨进程写入）→ 出站协程整链路现做（getuploadurl → CDN 密文 POST 取 `x-encrypted-param` → caption 文本条 → 图片条），失败整行重试（不缓存 downloadParam）。协议细节见 [docs/superpowers/specs/2026-08-19-m3-media-design.md](docs/superpowers/specs/2026-08-19-m3-media-design.md) §2。
- **schema**：messages 加 `media_path`、outbox 加 `kind` / `media_path` / `caption`（幂等 ALTER），入站图片路径随 messages 行落盘。
- **OCR MCP**：daoyu-ocr 独立 stdio server（[worker/ocr_mcp.py](worker/ocr_mcp.py)，工具 ocr(path)——本地 RapidOCR 中英混识、引擎 lazy、PNG/JPEG 白名单、bytes 直传实测 API）；runner 恒注入系统条目（disabled 不管辖）、settings allow `mcp__daoyu-ocr__ocr`；/mcp 列表呈现系统条目行。bg 无 MCP 口径不变。

**M4 功能清单**（主动服务：定时日报 + 巡检告警；已实现 2026-08-21，spec [docs/superpowers/specs/2026-08-21-cron-patrol-design.md](docs/superpowers/specs/2026-08-21-cron-patrol-design.md)）：

- **scheduler 第五常驻协程**（[gateway/scheduler.py](gateway/scheduler.py)，与 poll/outbound/reconnect/worker-pool 并列）：每分钟整分对齐醒来、每轮现读 cron_jobs 表（`/cron` 改表即时生效，无需通知机制）；整轮 try/except 记 audit 不杀协程（调度器死 ≠ 通道死）；CPU/内存滚动采样窗口在 loop 建一次跨轮持久（每轮新开窗口会让「持续 N 分钟」判定永久失效）。**正常轮次零 Claude 调用**（零成本原则）——scheduler 不直接调 iLink、不自己跑 Claude：分析任务经现有任务池（预算/审批/节流自然生效），推送经现有 outbox。
- **`/cron` 命令族**（桥命令）：列表（✅/⏸、每天 HH:MM 或每 N 分钟、下次运行现算、上次结果）+ `on|off <daily|patrol>` + `time daily <HH:MM>` + `interval patrol <分钟>`；`on` 重置 last_run_at 从当前时刻起算——patrol 满一个间隔跑首轮、daily 到点即跑不补跑错过的时间点。cron_jobs 表 ensure_schema 建表并预置 daily(08:00) + patrol(10min) 默认启用。
- **日报（daily）+ 巡检（patrol）**：日报三板块（昨日全天窗口：任务三态 done/canceled/dead 与费用——tasks 表无持久 failed 态，重试回 pending、耗尽即 dead ｜ psutil 健康快照 ｜ 刀鱼自身：出站条数/队列/死信/连接/media 体积）纯 Python 模板秒推，异常（死信新增/健康超阈/队列积压/掉线）才建 Claude 分析任务，**模板先推、分析后到**。巡检四类——磁盘阈值、CPU/内存连续 `load_sustain_min`（默认 5）个采样超阈（瞬时尖峰不误报）、刀鱼自身（队列积压 + iLink token 复核；死信不查——M2 即时告警专责，不双通道重复）、证书到期（cert_paths 扫 *.pem，路径不存在跳过不误报）；异常合并推一条告警 + 合并建一个分析任务，同类 item_key 静默期 `alert_silence_h`（默认 6h）内不重报、过期仍异常再报（state KV `cron_alert:<key>`）。
- **ops 话题与配置**：分析任务挂靠固定 UUID 话题（`ensure_ops_session` 幂等建行）——分析历史聚一处、Claude 有先前分析上下文；出现在 /sessions、可 /cd 进入、可 /delete（删了下次需要时自动重建）。`gateway/config.json` 新增 `cron` 节（八键，config.example.json 同构默认值），数值七键入 /config set 白名单（原七键 → 共 14 键，重启生效），cert_paths 低频直接改文件。psutil 入 pyproject dependencies（`pip install -e .` 自动装；scheduler 内 lazy import，未装不影响 /cron 命令）。

**M5A 功能清单**（通知通道：外部事件接入；已实现 2026-08-21，spec [docs/superpowers/specs/2026-08-21-notify-channel-design.md](docs/superpowers/specs/2026-08-21-notify-channel-design.md)）：

- **纯单向推送（outbox 直写复用）**：所有入口经 [common/notify.py](common/notify.py) `push_notification`（取裸 sqlite3.Connection 直写——gateway/CLI/MCP 孙进程三类调用方零适配，WAL 多进程写安全）写 outbox 文本行（task_id=None）+ audit 一行（同 commit）——不建任务、不进会话，节流/分页/重试/死信/日限熔断全部由现有出站协程自然继承。前缀语言：🔔 通用 / ✅ 终端任务完成（Stop）/ ❓ 等待确认（Notification），与 ⚠️/🔐 同一体系。
- **四入口**：① CLI `daoyu-notify <标题> [正文…]`（console script，[gateway/notify_cli.py](gateway/notify_cli.py)；`--hook stop|notification` 从 stdin 读 Claude Code hooks JSON 容错解析；env `DAOYU_DB`+`DAOYU_WHITELIST` 齐备则不读 config.json——shell/cron/终端 hooks 共用）→ 全部白名单广播；② MCP 工具 `mcp__daoyu__notify(title, body)`（approval_mcp 内 `DAOYU_TOOLS` 装配，**四档恒装**：strict="approve,send_image,notify"、其余档="send_image,notify"（M5B 起各追加 send_file）；目标 = 任务属主，`DAOYU_TO_USER` 注入同 send_image 先例；`/bg` 无 MCP 不可用——同 send_image）；③ HTTP `POST /notify`（[gateway/notify_http.py](gateway/notify_http.py) **第六常驻协程** aiohttp 单路由，默认监听 127.0.0.1:8417；`secrets.env` 设 `notify_token` 则要求 `Authorization: Bearer`，不设则仅 localhost 绑定兜底；启动失败 audit 不杀其余通道）→ 白名单广播；④ 终端 hooks 零代码接入——[deploy/notify-hooks.example.json](deploy/notify-hooks.example.json) 的 `hooks` 节一次性合并进服务器 **`data/claude-home/settings.json`**（TUI 经 daoyu-tui.sh 启动、CLAUDE_CONFIG_DIR 指此——2026-08-21 真机落定：宿主 `~/.claude/settings.json` 不被 TUI 读取；Stop 每轮回复结束都触发，嫌吵可只留 Notification 节）。
- **配置**：`gateway/config.json` 新增 `notify` 节（`listen` 默认 "127.0.0.1:8417"、`http_enabled` 默认 true，config.example.json 同构默认值）——低频运维键直接改文件、不进 /config set 白名单（同 cert_paths 口径）。
- **熔断代价（明示）**：通知行走 outbox，日限熔断对通知同样生效——外部源高频推送会触发全局熔断暂停**全部**出站（含对话回复），README 明示接入方自行限频。

**M5B 功能清单**（媒体二期：文件双向 + 语音入站 + 视频入站；已实现 2026-08-21，spec [docs/superpowers/specs/2026-08-21-media2-design.md](docs/superpowers/specs/2026-08-21-media2-design.md)；真机验收通过）：

- **范围与架构**：文件双向、语音入站（转写优先）、视频入站存盘；语音/视频出站不做专用条（音频走文件条，官方同款模式）。零新模块——media.py / app.py / outbound.py / approval_mcp.py / ilink.py 直接扩展。
- **两套媒体编号错位（高危协议事实）**：入站 item type 语音=3/文件=4/视频=5；出站 getuploadurl media_type 视频=2/文件=3——同名不同值、无对应关系。[gateway/media.py](gateway/media.py) 常量分域（`ITEM_TYPE_*` 入站 / `MEDIA_TYPE_*` 出站）防混用。
- **大小字段三语义**：image 条 `mid_size` = 密文数字、video 条 `video_size` = 密文数字、file 条 **`len` = 明文大小十进制字符串**（三处不一致照抄官方 send.ts，`build_file_item` / `build_video_item`）。
- **入站路由**（[gateway/app.py](gateway/app.py) `handle_inbound` 四分支）：语音 `voice_item.text` 转写非空直接并入 text_parts 当用户文字（官方同构）；空则下载存档 `.silk` + ⚠️ 回执、**不建任务**（Claude 解不了 SILK）。文件 prompt 行带原始名+大小（`[用户发来文件 报表.xlsx（0.0MB），已保存到 …，请查看处理]`）；视频存盘 `.mp4` + ffmpeg 抽帧提示。语音/文件/视频 aeskey 仅 `media.aes_key` 单形态（base64(hex32 ASCII)，`parse_media_aes_key`），100MB 上限，随机名前缀 `file-`/`voice-`/`vid-` 落盘。
- **出站 `send_file(path, caption)` 三路由**：MCP 工具（[worker/approval_mcp.py](worker/approval_mcp.py) `_send_file`；`DAOYU_TOOLS` 四档恒装：strict="approve,send_image,send_file,notify"、其余档="send_image,send_file,notify"；`/bg` 无 MCP 不变；settings allow `mcp__daoyu__send_file`）——`IMAGE_EXTS` 转既有 `_send_image` 链路（kind='image'，含 magic bytes 校验）；其余 ≤100MB 保留原名复制 `data/media/outbound/`（重名加 hex 后缀防覆盖），写 outbox `kind='file'` 行。投递层 [gateway/outbound.py](gateway/outbound.py) `_send_file_media` 按扩展名再分：`VIDEO_EXTS` → `upload_media(media_type=2)` + video 条、否则 media_type=3 + file 条（file_name=basename）；caption 文本条先发，失败整行重试（M3 同款语义）。ilink 层 `send_media_message(to_user, ctx, item=…)` 泛化为任意媒体条（`send_image_message` 委托它，M3 签名不变）。
- **media 清理三分规则（实现期修正，原 spec「清理已被 media_retention_days 覆盖」不成立——M2 批清理只认 img-\*/图片扩展名）**：[gateway/media.py](gateway/media.py) `cleanup_expired_media` 扩展三分——outbound/ **全量**按 mtime（daoyu 独占）；inbound/ 按前缀 `img-`/`file-`/`voice-`/`vid-`（claude 误写的非前缀文件不碰）；media 根目录保守规则不变（img-\* 或图片扩展名——claude 工作产物混居，不作猜测）。未终态 outbox 行引用的 media_path 一律保护（`db.active_media_paths`）。

**M5C1 功能清单**（入站文本体验：连发合并 + 队列感知 ACK；已实现 2026-08-21，spec [docs/superpowers/specs/2026-08-21-input-merge-design.md](docs/superpowers/specs/2026-08-21-input-merge-design.md)；真机验收通过）：

- **连发合并窗口（A）**：纯文本 chat 消息进 per-user 合并窗口——首条 ACK「✅ 收到，正在合并后续消息（Ns 内无新增即开始处理）」+ 建 `merge_pending:<user>` KV（`{texts, session_id, first_msg_id, started_at}`，DB-state 持久化崩溃可恢复）；窗口内追加静默+重置 asyncio call_later 计时；flush 拼 texts → `create_task` → 清 KV。窗口默认 `throttle.merge_window_s=2.0`（`/config set` 可调，0 禁用）。语音转写（`voice_item.text`）并入 text_parts 同走合并路径（生产语义）。
- **flush-first 收窄（实现期修正 spec §3.2）**：原述「slash/Y-N/媒体先 flush」收窄为**仅 task-creating 非 chat 路径**（forward / 媒体即对话 / 纯媒体任务）先 flush 暂存再建自身任务（序不倒）；Y-N/bridge/proxy 不建任务、窗口计时自行 flush；session_id 在 append 时锁定故 `/cd` 不影响已暂存 batch。`_flush_merge_pending` 空暂存时空操作。`_flush_merge_pending` 必须 cancel 弹出的计时器（防僵尸级联过早冲刷后续窗口）。
- **B 队列感知 ACK**：新建任务查 `db.pending_task_count(session_id)`（pending/running 计数），pos>1 时 ACK 追加「（当前任务完成后接上，你排在第 M 位）」——诚实表达「追加=下一轮」的结构现实。**真·中途注入结构不可**（[worker/runner.py:226-231](worker/runner.py#L226-L231) `proc.stdin.close()`）。
- **崩溃恢复**：`main_async` 启动扫描 `db.scan_merge_pending()` 逐条 flush（`recover=True`，ACK 用「已恢复」措辞 + audit `merge_recover`）；session 已删则回退 active binding 不炸。
- **不做**：text+media 跨类合并、自适应窗口、bg blocked 喂输入（结构不可）。

**M5C2 功能清单**（出站 Markdown 清洗；已实现并真机验收通过 2026-08-21，spec [docs/superpowers/specs/2026-08-21-mdclean-alias-design.md](docs/superpowers/specs/2026-08-21-mdclean-alias-design.md)——验收结论与设计修订见 spec §7）：

- **md_clean 纯函数**（[common/mdclean.py](common/mdclean.py)，仅 stdlib re）：处理管线 = fenced 代码块切块保护（```/~~~ 围栏内**原样保留**——代码里 `**` 是 glob、`#` 是注释绝不能洗；整体每行缩进 4 空格、去围栏行与语言名）→ 块级规则（标题 `#{1,6}`→【】、无序列表→`•`、引用 `>`→`｜`、水平线→`———`）→ 表格转写（header + `|---|` 分隔行识别；**两列恰一行**=转置键值竖排 `• h：v`，其余=删分隔行统一 `• c0 ｜ c1`）→ 行内规则（`` `x` ``→「x」占位提取后不碰、粗体/斜体/删除线脱壳、`[t](u)`→`t(u)`、`![a](u)`→`图片 a(u)`、反斜杠转义最后）。**幂等**（产物不再构成 Markdown 输入）；无 Markdown 文本**逐字节不变**（系统回执模板天然无损）；`_x_` 斜体明确不做（snake_case 误伤）、斜体 `*x*` 要求两侧紧贴非空白（`3 * 4 * 5` 不误伤）。
- **投递前清洗**（[gateway/outbound.py](gateway/outbound.py) `_mdc` helper）：`_drain_once` 文本行 **先 `md_clean` 后 `split_text`**——清洗增量（表格转置/缩进）必须先于字节硬闸 `MAX_PAGE_BYTES` 计算，分页后清洗会越 16384B 静默丢；`_send_media`/`_send_file_media` 的 caption 同清洗（单发短文本无字节风险）；`_send` 纯发送不动。**outbox 恒存原文**——清洗只在发送侧，规则升级后死信重投自动受益、审计无损。
- **折算四处口径一致**（出站熔断按页计数的延伸）：`outbox_sent_pages` / `db.sent_pages_today` 加 `md_clean_enabled` 参数（签名默认 False），文本行折算同过 md_clean——运行时计数 / 重启恢复 / 日界重算 / bridge `/status` 折算四处统一传 `bool(cfg.throttle.get("md_clean", False))`。
- **开关**：`throttle.md_clean`（bool **默认 false**——2026-08-21 真机实测微信新版手机+PC 双端原生渲染 Markdown，原文直发视觉优于转写；清洗降级为逃生通道，老客户端/渲染异常时 `/config set throttle.md_clean true` 开回，`_DEFAULT_THROTTLE` + config.example.json 同构）——`/config set` **首个 bool 键**（值认 true/false、parser 转布尔、JSON 写回布尔；既有数值键解析路径不动），白名单 16 键；重启生效。

**M5C3 功能清单**（快捷命令：内置短别名 + 用户自定义；已实现并真机验收通过 2026-08-21，同 spec）：

- **内置短别名**（[gateway/router.py](gateway/router.py) `BUILTIN_ALIASES`）：`/t`=`/tasks`、`/s`=`/status`、`/c`=`/cancel`、`/cs`=`/sessions`——`route()` 判空后、BRIDGE_COMMANDS 判定前静态映射（route 保持纯函数），args 原样跟随；unknown 建议池不含用户别名（纯函数拿不到 KV，YAGNI）。
- **`/alias` 桥命令**（[gateway/bridge.py](gateway/bridge.py)）：`add <名> <内容…>` / `del <名>` / `list`（空时列内置四条），state KV `alias:<user>` 单键 JSON dict（`merge_pending:<user>` 同构先例，崩溃天然持久）。校验：name 非空 ≤16 字符无空白、value ≤2000 字符、条数 ≤50；**撞名规则**——撞桥/运维/代理集合及 `alias` 自身拒绝（防自毁管理入口），撞内置别名 t/s/c/cs **允许=覆盖**（用户层先于内置层展开，天然生效），撞 Claude 动态 slash_commands 允许但回执重名提示。
- **双层展开**（[gateway/app.py](gateway/app.py) `_expand_alias`）：用户别名在 `handle_inbound` 内 route **前**展开（KV 命中返回「值 + 空格 + 附加参数」；非斜杠/未命中/坏 JSON 均返回 None 不炸入站），**先于内置层**——同名时用户定义覆盖内置；展开结果照常 route 一次（**不再二次展开**，防链式循环），与直发该文本完全一致——展开为 chat 文本照常进 M5C1 合并窗口、展开为 `/tasks` 走 bridge 秒回，零特判路径。入站 messages 落盘**原始** `/go`（审计看用户发了什么）、create_task prompt 用**展开后**文本（任务看 Claude 收到什么）。

组件清单（入口文件）：

- **入口**：`daoyu` console script → [gateway/app.py](gateway/app.py) `start()`（读 `gateway/config.json` + `claude/secrets.env`，崩溃恢复后常驻 poll / outbound / reconnect / worker-pool / scheduler / notify-http 六协程）；`daoyu-notify` → [gateway/notify_cli.py](gateway/notify_cli.py)（M5A 通知 CLI：直接参数 / --hook 模式，推微信后退出）；`daoyu-login` → [gateway/login.py](gateway/login.py)（终端扫码，token 写 DB state 后退出）。
- **gateway**：[gateway/ilink.py](gateway/ilink.py)（iLink 协议封装）、[gateway/router.py](gateway/router.py)（命令总线路由）、[gateway/bridge.py](gateway/bridge.py)（桥命令 + /help 多层合并）、[gateway/proxy.py](gateway/proxy.py)（TUI 配置命令微信代理）、[gateway/outbound.py](gateway/outbound.py)（outbox 投递/重试/死信/节流/typing + 图片与文件 CDN 上传链路）、[gateway/reconnect.py](gateway/reconnect.py)（连接守护，**主动续期周期默认 30 天**——2026-08-19 实证推翻 TRD "24h 过期"假设：本机实例 token 连续 ≥2.6 天活跃有效无 401，官方 openclaw-weixin dist 亦无免扫码续期路径（`binded_redirect` 是扫码时已绑定的状态）；官方 README 定义 `errcode -14 = session timeout` 且不声明 TTL，官方客户端对 -14 仅暂停 1h 同 token 重试（无重登）。**token 失效的权威信号是应用层 `errcode`/`ret = -14`（HTTP 200 响应体）而非 HTTP 401**——poll_loop 两路都清 token 触发重扫（-14 连续 5 次防抖 ≈25s）。续期时静默优先：`local_token_list` 带旧 token 轮询 `silent_grace_s`（默认 30s）超窗才推二维码；`bot_token_last` 永清副本保证 401/403 清 token 后线索不丢）、[gateway/media.py](gateway/media.py)（媒体 CDN AES-128-ECB 上传/下载/解密；M3 图片 + M5B 文件/语音/视频与清理三分规则）、[gateway/scheduler.py](gateway/scheduler.py)（M4 主动服务：日报+巡检调度协程）、[gateway/notify_http.py](gateway/notify_http.py)（M5A 通知 HTTP 入口协程：127.0.0.1 单路由 POST /notify）、[gateway/notify_cli.py](gateway/notify_cli.py)（daoyu-notify CLI：shell/cron/终端 hooks 入口）。
- **worker**：[worker/pool.py](worker/pool.py)（按 session 串行调度池 + bg 后台监视 watcher）、[worker/cli_builder.py](worker/cli_builder.py)（claude argv 组装 + Linux chrome 注入 + Windows shim 解析）、[worker/runner.py](worker/runner.py)（子进程执行/流式进度/费用记账/bg 启动分支；进程组/整树 kill——POSIX `start_new_session`+`killpg`、Windows `taskkill /T`，MCP 孙进程不残留）、[worker/stream.py](worker/stream.py)（stream-json 解析 + 节流器）、[worker/approval_mcp.py](worker/approval_mcp.py)（daoyu MCP server：审批 approve + 发图 send_image + 发文件 send_file + 中间通知 notify，`DAOYU_TOOLS` 按档装配）、[worker/ocr_mcp.py](worker/ocr_mcp.py)（daoyu-ocr：本地 OCR）、[worker/version.py](worker/version.py)（CLI 版本探测：`EXPECTED_CLAUDE_VERSION` 基线比对，漂移 audit+warning fail-open）。
- **common**：[common/db.py](common/db.py)（SQLite 五表 + approvals + state KV；M3 加 messages.media_path 与 outbox.kind/media_path/caption；M4 加 cron_jobs 表并预置 daily/patrol 两行）、[common/config.py](common/config.py)（配置加载契约）、[common/models.py](common/models.py)、[common/text.py](common/text.py)（长文本分页）、[common/notify.py](common/notify.py)（M5A 通知核心：format+push 写 outbox+audit）、[common/mdclean.py](common/mdclean.py)（M5C2 出站 Markdown 清洗纯函数 md_clean——fence 保护+表格竖排+幂等）。
- **配置**：`gateway/config.example.json`（实例 config.json 进 gitignore）；`claude/settings.json` + `claude/mcp.json`（进 git，宿主隔离靠 CLAUDE_CONFIG_DIR，见硬性约束）；`claude/secrets.env`（gitignore）；`deploy/daoyu.service`（systemd 单元）；`deploy/notify-hooks.example.json`（M5A 终端 hooks 配置片段示例）。

## 常用命令

```bash
python -m pytest                        # 全量测试（487 个）
python -m pytest tests/test_e2e.py -v   # E2E（fake iLink + fake claude 子进程；M2 含审批往返/bg 冒烟；M3 媒体 E2E 在 tests/test_media_e2e.py）
daoyu-login                             # 终端扫码登录（token 落盘后退出）
python -m gateway.app                   # 前台调试运行（不进 systemd）
```

Windows 开发机（Git Bash）下 venv 解释器在 `.venv/Scripts/python`，Linux 生产在 `.venv/bin/python`。

## 核心架构（写代码前必读）

三组件 + 一条持久化脊柱，gateway 与 worker 同进程（单个 systemd 服务）：

1. **gateway**（Python asyncio，fork weixin-ClawBot-API 收发层）：iLink 长轮询收微信消息 → 入站落盘去重 → 命令路由（本地命令秒回 / 其余入队）→ 出站发送（重试、分页、节流）。**永不阻塞、绝不等 Claude**——agent 慢不影响微信端。
2. **SQLite**（`data/daoyu.db`，WAL 模式）：唯一事实源。messages / tasks / outbox / sessions / audit_log 五表，另 approvals 表、state KV 与 M4 的 cron_jobs 表。**一切先落盘**，任何进程崩溃后可完整恢复（启动时 running 任务重置重跑、pending 消息重投）。
3. **worker**（同进程 asyncio task 池，并发 2~3）：取任务 → 按官方规范组装 claude 命令行 → 子进程执行 → 解析 stream-json → 节流推进度 → 写 outbox。

**关键认知**：后端没有独立的 agent 框架——**智能本体就是 Claude Code CLI 本身**（`claude -p` headless 子进程）。worker 只是"保姆"代码：取任务、拼命令行、起子进程、解析输出流、回推结果。工具、MCP、skills、上下文管理全部由 Claude Code 原生提供，worker 一概不重新实现。

## 硬性技术约束（违反即 bug）

- **每次调用 claude CLI 全量传 flag**：`--resume` 不恢复 `--permission-mode` / `--mcp-config` / `--add-dir`，必须每次重传。
- **同一 Claude 会话（同 session UUID）的任务必须串行**（`--resume` 同会话并发会冲突）；不同会话可并行。任务队列按 session 分组串行。
- **resume 必须在同一 cwd**（Claude 按 cwd + git worktree 作用域）；`/cd` 切目录 = 换绑另一会话。
- **用户 prompt 经 stdin 传入** `claude -p`，避免 shell 转义问题；子进程 cwd = 会话绑定的工作目录。**stdin 一次性关闭**（[worker/runner.py](worker/runner.py) 写 prompt 后即 `proc.stdin.close()`）——`-p` 读一次即跑完，**中途无注入通道**；会话继续靠任务结束后的 `--resume`（下一回合）。故「运行中追加输入」只能做 ACK 队列位次语义（M5C1），不能真注入运行中回合。
- **strict 审批 flag 语义**：strict = `--permission-mode default` + `--permission-prompt-tool mcp__daoyu__approve`（实测 acceptEdits 下不触发 prompt-tool、default 才触发，TRD §4.1 "strict=acceptEdits" 假设已被实测推翻）；审批 server 条目经**临时合并 mcp config**（静态 mcp.json + daoyu 条目，含任务级 env，`daoyu-mcp-` 前缀）传入，任务结束（成功/失败/取消）即删、启动时清扫 kill 残留。server 键 `daoyu` 与工具引用必须严格一致（不一致 = Claude 找不到审批工具 = 该次工具调用被 deny，fail-safe）。**审批工具的返回必须是 behavior JSON**（`{"behavior":"allow","updatedInput":{...}}` / `{"behavior":"deny","message":...}`）——纯文本会被 claude 判 invalid permission result，决策从未生效。
- **`--bg` flag 集（真机实测 2026-08-19）**：`--bare` + 预算 + `--permission-mode` + `--settings`（硬 deny 清单与 `-p` 一致生效）+ bypass 档 `--disallowedTools`（与 `-p` 同源常量）；不传 `--permission-prompt-tool`——strict 档 `/bg` 在 default 模式下需审批的工具（Bash/写文件）被直接拒绝（fail-safe，仅适合只读任务），回执/文档已如实明示；prompt 以 `-` 开头时前置空格防 flag 解析。**不传 `--mcp-config`**：daemon 异步拉起 worker（客户端返回 ~1s 后才读 mcp config），临时文件在 run() 返回即删 → daemon "exit 1 before init" 100% 复现；bg 会话因此无 MCP 工具（send_image 不可用），回执明示。
- **bg 三终态与取结果（真机实测 2.1.233）**：`claude agents --json --all` 条目终态 `done`/`blocked`/`failed`（默认过滤 failed，**必须带 `--all`**）；done 条目十字段（pid/id/cwd/kind/startedAt/sessionId/name/status/state）**无输出/cost 字段** → 取结果靠回原会话（`--fork-session`，直接 `--resume` 被 daemon 持有拒绝且 **rc=0**、错误只在输出——静默空结果）；`blocked` = 会话等用户后续输入（Claude 结尾反问是常态），bg 无输入通道即永久挂起 → 首次观察即 fork 取结果完结。
- **长任务必须走 `claude --bg` + `claude agents --json` 轮询**（后台任务管理是 `claude agents`；停止是 `claude stop <id>`；`claude logs <id>` 实测 2.1.233 存在——TUI 流含 ANSI 转义、人读可但不宜程序解析）：`-p` 结束 5s 会杀后台 bash，subagent 默认上限 10min。
- **`context_token` 只使用当前会话最新入站消息的**，绝不复用历史值（复用旧 token 会 HTTP 200 但静默不投递）。
- **微信单条文本上限 = 16384 字节 UTF-8**（2026-08-20 实测钉死：16384 ✓ / 16385 ✗，按**字节**计——中文 5450 字过 / 5500 字不过、ASCII 12000 字过）；超限仍 `errcode=0` 静默不投递，与 context_token 复用同款陷阱。出站分页 [common/text.py](common/text.py) `split_text` 双上限兜底：字符（`page_char_limit`）+ 字节硬闸 `MAX_PAGE_BYTES=15000`——即便 limit 调高也不越线。
- **入站按 `msg_id` 幂等去重**（iLink 重连后消息会重投）；出站走 outbox 发件箱，失败重试，至少 5 次后才进死信并告警。
- **宿主配置隔离靠 `CLAUDE_CONFIG_DIR`（机制化）**：实测 `--bare`/`--settings` 均不能隔离宿主 `~/.claude`（宿主 defaultMode/allow/trustAllFiles/插件全部穿透生效，直接架空 strict 审批与硬 deny 清单）；runner 与 pool 给每个 claude 子进程注入 `CLAUDE_CONFIG_DIR=<repo>/data/claude-home/`（调用即 mkdir）。**`-p` 路径已不带 `--bare`**（2026-08-19 实测：`--bare` 剥离 WebFetch/WebSearch/Write/Glob/Grep 全部扩展工具只留 Bash/Edit/Read+MCP；去掉后 WebSearch 经智谱端点适配 `web_search_prime` 完全可用、真机查证带 Sources 验证通过，WebFetch 因抓取前的 claude.ai 域名验证国内不可达而失败但模型会 fallback 到 web-reader MCP；bg 分支保守集保留 `--bare`）。凭据/模型映射**动态跟随宿主 `~/.claude/settings.json` 的 env 块**（`host_claude_env` 白名单取 `ANTHROPIC_*` + `API_TIMEOUT_MS`，逐键优先于 `claude/secrets.env` 兜底层；AUTH_TOKEN/API_KEY 形态二选一去重——用户在宿主侧轮换 key/改模型映射，刀鱼每任务现场跟随；只取凭据键，permissions/plugins 不碰、隔离语义不变）；MCP 清单经 `--mcp-config` 显式传。刀鱼持久配置在 `claude/settings.json` 与 `claude/mcp.json`（进 git），代理命令（/permissions /config /mcp）改的就是这些文件。
- **媒体出站走 outbox kind=image 行**：投递时整链路现做（上传→caption→图），
  失败整行重试（不缓存 downloadParam）；caption 与图分两条 sendmessage（官方模式）。
  MCP server 键 `daoyu` 统一装配（approve 仅 strict + send_image 四档，`DAOYU_TOOLS`）。
- **入站图片消息 `message_type==1` 不变、`item_list[].type==2`**；aeskey 双形态
  （`image_item.aeskey` hex 优先 / `media.aes_key` base64）；magic bytes 白名单 +
  20MB 上限，随机名落盘 `data/media/inbound|outbound/`。
- **媒体编号两套错位（M5B）**：入站 item type 语音=3/文件=4/视频=5；出站
  getuploadurl media_type 视频=2/文件=3——同名不同值、无对应关系，media.py
  常量分域防混用；条目大小字段三语义：image `mid_size`/video `video_size` 是
  密文数字，file 条 **`len` 是明文大小十进制字符串**（照抄官方）。

## 统一命令总线（产品核心）

微信命令与 Claude Code CLI **同一套语法、同一个命名空间**，不发明第二套命令体系。路由顺序：

1. **桥命令**（`/cancel` `/tasks` `/status` `/cd` `/sessions` `/policy` `/bg` `/new` `/adopt` `/delete` `/cron` `/alias`）→ gateway 本地执行，秒回。另有 iLink 运维命令 `/time` `/重新连接`（管连接本身，与 Claude 无关）。
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

- **硬 deny 清单**（`//etc/**`、`~/.ssh/**`、`~/.claude/**`、`//**/data/daoyu.db` 等；注意官方 permissions 语义：单前导 `/` 锚定 settings 来源目录而非绝对路径，**绝对路径必须 `//`**）：auto/strict/plan 档恒生效；**bypass 档下 deny 不生效**（2026-08-20 实测：bypassPermissions 跳过包括 deny 在内的全部权限检查，见 `.superpowers/sdd/bypass-deny-research.md`），恒叠加 `--disallowedTools` 工具级兜底（实测确证有效；但该清单只覆盖 Read/Edit/daoyu.db/Bash rm，Write 工具在 bypass 档无兜底——该档本义即用户自担）。
- **预算闸**（`--max-turns` + `--max-budget-usd`）与权限档位独立、恒生效，bypass 下仍限费；预算/回合耗尽的失败**不重试**（直接死信，防 3× 上限放大）。
- `/policy` 四档：auto（默认全放）/ strict（default + 审批 MCP：需批准的工具调用推微信 Y/N，5 分钟超时视为拒绝；`/bg` 任务无审批通道、需审批工具被直接拒绝，仅适合只读）/ bypass（deny 清单不生效——2026-08-20 实测，见硬 deny 条；工具级兜底 `--disallowedTools` 恒加）/ plan。
- secret 只放 `claude/secrets.env`（gitignore）+ 环境变量注入，日志脱敏。
- gateway 仅响应白名单微信账号，白名单外一律不响应。

## 图片回传约定（违反即体验缺陷）

- **截图/生成图片类任务必须用 `mcp__daoyu__send_image` 工具把原图回传微信**，不能只存盘 + 文字描述。用户在微信端**只收得到 send_image 回传的图**；claude 存到 `data/media/` 的文件用户看不到（除非经 send_image 走 outbox 出站）。
- 覆盖场景：`chrome-devtools` 的 `take_screenshot`、`playwright` 的 `browser_take_screenshot`、`browser_run_code_unsafe` 生成的截图、任何用 Write 落盘的 png/jpg/gif/webp。
- 流程：工具截图落盘 → 立即调 `send_image(path, caption=简短说明)` 回传 → 再文字总结。caption 写一句图里关键内容（用户先看文字再看图）。
- **双层落实（真机实证 2026-08-20）**：本节 CLAUDE.md 软指令单独**不够**——截图场景模型默认"存盘+描述"三次压过约定；runner 对每个非斜杠任务 prompt 末尾**强制注入**环境约定后缀（[worker/runner.py](worker/runner.py) `_PROMPT_SUFFIX`，斜杠命令转发不加防破坏命令解析），任务级 prompt 遵循度最高。

## 实现顺序（勿颠倒依赖）

- **M1（MVP）✅**：SQLite schema → gateway 收发+落盘去重 → worker 调 `claude -p`（会话绑定、stream 解析、节流推送）→ 命令总线 → 崩溃恢复 → E2E。
- **M2 ✅**：审批（`--permission-prompt-tool` **实测在 2.1.233 仍存在可用**——注意 `--help` 不列全 flag，勿以 help 缺失判断移除）→ `--bg` 长任务（启动 `claude --bg "<prompt>"` 返回任务 id；轮询 `claude agents --json`；停止 `claude stop <id>`；`claude logs` 是 TUI 流不可解析）→ MCP 装载（chrome-devtools/context7/web-reader；tesseract-ocr/ai-vision 推迟 M3）→ 配置代理命令全套（/permissions 读写、/mcp 列表+启停、/config 概览+set）→ `/policy` strict 档审批 → `/sessions` → 监控告警。已移交：kill 需进程组（MCP 孙进程继承管道）、出站按页计数熔断。
- **M3 ✅**：媒体收发（图片双向，CDN AES-128-ECB）代码完成并**真机验收通过**（2026-08-19，spec §5 五项全过；协议源 @tencent-weixin/openclaw-weixin v2.4.6 dist）；验收期修正：出站 aes_key 形态、bg watcher 三终态、bg 摘除 mcp-config、resume 恒 fork、取结果 prompt 逐项列出。余项 A（/mcp 启停 + /config 写入）与余项 B（daoyu-ocr）均已完成（2026-08-19），M3 闭环。

## 开放问题（涉及前先实测，勿凭假设实现）

TRD §11 登记的未决项：~~`/init` 在 headless 下的确切行为~~（已按设计消解：不依赖它，以 `system/init` 的 `slash_commands` 实清单同步）、~~bypass 档下 `permissions.deny` 是否生效~~（**2026-08-20 实测落定：不生效**，`--disallowedTools` 兜底确证有效，结论见 `.superpowers/sdd/bypass-deny-research.md`）、~~微信文本单条长度上限~~（已实测 16384 字节，[common/text.py](common/text.py) 字节硬闸）、~~Claude Code 版本漂移~~（已机制化：启动版本探测 + `EXPECTED_CLAUDE_VERSION` 基线比对，见 [worker/version.py](worker/version.py)）。M2 新登记的 bg 三项已随 M3 验收（2026-08-19）全部落定：

- ~~**bg completed 条目字段名未采样**~~ → 已采样：终态值是 `done`（非 completed）、条目十字段无输出/cost 字段，取结果靠 `--fork-session` 回原会话（常态路径而非兜底）。
- ~~**`--bg` 与 `--permission-prompt-tool` 组合未实测**~~ → 已落定：bg 不传审批工具（strict 档回执明示）；`--settings` 硬 deny 与 acceptEdits 下 Bash 正常放行均实证；**`--mcp-config` 与 `--bg` 结构性不兼容**（daemon 异步读竞态，已摘除，bg 无 MCP）。
- ~~**bg 停机竞态**~~ → 已落定：按"先落终态者胜"处理 cancel/watcher 双向，真机验收通过（`/cancel` 与 watcher 完结均正常）。
