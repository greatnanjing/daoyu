# 刀鱼 M4：主动服务（定时日报 + 巡检告警）设计

- **日期**: 2026-08-21
- **状态**: 设计已确认（本 session brainstorm 对话结论沉淀），待实现
- **配套文档**: [PRD.md](../../PRD.md) / [TRD.md](../../TRD.md)
- **背景**: M1/M2/M3+收尾批完成后（359 测试全绿、开放问题全清），用户选定四个后续方向（通知通道事件接入 / 主动服务 / 输入体验增强 / 媒体二期）中**主动服务优先**。本 spec 覆盖主动服务全量；通知通道、体验增强、媒体二期为后续独立立项。

---

## 1. 背景与决策记录

| 问题 | 结论 |
|---|---|
| 功能范围 | **每日日报 + 巡检告警**两类；通用定时任务（cron 表达式 + 任意 prompt）不做——两者已覆盖实际需求，留待将来有真需求再扩展 |
| 日报板块 | 任务与费用统计、服务器健康快照、刀鱼自身运行指标三板块；**不含 git 提交摘要**（用户未选） |
| 巡检项 | 磁盘阈值、CPU/内存持续超载、刀鱼自身健康、证书到期预警（四项全选） |
| 日报生成方式 | **模板为主 + 异常升级**：纯 Python 拼模板零 token 成本、秒推；仅数据异常（昨日失败任务/死信新增/健康项超阈）才自动建 Claude 分析任务附结论 |
| 巡检异常处置 | **告警推送 + 自动 Claude 分析**：半夜出事早上看结论；静默期防重复告警重复烧钱 |
| 管理命令 | `/cron` 单命令族；开关/时间/间隔存 DB **即时生效**（不重启） |
| 架构 | 进程内第五常驻协程 `scheduler_loop`。弃系统 crontab 方案（配置分散系统层、微信开关绕远）；弃 bg 复用方案（bg 无 MCP、strict 下受限、非调度器职责） |
| 分析任务会话 | **专用 ops 话题**（固定 UUID session）：分析历史聚一处可 `/cd` 翻阅、Claude 有先前分析上下文（同类异常能"与上次比"）、不污染正常工作话题 |
| 死信告警分工 | 巡检**不重复**查死信——M2 已有死信即时告警专责；巡检只查队列积压与连接状态复核 |

## 2. 总体架构

[gateway/app.py](../../../gateway/app.py) `main_async` 加第五常驻协程（现有四个不动）：

```
worker-pool / outbound / ReconnectTimer / poll_loop   ← 现有
scheduler_loop                                         ← 新增 gateway/scheduler.py
```

`scheduler_loop(db, cfg, outbound)` 每 60s 对齐整分醒来，**每轮现读 cron_jobs 表**决定本轮动作（`/cron` 改配置即时生效无需通知机制）：

```
每分钟醒来：
  ├─ 轻量采样（总是做）：CPU%/内存% 瞬时值 → 内存滚动窗口（供持续超载判定）
  ├─ daily 到点（time_of_day）→ run_daily_report()
  ├─ patrol 到点（last_run_at + interval_min）→ run_patrol()
  └─ 都没到 → 直接睡
```

关键约束：

- **scheduler 不自己跑 Claude**——分析任务经现有 `db.create_task(message_id=None, session_id=<ops>, kind="chat")` 入队，worker-pool 照常调度（预算闸、审批档、进度节流全部自然生效）。
- 推送经现有 `db.enqueue` 出 outbox，与死信告警同通道、发全部白名单用户（[gateway/outbound.py](../../../gateway/outbound.py) 已有广播先例）。
- **ops 话题**：首次需要时 ensure 一行 session（每个白名单用户一行、`cwd=default_cwd`、专用固定 UUID、标签「🔧 巡检分析」）；出现在 `/sessions` 列表，可 `/cd` 进入、可 `/delete`（删了下次自动重建）。
- 协程自保护：整轮 try/except 记 audit，不杀协程——调度器死 ≠ 通道死。

## 3. 数据模型与配置

### 3.1 新表 cron_jobs（ensure_schema 建表 + 预置两行）

```sql
CREATE TABLE IF NOT EXISTS cron_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,      -- 'daily' | 'patrol'
  enabled INTEGER NOT NULL DEFAULT 1,
  time_of_day TEXT,               -- daily 用：'08:00'
  interval_min INTEGER,           -- patrol 用：10
  last_run_at INTEGER,            -- 下次运行时间的计算依据 + /cron 呈现
  last_result TEXT                -- 上次结果一句话（正常/异常/推送几条）
);
-- INSERT OR IGNORE 预置：daily('08:00') + patrol(10)
```

### 3.2 配置分工（调整频率决定存放位置）

