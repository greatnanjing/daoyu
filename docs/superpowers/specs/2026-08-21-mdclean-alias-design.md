# 刀鱼 M5C2+M5C3：出站 Markdown 清洗 + 快捷命令 设计

- **日期**: 2026-08-21
- **状态**: 设计定稿（brainstorm 2026-08-21，两子项方案均经用户确认）
- **配套文档**: [PRD.md](../../PRD.md) / [TRD.md](../../TRD.md) / [M5C1 spec](2026-08-21-input-merge-design.md)
- **背景**: M5C「输入体验增强」方向后续两子项（M5C1 spec 预告）。M5C2 解决 Claude 回复的 Markdown 符号在微信纯文本下裸露（`**`、`##`、`` ``` ``）的可读性问题；M5C3 让常用命令与常用长 prompt 一个词可达。

---

## 1. 背景与决策记录

| 问题 | 结论 |
|---|---|
| 清洗力度 | **积极转写**（brainstorm 选定）：标题→【】、行内代码→「」、链接→`文字(url)`、表格→竖排；非"只删符号" |
| 清洗位置 | **投递前清洗（outbound.py）**而非源头（runner/pool 各出口）——单点覆盖任务回复/notify body/caption/进度行全部文字出口，outbox 恒存原始 Markdown（规则升级后死信重投自动受益），worker 侧零改动 |
| 清洗与分页顺序 | **清洗在 `split_text` 之前**（`_drain_once` 文本行）——分页后清洗会让单页增量（表格转置/【】/缩进）越过 `MAX_PAGE_BYTES=15000` 字节硬闸，16384B 静默丢消息 |
| 出站计数折算口径 | 折算（`outbox_sent_pages`）文本行**同过 md_clean**——运行时/重启/日界/`/status` 四处口径一致（"三处必须同一折算"硬约束的延伸）；折算仍是近似值（重试重发固有偏差，方向偏保守） |
| 清洗开关 | `throttle.md_clean`（bool，默认 true，进 /config set 白名单，重启生效）——清洗误伤时一键回退原文直发 |
| 别名形态 | **内置短别名 + 用户自定义**（brainstorm 选定"两者都要"）：内置 `/t /s /c /cs`；`/alias add/del/list` 管理自定义 |
| 别名展开时机 | **双层**：用户别名在 [gateway/app.py](../../../gateway/app.py) `handle_inbound` 内 route **前**展开（需查 KV）；内置别名在 [gateway/router.py](../../../gateway/router.py) `route()` 开头静态映射（纯函数不破坏）。用户层先于内置层——同名时用户定义覆盖内置（brainstorm 确认） |
| 展开后行为 | 展开结果**重新 route 一次**（不再展开，防链式循环），与用户直接发该文本完全一致：chat → 进 M5C1 合并窗口、bridge → 秒回、forward → 转发。零特判路径 |
| 别名存储 | state KV `alias:<user>` 单键 JSON dict（`merge_pending:<user>` 同构先例），崩溃天然持久 |
| 撞名规则 | 禁与桥/运维/代理集合及 `alias` 自身撞（防自毁管理入口）；撞内置别名（t/s/c/cs）**允许**=覆盖（用户显式意图）；撞 Claude 动态 slash_commands 允许但回执提示重名 |
| `_x_` 斜体不做 | snake_case（`my_var_name`）误伤风险高、Claude 中文回复极少用 `_斜体_`——明确不做（`\*` 形态才处理） |

## 2. 总体架构

```
M5C2（出站）：
  outbox 文本行 ──→ _drain_once: split_text(md_clean(text)) ──→ _send 分页投递
  outbox caption ──→ _send_media/_send_file_media: md_clean(caption) ──→ _send 单发
  折算: outbox_sent_pages 文本行同过 md_clean（四处口径一致）

