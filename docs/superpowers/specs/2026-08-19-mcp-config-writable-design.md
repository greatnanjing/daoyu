# 刀鱼 M3 余项 A：/mcp 启停与 /config 写入设计

- **日期**: 2026-08-19
- **状态**: 已实现并真机验收通过（2026-08-19 生产服务器：/mcp 启停 audit 实证、off 后任务不炸、/config set 重启后运行值=文件值，见 progress 台账余项 A 段）
- **配套文档**: [PRD.md](../../PRD.md) / [TRD.md](../../TRD.md) / [M3 media spec](2026-08-19-m3-media-design.md)（§1 划出的独立小项目之一）
- **姊妹篇**: [OCR MCP spec](2026-08-19-ocr-mcp-design.md)（M3 余项 B，本项完成后开发）

---

## 1. 背景与决策记录

M3 媒体收发验收通过后，spec §1 划出的两项余项之一。现状（M2 Task 5）：/mcp 与
/config 均只读（[gateway/proxy.py](../../gateway/proxy.py)）。本 session 拍板：

| 问题 | 结论 |
|---|---|
| /mcp 做到什么程度 | **启停式**：列表 + on/off，停用不丢配置（标记状态而非删条目）；增删条目仍改文件 |
| /config 开放哪些键 | **常用键集**：throttle 四键 + budget 两键 + worker.concurrency；whitelist 等其余提示改文件（whitelist 从微信改 = 放别人进服务器，安全不开放） |
| 启停状态存哪 | mcp.json **顶层 `"disabled": []`**（与 mcpServers 平级；配置单一事实源，不拆 DB） |
| 平台形态矛盾 | mcp.json 改**平台无关形态**，runner 合并层按平台展开（§3.1） |
| 生效时机 | /mcp 启停对**下一任务即生效**（每次任务重写临时 mcp config）；/config 需重启（Config 启动时加载） |
| 热重载 | **不做**（YAGNI：单用户产品 kill -TERM 重启几秒；Config dataclass 引用散布各协程，热重载复杂度不值当） |

**顺带修复的欠账**：现 mcp.json 是 Windows 形态（`cmd /c npx`），生产 Linux 的
`cmd` 不存在——生产 MCP 实际从未跑通（CLAUDE.md M2 清单已登记「Linux 部署改直用
npx/uvx + 冷缓存预热」）。本项的平台无关化正是解此欠账。

## 2. 数据模型

### 2.1 mcp.json 新形态（平台无关 + 启停标记）

```json
{
  "mcpServers": {
    "chrome-devtools": {"type": "stdio", "command": "npx",
                        "args": ["chrome-devtools-mcp@latest"], "env": {}},
    "context7":       {"type": "stdio", "command": "npx",
                        "args": ["-y", "@upstash/context7-mcp"], "env": {}},
    "web-reader":     {"type": "stdio", "command": "uvx",
                        "args": ["--with", "mcp~=1.0", "mcp-server-fetch"], "env": {}}
  },
  "disabled": ["web-reader"]
}
```

- 条目直写 `npx` / `uvx`（**去掉 Windows 的 `cmd /c` 包装**），静态文件两端通用。
- `"disabled": []` 顶层键，缺省视为空。旧文件（无 disabled 键）幂等兼容。
- **claude 不会读到 disabled 键**：传给 claude 的是 runner 合并生成的临时文件
  （只含 mcpServers），静态 mcp.json 从不直接进 `--mcp-config`——disabled 键零风险，
  无需实测 claude 对顶层未知键的容忍度。
- disabled 数组里的名字若已不在 mcpServers（改名/删条目后残留），合并层静默忽略，
  /mcp 列表显示时跳过——不报错不清理（用户下次 on/off 自然覆写）。

### 2.2 展开层（runner 合并时按平台包装）

[worker/runner.py](../../worker/runner.py) `_write_daoyu_mcp_config` 读静态清单后：

- **Windows** 且 `command ∈ {"npx", "uvx"}`（白名单，可扩展常量）→ 展开为
  `cmd /c <command> <args...>`（npm 的 npx/uvx 在 Windows 是 .cmd shim，
  `create_subprocess_exec` 直启会 FileNotFoundError）。
- **Linux** 直传（npx/uvx 全局命令本身可执行）。
- 其余 command（如 B 项注入的 `sys.executable` 绝对路径 python）两平台都直传，
  不在白名单不包装。
- disabled 条目在此层过滤（进不了临时文件 = 对 claude 而言该 server 不存在）。

### 2.3 gateway/config.json 写入

不改变文件结构（[common/config.py](../../common/config.py) 契约不动），只在
proxy 层对白名单键做 set。**白名单与校验规则**：