| 配置 | 位置 | 调整方式 |
|---|---|---|
| 开关、日报时间、巡检间隔 | `cron_jobs` 表 | `/cron` 即时生效 |
| `disk_threshold_pct`（85）、`cpu_threshold_pct`（90）、`mem_threshold_pct`（90）、`load_sustain_min`（5）、`cert_warn_days`（14）、`cert_paths`（`["/etc/letsencrypt/live"]`）、`alert_silence_h`（6）、`queue_backlog_warn`（20） | `config.json` 新增 `cron` 节 | `/config set`（需将 `cron.*` 键加入现有 set 白名单，重启生效） |

### 3.3 运行态 KV 与内存状态

- 静默期：state KV `cron_alert:<item_key>` = 上次告警时间戳；item_key 粒度 `disk:/`、`cpu`、`mem`、`cert:<域名>`、`queue`。
- CPU/内存滚动采样窗口：scheduler 协程内存变量（不落盘——写放大不值；进程重启清零重新计）。

### 3.4 新依赖

`psutil`（pyproject dependencies + Linux 服务器 `pip install`）——CPU/内存/磁盘跨平台读取；开发机 Windows 与生产 Linux 都要跑测试，读 `/proc` 方案不可行。

## 4. 日报链路（run_daily_report）

```
1. 收集（纯 Python，统计窗口 = 昨日 00:00~24:00 本地时区）
   📊 任务与费用：tasks 表按 state 分组计数（成功/失败/死信）
                  + audit_log kind='cost' 昨日行 sum(usd)（费用记账现位置，
                    tasks 表无 cost 字段——实测确认）
   🖥 服务器：psutil —— CPU% / 内存% / 磁盘%（default_cwd 所在分区）/ 开机时长
   🐟 刀鱼自身：outbox 昨日发送条数、当前队列积压（pending+running）、
                死信总数、iLink 连接状态（DB token 线索）、media 目录体积
2. 拼模板 → enqueue（发全部白名单，秒推）：
   🌅 刀鱼日报 2026-08-21
   📊 任务：昨日 5 个（成功 4 / 失败 1 / 死信 0），费用 $0.83
   🖥 服务器：CPU 23% / 内存 61% / 磁盘 42%，已运行 12 天
   🐟 刀鱼：出站 32 条 / 队列 0 / 死信 0 / 连接正常
3. 异常升级判定（任一命中即触发）：
   · 昨日失败任务 > 0 或 死信新增 > 0
   · 健康快照任一项超巡检同款阈值
   → 模板末尾追加"⏳ 检测到异常，分析进行中…"
   → 建 Claude 分析任务（挂 ops 话题）：prompt 附数据 +
     "查看 audit_log / 失败任务上下文，分析原因给出简要结论"
   → 分析结果经 worker 正常管道后续推送
```

**模板先推、分析后到**（不拼接等待）：符合"gateway 永不阻塞"哲学；分析任务失败日报照样在。ops 话题 policy 默认 `auto`（只读分析足够），用户可 `/cd` 进去改档。

## 5. 巡检链路（run_patrol）

**两级节奏**（scheduler 每分钟统一驱动）：

- **每分钟轻量采样**：CPU%/内存% 入滚动窗口；**连续 `load_sustain_min`（默认 5）个采样超阈值**才判"持续超载"——瞬时尖峰（一次编译/cron job）不误报。
- **每 `interval_min`（默认 10min）完整巡检**：

| 检查项 | 实现 | 告警条件 |
|---|---|---|
| 磁盘 | psutil.disk_usage——`/` 与 default_cwd 所在分区（去重） | > 85% |
| CPU/内存 | 复核滚动窗口 | 连续 5 个采样 > 90% |
| 刀鱼自身 | 队列积压 pending+running > 20（worker 疑似卡死）；iLink token 缺失（连接复核；断连即时告警已有专责） | 命中即报 |
| 证书 | `cert_paths` 扫 *.pem 读 NotAfter（cryptography 已在依赖） | 剩余 < 14 天；路径不存在跳过不误报（Windows 开发机） |

**异常处置**（本轮全部异常项合并）：

```
├─ 推告警（发全部白名单）：
│    ⚠️ 巡检告警 [磁盘]
│    / 分区 91%（阈值 85%）
│    ⏳ 已建分析任务，结论稍后推送
├─ 静默期：同类 item_key 6h 内不重报（state KV）；
│    静默期后仍异常再报一次（防"告一次永远沉默"）
└─ 建 Claude 分析任务（挂 ops 话题；多项异常合并为一个任务，
   prompt 列出全部异常数据 + 分析指令）——静默期内不重复建
```