M5C3（入站）：
  斜杠消息到达（handle_inbound）
    ├─ 用户别名 KV 命中？ ──→ 展开为「值 + 空格 + 附加参数」→ 对展开结果 route（一次）
    └─ 未命中 → route() →（内置映射 /t→tasks …）→ 正常四类分发
```

关键约束：

- md_clean 是**纯函数**（仅 stdlib re），无 Markdown 模式的文本原样返回——系统回执（✅ 模板行、⚠️ 告警）天然无损。
- md_clean **幂等**：转换产物（【】、「」、•、｜）不再构成 Markdown 输入模式，重复清洗不变（防御性正确）。
- 别名展开**仅一层**：展开结果即使是斜杠也不再查 KV——`/a` 展开为 `/b`、`/b` 又定义指向 `/a` 的链式循环不存在。
- 入站落盘（messages.text）存**原始** `/go`，create_task 的 prompt 用**展开后**文本——审计看用户发了什么、任务看 Claude 收到什么。

## 3. 组件设计

### 3.1 `common/mdclean.py`（新模块）

`md_clean(text: str) -> str`，纯函数。处理管线：

1. **fenced 代码块切块保护**：``` 或 ~~~ 围栏（可带语言名）之间的内容**原样保留**（代码里 `**` 是 glob 语义、`#` 是注释——绝不能清洗）、整体每行缩进 4 空格、去围栏行与语言名。块外内容进后续步骤。
2. **块级规则**（逐行）：

| 输入 | 输出 | 备注 |
|---|---|---|
| `#{1,6} x`（标题） | `【x】` | 井号数不区分层级 |
| `(\s*)[-*+] x`（无序列表） | `\1• x` | 嵌套缩进保留 |
| `> x`（引用） | `｜ x` | 连续多个 `>` 各转一个 ｜ |
| `(-{3,}\|\*{3,})$`（水平线） | `—————————` | 单独成行才匹配（`- x` 列表项不受影响） |
| `\d+\. x`（有序列表） | 原样 | 本就可读 |
| 表格块 | 见下 | 连续 ≥2 行 `\|…\|` 且第二行为 `\|---\|` 分隔行 |

3. **表格转写**：识别 header 行 + `\|---\|` 分隔行（分隔行单元格可带冒号对齐形态 `:---:`/`---:`，对齐语义丢弃）+ 数据行。
   - **两列且恰一个数据行**（参数表形态）：转置键值竖排 `• h0：v0`、`• h1：v1`
   - **其余**（两列多行 / N 列）：删分隔行，header 与数据行统一 `• c0 ｜ c1 ｜ …`
4. **行内规则**（块级转换后逐行，顺序敏感）：
   1. 行内代码 `` `x` `` → `「x」`（内容先占位提取，后续规则不碰「」内文字，最后还原）
   2. 粗体 `**x**` → `x`
   3. 斜体 `*x*` → `x`（要求成对且两侧紧贴非空白——`3 * 4 * 5` 空格包裹不误伤）
   4. 删除线 `~~x~~` → `x`
   5. 链接 `[t](u)` → `t(u)`；图片 `![a](u)` → `图片 a(u)`
   6. 行内代码占位还原；反斜杠转义 `\x` → `x`（最后）

### 3.2 `gateway/outbound.py` + `common/text.py`（清洗应用与折算）

| 项 | 设计 |
|---|---|
| `_mdc(self, text)` 私有 helper | `throttle.md_clean` 为 true 时返回 `md_clean(text)` 否则原样；三处调用共用 |
| `_drain_once` 文本行 | `pages = split_text(self._mdc(item.text), limit)`——**先清洗后分页**（字节硬闸在清洗后文本上计算） |
| `_send_media` / `_send_file_media` | `caption = self._mdc((item.caption or "").strip())`——caption 单发不分页，本身短、无字节风险 |
| `_send` | **不动**（清洗在数据准备处完成，_send 保持纯发送） |
| `common/text.py` `outbox_sent_pages` | 签名加 `md_clean_enabled: bool = True`；文本行 `split_text(md_clean(text) if on else text)`——`common.text → common.mdclean` 单向依赖无环 |
| `common/db.py` `sent_pages_today` | 签名加 `md_clean_enabled: bool = True` 透传 |
| 调用方 | `OutboundLoop.__init__` / 日界重算 / bridge `/status` 折算三处统一传 `bool(cfg.throttle.get("md_clean", True))` |