| 键 | 类型 | 合法范围 |
|---|---|---|
| throttle.min_send_interval_s | float | > 0 |
| throttle.progress_window_s | float | > 0 |
| throttle.page_char_limit | int | ≥ 200 |
| throttle.daily_send_limit | int | ≥ 1 |
| budget.max_turns | int | ≥ 1 |
| budget.max_usd | float | > 0 |
| worker.concurrency | int | 1 ~ 10 |

范围外的键（whitelist / default_cwd / claude_bin / reconnect.* / secrets）set 时
拒绝并提示「该键不开放微信修改，请直接改 gateway/config.json」。

## 3. 设计

### 3.1 命令语法

```
/mcp                        → 列表：序号 + 名字 + command + ✅启用/⛔停用 + 用法行
/mcp off <序号|名字>         → 停用（写 disabled，原子写 + audit）
/mcp on  <序号|名字>         → 启用（移出 disabled）
/config                     → 现状展示（增强：尾附用法行）
/config set <键> <值>        → 白名单键写入（类型+范围校验，原子写 + audit）
```

- 序号 1-based，与 /mcp 列表显示一致（照 /permissions del 先例）；序号与名字都收，
  名字优先精确匹配。
- `/config set` 回执明示「已写入 gateway/config.json，**重启生效**（systemctl
  restart daoyu）」；/mcp on/off 回执明示「下一任务生效」。
- audit_log 记 `config_change`（照 /permissions 先例：`mcp off web-reader` /
  `config set throttle.page_char_limit=1500`）。
- bg 任务无 MCP（M3 定论），启停对 bg 无效——/help 与回执不特别区分（bg 回执本就
  明示无 MCP）。

### 3.2 模块改动

| 位置 | 改动 |
|---|---|
| [gateway/proxy.py](../../gateway/proxy.py) | `_mcp` 加 on/off 子命令；新 `_config_set`；抽公共 `_atomic_write_json(path, data)`（`_save_settings` 改为其调用方） |
| [worker/runner.py](../../worker/runner.py) | `_write_daoyu_mcp_config`：读静态清单 → 过滤 disabled → 平台展开（§2.2）→ 合并 daoyu 条目 |
| [worker/cli_builder.py](../../worker/cli_builder.py) | 白名单常量 `_WINDOWS_WRAP = {"npx", "uvx"}` 与展开函数（纯函数可单测）放这 |
| [gateway/bridge.py](../../gateway/bridge.py) | `PROXY_HELP` 的 mcp / config 两行更新用法 |
| `claude/mcp.json` | 改平台无关形态（§2.1 示例，disabled 初始为空） |
| [docs/superpowers/plans/](../plans/) 与部署 | Linux 冷缓存预热步骤（部署后手动跑一次各 npx/uvx 包确保缓存命中） |

### 3.3 错误处理

- 序号越界 / 名字不存在 → 提示当前清单（照 /permissions del 越界先例）。
- 键不在白名单 / 值类型错 / 范围越界 → 各自明确报错 + 用法行，不改文件。
- mcp.json / config.json 顶层不是对象 → 沿用 NotJsonObjectError 先例。
- 原子写失败（磁盘满等）→ 异常上抛由 execute_proxy 兜底（现状结构），不留半写文件。

## 4. 测试策略

| 层 | 内容 |
|---|---|
| 展开层单测 | Windows npx/uvx 包 cmd /c、Linux 直传、白名单外 command 不包装、disabled 过滤、disabled 残留名静默忽略 |
| /mcp 单测 | 只读列表（含状态标记）、on/off 按序号与名字、重复 off 幂等提示、越界/未知名报错、原子写后文件内容断言 |
| /config set 单测 | 白名单七键全过一遍（类型+边界值）、范围外键拒绝、坏类型拒绝、文件内容断言、只读展示不变 |
| 路由 | /mcp off 等带子命令经 proxy 分发（照 /permissions deny add 先例） |
| E2E | 可选：/mcp off 后下一任务 argv 的临时 mcp config 不含该条目（fake claude 断言） |

## 5. 待实测清单（真机）

1. **Windows 直启 npx 确认**：白名单包装的必要性实证（不包装时
   create_subprocess_exec 是否 FileNotFoundError——预期是，实测后登记）。
2. **Linux 生产 MCP 首通**：平台无关 mcp.json + 冷缓存预热后，生产任务里
   chrome-devtools / context7 / web-reader 三台 connected（M2 欠账清偿）。
3. **on/off 即时性**：/mcp off 后下一任务的临时 mcp config 已过滤（audit 或
   fake 断言）。

## 6. 实现后需同步的文档勘误

- CLAUDE.md：M2 清单「MCP 装载」行（Windows 形态注记 → 平台无关 + 展开层）、
  代理命令行（/mcp 启停、/config 写入）
- README：/mcp /config 只读边界描述更新
- TRD §207 表：「/config /mcp 只读」→ 读写口径（引用本 spec）
