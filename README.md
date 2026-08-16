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

`claude/mcp.json`（MCP server 清单，进 git）内置条目为 Windows 形态（`command: "cmd"` + `args: ["/c", "npx", …]`）；Linux 服务器部署时需把各条目的 `command` 改为 `npx` / `uvx`、`args` 去掉 `/c` 前缀，并确认已装 Node.js（含 npx）与 [uv](https://docs.astral.sh/uv/)（提供 uvx）。

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
| 桥命令（本地秒回） | `/tasks` | 查看 running/pending 任务 |
| | `/status` | 队列深度、死信数、当日费用、连接剩余时间 |
| | `/cancel <任务号>` | 取消任务 |
| | `/cd <目录>` | 切换工作目录（= 换绑另一个 Claude 会话；无参查看当前与历史） |
| | `/policy <auto\|strict\|bypass\|plan>` | 查看或切换权限档位 |
| iLink 运维 | `/help` | 全部可用命令（按实际能力动态生成） |
| | `/time` | 连接剩余时间 |
| | `/重新连接` | 立即重新扫码连接（Y/N 确认） |
| 转发 | `/review`、`/compact` 等 | Claude Code headless 可用的斜杠命令原样转发执行（可用集从 `system/init` 事件同步缓存） |
| 对话 | 任意文本 | 直接作为 prompt 发给当前会话的 Claude |

典型流程：发「你好」→ 秒回「✅ 收到，处理中」→ 工具执行时推送「🔧 工具名」进度 → 最终回复（超长自动分页）。

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
python -m pytest                        # 全量测试（89 个）
python -m pytest tests/test_e2e.py -v   # E2E：fake iLink + fake claude 子进程全链路
python -m gateway.app                   # 前台调试运行（不进 systemd）
```

```
├── gateway/   # app 入口 / ilink 协议 / router 命令路由 / bridge 桥命令 /
│              # outbound 出站节流重试 / reconnect 24h 连接守护 / login 扫码
├── worker/    # pool 会话串行调度 / cli_builder argv 组装 / runner 子进程执行 / stream 解析
├── common/    # db（SQLite 五表+state KV）/ config / models / text（分页）
├── claude/    # settings.json + mcp.json（进 git）、secrets.env（gitignore）
├── tests/     # 单测 + E2E（fixtures/fake_claude.py 模拟 claude 子进程）
├── deploy/    # daoyu.service（systemd 单元）
└── docs/      # PRD / TRD
```

## M1 边界（当前版本不包含，勿过度期待）

- **strict 档审批推送**：`--permission-prompt-tool` 已从 claude CLI 移除，M2 重选方案；M1 的 strict 与 auto 同为 `acceptEdits` 基线。
- **`--bg` 长任务**：M2（后台任务管理为 `claude agents --json`）。
- **MCP server 装载**：`claude/mcp.json` 目前为空清单，M2 接入。
- **TUI 配置代理命令全套**（/permissions /hooks /plugins /login /config /mcp 等）：M1 仅提示，M2 提供文字版代理。
- **监控告警渠道**：M2（`/status` 已可查当日费用与死信数）。
- **媒体收发**（图片/语音）：M3。