### 3.3 配置

- `common/config.py` `_DEFAULT_THROTTLE` 加 `"md_clean": True`；`gateway/config.example.json` throttle 节同步。
- [gateway/proxy.py](../../../gateway/proxy.py) `/config set`：`CONFIG_KEYS` 加 `"throttle.md_clean"`——现有 `_is_int`/`_is_float` 预校验框架需加 **bool 分支**（值认 `true/false`，parser 转 Python bool，JSON 写回 true/false）。`/config` 概览行补该键显示。

### 3.4 `gateway/router.py`（内置别名）

```python
BUILTIN_ALIASES = {"t": "tasks", "s": "status", "c": "cancel", "cs": "sessions"}
```

`route()` 在 `if not name:` 判空后、BRIDGE_COMMANDS 判定前：`name = BUILTIN_ALIASES.get(name, name)`。映射目标全部是 BRIDGE_COMMANDS 成员，args 原样跟随。unknown 建议池不含用户别名（route 是纯函数拿不到 KV，YAGNI）。

### 3.5 `gateway/app.py`（用户别名展开）

模块级函数，route 调用前插入（[app.py](../../../gateway/app.py) `handle_inbound` 内 slash 清单读取之后）：

```python
def _expand_alias(db, from_user: str, text: str) -> str | None:
    """用户别名展开（M5C3）：仅斜杠消息；KV 命中返回「值 + 空格 + 附加参数」，
    未命中/非斜杠/坏 JSON 返回 None。展开结果不再二次展开（调用方只调一次）。"""
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None
    parts = stripped[1:].split(None, 1)
    if not parts:
        return None
    raw = db.get_state(f"alias:{from_user}")
    if not raw:
        return None
    try:
        aliases = json.loads(raw)
    except ValueError:
        return None                      # 坏 JSON 容错：当无别名，不炸入站
    value = aliases.get(parts[0])
    if not value:
        return None
    args = parts[1] if len(parts) > 1 else ""
    return f"{value} {args}".strip()
```

调用：`text.startswith("/")` 时 `expanded = _expand_alias(...)`，非 None 则 `text = expanded`，随后照常 `route(text, slash)`。Y-N 单字非斜杠、不进此路径。

### 3.6 `gateway/bridge.py`（/alias 命令）

- `BRIDGE_COMMANDS`（[gateway/router.py](../../../gateway/router.py)）加 `"alias"`；`BRIDGE_HELP` 加条目（含内置别名提示）。
- `execute_bridge` 加 `"alias"` 分支 → `_alias(db, arg, from_user)`：

| 子命令 | 行为 |
|---|---|
| 无参 / `list` | 列全部：`/name → 预览(30字)`；空则提示 + 列内置四条 |
| `add <name> <value…>` | 校验 → 写 KV dict → audit `alias_add` → 回执 ✅（覆盖内置别名时附注；撞 slash_commands 时附注重名提示） |
| `del <name>` | 删键 → audit `alias_del`；不存在如实回执 |

校验规则：name 非空、≤16 字符、无空白（split 取词天然保证）；value 非空、≤2000 字符；条数 ≤50；name ∉ `BRIDGE_COMMANDS ∪ ILINK_COMMANDS ∪ PROXY_COMMANDS ∪ {"alias"}`（bridge.py 从 router import 三集合，单向依赖无环）。内置别名 t/s/c/cs **不在禁止集**——可覆盖（用户层先于内置层展开，天然生效）。