**零成本原则**：正常轮次纯 Python 判断零 Claude 调用；只有异常才花一次调用，静默期 + 合并建任务双重控制异常期花费。

## 6. /cron 命令

注册：[gateway/router.py](../../../gateway/router.py) `BRIDGE_COMMANDS` + [gateway/bridge.py](../../../gateway/bridge.py) `execute_bridge` 分支 + `BRIDGE_HELP` 文案。

```
/cron                    列表：
                         📅 daily  ✅ 每天 08:00（下次：明天 08:00）
                            └ 上次 08-21 08:00 · 正常，推送 1 条
                         🔍 patrol ✅ 每 10 分钟（下次：10:32）
                            └ 上次 10:20 · 正常
/cron on|off <daily|patrol>        开/关（即时生效）
/cron time daily <HH:MM>           调日报时间
/cron interval patrol <分钟>       调巡检间隔（最小 1）
```

下次运行时间现算：daily = 今天该时刻（已过则明天）；patrol = `last_run_at + interval_min`（enabled 后从当前时刻起算）。非法参数回用法提示（对齐现有桥命令错误风格）。

## 7. 错误处理

| 故障点 | 行为 |
|---|---|
| scheduler 协程自身异常 | 整轮 try/except → 记 audit，不杀协程，下轮重来 |
| psutil / 证书读取单项失败 | 该项跳过 + 记 `last_result`，不中断其他项 |
| 分析任务失败 | 走现有任务管道重试/死信，无新逻辑 |
| 日报/告警推送失败 | 走现有 outbox 重试/死信，无新逻辑 |
| ops 话题被 /delete | 下次需要时 ensure 自动重建 |

## 8. 测试

新增 `tests/test_scheduler.py`：

- **单元**：模板拼装、阈值判定、连续采样窗口（不足 N 个不告警）、静默期去重（6h 内同 key 不重报、过期重报）、next-run 计算（daily 已过/未过、patrol 间隔）、`/cron` 子命令解析与 DB 读写（含非法参数回提示）。
- **时间可注入**：scheduler 判定函数接受 `now` 参数（不写死 `time.time()`），假时钟推进驱动。
- **psutil mock**：测试注入假值（磁盘 91% 等），不依赖真机状态。
- **E2E**：假时钟推进 → daily 到点 → outbox 出现日报行；异常注入 → 出现告警行 + tasks 表出现挂 ops 话题的分析任务；`/cron off` 后推进 → 无新行；正常轮次 audit_log 无新增 cost 行（零成本验证）。

## 9. 改动面与验收标准

**改动面**：新文件 `gateway/scheduler.py`；修改 [common/db.py](../../../common/db.py)（cron_jobs 表 + 预置 + 读写方法）、[gateway/router.py](../../../gateway/router.py) / [gateway/bridge.py](../../../gateway/bridge.py) / [gateway/app.py](../../../gateway/app.py)（三处接入）、gateway/config.example.json + [common/config.py](../../../common/config.py)（`cron` 节 + set 白名单扩展）、pyproject.toml（+psutil）。

**验收标准**：

1. `/cron` 列表呈现两任务状态与下次运行时间；on/off/time/interval 即改即生效。
2. 到点收到日报，含三板块（任务与费用 / 服务器健康 / 刀鱼自身），无异常时不建任何 Claude 任务（零成本）。
3. 磁盘超阈（mock 或真机）→ 收到告警 + ops 话题出现分析任务 + 分析结论后续推送。
4. 同类异常静默期内不重报；静默期后仍异常会再报。
5. CPU 瞬时尖峰（不足连续 N 采样）不告警。
6. 全量测试绿（现有 359 + 新增）。

## 10. 真机验收结论（2026-08-21）

验收通过（383 测试全绿 + 生产服务器 + 微信真机）：

1. `/cron` 列表 / on / off / time / interval 即改即生效 ✅（验收期实机操作）。
2. 日报到点推送、三板块完整 ✅（outbox 393）；当日含异常 → 自动升 Claude 分析、挂 ops 话题、结论推送全链路三证 ✅（outbox 409-411）。
3. 正常轮次零 Claude 调用 ✅（patrol `last_result=正常` 轮转、无任务产生）。
4. 同类异常静默期（state KV `cron_alert:<key>`，6h）与 CPU 瞬时尖峰不告警（`load_sustain_min` 连续采样窗口）：单测钉死 + 真机运行期零误报佐证。
5. 全量测试绿 ✅（383，M4 时点基线）。

**验收期首 bug（已修 9bc627f）**：ops 话题分析任务进程中断后重入异常——`ensure_ops_session` 检测 OPS_UUID transcript 在场即置 inited 转 `--resume`。教训：任何「固定 uuid 首建会话」路径都要防中断重入（与 /adopt 同款硬约束）。