### 3.7 文档

README 命令表加 `/alias` 与内置别名、`md_clean` 配置键说明；CLAUDE.md 增 M5C2/M5C3 功能清单（实现完成后随验收状态更新）。

## 4. 测试策略

| 层 | 内容 |
|---|---|
| mdclean 单测（新 tests/test_mdclean.py） | 规则矩阵逐条（标题/粗体/斜体/行内代码/删除线/链接/图片/列表/引用/水平线/表格两形态）；**代码块保护**（块内 `**`/`#` 原样、缩进 4 空格、围栏与语言名去除）；`3 * 4 * 5` 与 snake_case 不误伤；无 Markdown 文本**逐字节不变**（emoji/中文/系统回执模板）；幂等（清洗两遍 == 一遍）；嵌套（标题含粗体、列表含行内代码） |
| outbound 集成 | md_clean on：文本行清洗后分页（断言 fake iLink 收到清洗文本）；off：原文直发；caption 清洗；死信告警 ⚠️ 行含 Markdown 截断片段不炸 |
| 折算 | outbox_sent_pages 开/关参数两态；sent_pages_today 透传 |
| router 单测 | `/t`→tasks、`/cs`→sessions、args 跟随；`/t` 不再匹配 unknown；既有路由零回归 |
| app 层 | _expand_alias：命中展开+附加参数、未命中 None、坏 JSON None、非斜杠 None；展开为 chat 文本进合并窗口、展开为 `/tasks` 走 bridge（E2E） |
| bridge /alias | add/del/list 全回执；name/value/条数校验越界拒绝；撞系统命令拒绝；覆盖内置别名允许+附注；KV 持久（重读生效） |
| E2E | fake claude 回复带 Markdown 全语法 → 微信端断言收到清洗后纯文本；`/alias add go <prompt>` → `/go` 建任务且 prompt 为展开文本；`/t` 等价 `/tasks` |
| 回归 | 既有 434 测试全绿 |

## 5. 风险与真机验收点

| 风险 | 缓解 |
|---|---|
| 正则歧义误伤（`*` 乘号、下划线变量名） | 斜体规则要求两侧紧贴非空白；`_x_` 明确不做；单测矩阵覆盖；`md_clean` 开关一键回退 |
| 表格转写对无 header 语义表格的退化 | 通用形态（• c0 ｜ c1）不丢信息；真机验收看 Claude 实际产出形态 |
| 清洗增量越字节硬闸 | 清洗在 split_text **之前**（硬闸按清洗后文本计算）——结构上杜绝 |
| 微信文本内 URL 是否可点 | 链接转写为 `t(u)` 无论可否点信息无损；真机验收登记观察 |
| 别名展开把 `/help` 等动态命令遮蔽 | 撞 slash_commands 时 add 回执明示重名；用户显式意图优先（单用户产品语义） |
| /config set bool 解析新类型引入回归 | 既有数值键解析路径不动，bool 走独立分支；现有 proxy 测试全量回归 |

**真机验收点**（实现后另行走查）：① Claude 自然回复（含代码块+表格）微信端阅读体验；② `t(u)` 链接在微信端可点性；③ `/alias add go <长 prompt>` → `/go` 全链路；④ `/t`/`/cs` 实机秒回；⑤ `md_clean` 开关切换重启生效。

## 6. 明确不做

- `_x_` / `__x__` 斜体粗体转写（snake_case 误伤，Claude 中文回复极少用）
- HTML 标签剥离（`<br>` 等，低频 YAGNI）
- 用户别名进 unknown 建议池（route 纯函数拿不到 KV）
- 别名多级链式展开（`/a`→`/b`→…，仅一层，防循环）
- 别名跨用户共享 / 导入导出（单用户产品）
- M5C1 合并窗口语义变更（别名展开为 chat 文本照常进窗口，一致性优先）
