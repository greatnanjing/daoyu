# M5C2+M5C3（Markdown 清洗 + 快捷命令）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 出站文本投递前做 Markdown→微信纯文本积极转写（M5C2）；双层快捷命令——内置 `/t /s /c /cs` + 用户自定义 `/alias add/del/list`（M5C3）。

**Architecture:** 新纯函数模块 `common/mdclean.py`（fenced 代码块保护 + 块级/行内转写规则），在 `gateway/outbound.py` 投递前应用（**先清洗后分页**——字节硬闸在清洗后文本上计算）；出站折算口径四处同步过清洗。别名双层展开：用户别名在 `gateway/app.py` route 前查 KV（一层防循环），内置别名在 `gateway/router.py` 纯函数静态映射；`/alias` 为桥命令存 state KV 单键 JSON dict。

**Tech Stack:** Python 3.11+ stdlib（re/json/sqlite3），pytest + asyncio_mode=auto（async 测试无需装饰器）。

**Spec:** [docs/superpowers/specs/2026-08-21-mdclean-alias-design.md](../specs/2026-08-21-mdclean-alias-design.md)（本计划所有设计决策的权威来源，实现与 spec 冲突时停下来报告）

## Global Constraints

- 全量测试命令：`python -m pytest`（Windows 开发机 Git Bash，venv 在 `.venv/Scripts/python`；当前基线 434 个全绿）
- 单测命令形态：`python -m pytest tests/test_<file>.py -v`
- commit 信息用中文 conventional commits（`feat(M5C2): …` / `feat(M5C3): …` / `test: …`）
- md_clean 必须保持：纯函数（仅 stdlib）、幂等（清洗两遍 == 一遍）、无 Markdown 模式文本逐字节不变（系统回执天然无损）
- 别名展开仅一层（展开结果不再查 KV）；展开后行为与直接发该文本完全一致
- 不改 `_send()`（清洗在数据准备处，_send 保持纯发送）

---

### Task 1: `common/mdclean.py` 纯函数 + 测试矩阵

**Files:**
- Create: `common/mdclean.py`
- Test: `tests/test_mdclean.py`（新建）

**Interfaces:**
- Produces: `md_clean(text: str) -> str`——后续 Task 2 的 `outbound.py` / `common/text.py` 直接 import 使用。

- [ ] **Step 1: 写失败测试（完整矩阵）**

创建 `tests/test_mdclean.py`：

```python
"""M5C2 Markdown 清洗：规则矩阵 + 代码块保护 + 无损性 + 幂等。"""
from common.mdclean import md_clean


def test_no_markdown_unchanged():
    """无 Markdown 模式的文本逐字节不变——系统回执（✅/⚠️ 模板行）天然无损。"""
    for s in ("✅ 收到，处理中", "⚠️ 出站死信（id=1）：xyz…", "",
              "普通中文 abc 123\n第二行", "3 * 4 = 12", "snake_case_name",
              "读取 a*b*c.py 文件"):
        assert md_clean(s) == s, s


def test_heading():
    assert md_clean("## 部署报告") == "【部署报告】"
    assert md_clean("###### 六级") == "【六级】"
    assert md_clean("### 尾井号 ###") == "【尾井号】"
    assert md_clean("#无空格") == "#无空格"   # 井号后须空格（保守不误伤话题标签）


def test_bold_em_strike():
    assert md_clean("**状态**：ok") == "状态：ok"
    assert md_clean("*斜体*") == "斜体"
    assert md_clean("3 * 4 * 5") == "3 * 4 * 5"    # 空格包裹不误伤
    assert md_clean("a*b*c") == "a*b*c"            # intraword 不误伤
    assert md_clean("~~删除线~~") == "删除线"
    assert md_clean("___name___") == "___name___"  # _ 形态明确不做


def test_inline_code_protected():
    assert md_clean("用 `rm -rf **` 清理") == "用 「rm -rf **」 清理"
    assert md_clean("`code` 与 **粗**") == "「code」 与 粗"
    assert md_clean("行内 `#` 与 `|` 竖线") == "行内 「#」 与 「|」 竖线"


def test_lists():
    assert md_clean("- 项目一") == "• 项目一"
    assert md_clean("  - 嵌套项") == "  • 嵌套项"
    assert md_clean("* 星号列表") == "• 星号列表"
    assert md_clean("+ 加号列表") == "• 加号列表"
    assert md_clean("1. 有序保留") == "1. 有序保留"


def test_quote_hr():
    assert md_clean("> 引用行") == "｜ 引用行"
    assert md_clean("---") == "—————————"
    assert md_clean("***") == "—————————"
    assert md_clean("- 有内容的列表") == "• 有内容的列表"   # 非水平线


def test_links_images():
    assert md_clean("见 [日志](http://x/y)") == "见 日志(http://x/y)"
    assert md_clean("![截图](http://x/a.png)") == "图片 截图(http://x/a.png)"


def test_fenced_code_block_protected():
    md = "前文\n```bash\nls **glob** # 注释\n```\n后文"
    assert md_clean(md) == "前文\n    ls **glob** # 注释\n后文"


def test_fenced_tilde_and_empty_lines():
    assert md_clean("~~~python\nx = 1\n\ny = 2\n~~~") == "    x = 1\n\n    y = 2"


def test_indented_line_untouched():
    # ≥4 空格缩进 = 代码（CommonMark indented code block）——fence 转写产物
    # （已缩进）再清洗不受行内规则影响，幂等由此闭环
    assert md_clean("    **raw** # 注释") == "    **raw** # 注释"


def test_table_two_col_single_row_transposed():
    md = "| 环境 | 版本 |\n|---|---|\n| prod | 2.1.235 |"
    assert md_clean(md) == "• 环境：prod\n• 版本：2.1.235"


def test_table_alignment_colons():
    md = "| k | v |\n|:---|---:|\n| x | y |"
    assert md_clean(md) == "• k：x\n• v：y"


def test_table_general_rows():
    md = "| a | b | c |\n|---|---|---|\n| 1 | 2 | 3 |\n| 4 | 5 | 6 |"
    assert md_clean(md) == "• a ｜ b ｜ c\n• 1 ｜ 2 ｜ 3\n• 4 ｜ 5 ｜ 6"


def test_table_not_triggered_without_sep_row():
    # 无分隔行的 |…| 行不是表格——按普通行走行内规则
    assert md_clean("|a|b| 单行") == "|a|b| 单行"


def test_idempotent():
    md = ("## 标题\n\n**粗** 与 `code`\n\n"
          "| k | v |\n|---|---|\n| 1 | 2 |\n\n```py\n**raw**\n```\n")
    once = md_clean(md)
    assert md_clean(once) == once


def test_nested_inline_in_block():
    assert md_clean("## **重要** 提示") == "【重要 提示】"
    assert md_clean("- **粗体** 项") == "• 粗体 项"
    assert md_clean("> 引用 `code`") == "｜ 引用 「code」"


def test_escaped_punctuation():
    assert md_clean(r"1\. 不是有序") == "1. 不是有序"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_mdclean.py -v`
Expected: 全部 FAIL，`ModuleNotFoundError: No module named 'common.mdclean'`

- [ ] **Step 3: 实现 `common/mdclean.py`**

```python
"""Markdown → 微信纯文本转写（M5C2，spec 2026-08-21-mdclean-alias-design.md §3.1）。

Claude 回复是 Markdown，微信纯文本不渲染——**、##、``` 等符号裸露刺眼。
md_clean 做"积极转写"：
    ## 标题 → 【标题】    **粗体** → 粗体    `代码` → 「代码」
    [文字](url) → 文字(url)    表格 → 竖排    - 列表 → •    > 引用 → ｜
设计约束：
- fenced 代码块（```/~~~ 围栏）内容**原样**（块内 ** 是 glob、# 是注释），
  整体缩进 4 空格——CommonMark 的 indented code block 语义，缩进产物在
  再次清洗时不进行内规则，幂等由此闭环；
- 纯函数（仅 stdlib re）、幂等、无 Markdown 模式的文本逐字节不变
  （系统回执 ✅/⚠️ 模板行天然无损）；
- _x_ 斜体不做（snake_case 误伤）、HTML 标签不做（YAGNI）。
"""
import re

_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_HR_RE = re.compile(r"^ {0,3}(-{3,}|\*{3,})\s*$")
_HEADING_RE = re.compile(r"^ {0,3}#{1,6}\s+(.*?)\s*#*\s*$")
_ULIST_RE = re.compile(r"^(\s*)[-*+]\s+(.+)$")
_QUOTE_RE = re.compile(r"^(\s*)>\s?(.*)$")
_INDENT_RE = re.compile(r"^ {4,}")
_TABLE_SEP_RE = re.compile(
    r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")

_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_BOLD_RE = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*")
_EM_RE = re.compile(r"(?<![\w*])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![\w*])")
_STRIKE_RE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~")
_IMG_RE = re.compile(r"!\[([^\]\n]*)\]\(([^)\n]+)\)")
_LINK_RE = re.compile(r"\[([^\]\n]*)\]\(([^)\n]+)\)")
_ESCAPE_RE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!|~>])")
_PH_RE = re.compile("\x00(\\d+)\x00")


def md_clean(text: str) -> str:
    """积极转写管线：fenced 块保护 → 表格块 → 逐行块级 → 行内。"""
    if not text:
        return text
    lines = text.split("\n")
    out: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        marker = _fence_marker(lines[i])
        if marker:
            i += 1
            while i < n and _fence_marker(lines[i]) != marker:
                out.append("    " + lines[i] if lines[i].strip() else lines[i])
                i += 1
            i += 1   # 闭围栏（未闭合到 EOF 也自然终止）
            continue
        if _is_table_start(lines, i, n):
            rows = [lines[i]]
            j = i + 2   # 跳过 header 与分隔行
            while j < n and _is_table_row(lines[j]):
                rows.append(lines[j])
                j += 1
            out.extend(_clean_table(rows))
            i = j
            continue
        out.append(_clean_line(lines[i]))
        i += 1
    return "\n".join(out)


def _fence_marker(line: str) -> str | None:
    """行是 fenced 开/闭围栏时返回规范标记（```/~~~），否则 None。"""
    m = _FENCE_RE.match(line)
    return m.group(1)[0] * 3 if m else None


def _is_table_row(line: str) -> bool:
    s = line.strip()
    return s.startswith("|") and s.count("|") >= 2


def _is_table_start(lines: list[str], i: int, n: int) -> bool:
    return (i + 1 < n and _is_table_row(lines[i])
            and bool(_TABLE_SEP_RE.match(lines[i + 1])))


def _cells(row: str) -> list[str]:
    s = row.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _clean_table(rows: list[str]) -> list[str]:
    """表格转写：两列 + 恰一数据行（参数表形态）转置键值竖排；其余每行
    • c0 ｜ c1 ｜ …（header 同形态）。分隔行不进 rows（收集时已跳过）。"""
    header = _cells(rows[0])
    data = [_cells(r) for r in rows[1:]]
    if len(header) == 2 and len(data) == 1:
        return [_inline(f"• {h}：{v}") for h, v in zip(header, data[0])]
    out = [_inline("• " + " ｜ ".join(header))]
    out += [_inline("• " + " ｜ ".join(r)) for r in data]
    return out


def _clean_line(line: str) -> str:
    """单行块级转写（fence/表格已在上层处理）。"""
    if _INDENT_RE.match(line):
        return line   # ≥4 空格缩进 = 代码：原样（含 fence 转写产物，幂等闭环）
    if _HR_RE.match(line):
        return "—————————"
    m = _HEADING_RE.match(line)
    if m and m.group(1):
        return _inline(f"【{m.group(1)}】")
    m = _ULIST_RE.match(line)
    if m:
        return f"{m.group(1)}• {_inline(m.group(2))}"
    m = _QUOTE_RE.match(line)
    if m:
        return f"{m.group(1)}｜ {_inline(m.group(2))}"
    return _inline(line)


def _inline(line: str) -> str:
    """行内转写：行内代码先占位（内容不参与后续规则），粗→斜→删→图→链，
    还原占位为「内容」，最后去反斜杠转义。"""
    stash: list[str] = []
    line = _INLINE_CODE_RE.sub(
        lambda m: (stash.append(m.group(1)) or f"\x00{len(stash) - 1}\x00"),
        line)
    line = _BOLD_RE.sub(r"\1", line)
    line = _EM_RE.sub(r"\1", line)
    line = _STRIKE_RE.sub(r"\1", line)
    line = _IMG_RE.sub(lambda m: f"图片 {m.group(1)}({m.group(2)})", line)
    line = _LINK_RE.sub(lambda m: f"{m.group(1)}({m.group(2)})", line)
    line = _ESCAPE_RE.sub(r"\1", line)
    return _PH_RE.sub(lambda m: f"「{stash[int(m.group(1))]}」", line)
```

注意 `_PH_RE = re.compile("\x00(\\d+)\x00")`——\x00 是字面 NUL 占位符（正文不含 NUL，不会与用户文本撞车）。

- [ ] **Step 4: 跑测试确认全过**

Run: `python -m pytest tests/test_mdclean.py -v`
Expected: 全部 PASS（若个别断言失败，按失败信息修正**实现**而非测试——测试矩阵是 spec 的规则化身；实现与 spec 冲突时停下来报告）

- [ ] **Step 5: Commit**

```bash
git add common/mdclean.py tests/test_mdclean.py
git commit -m "feat(M5C2): md_clean 纯函数——Markdown 积极转写（fence 保护+表格竖排+幂等）"
```

---

### Task 2: 清洗接入 outbound + 折算口径四处同步 + 配置默认值

**Files:**
- Modify: `gateway/outbound.py`（import 区、`__init__`、`_drain_once` 两处、`_send_media`、`_send_file_media`）
- Modify: `common/text.py:37-52`（`outbox_sent_pages`）
- Modify: `common/db.py:569-577`（`sent_pages_today`）
- Modify: `gateway/bridge.py:145`（/status 折算传参）
- Modify: `common/config.py:15-17`（`_DEFAULT_THROTTLE`）
- Modify: `gateway/config.example.json`（throttle 节）
- Test: `tests/test_outbound.py`（增用例）

**Interfaces:**
- Consumes: `md_clean(text)`（Task 1）。
- Produces: `outbox_sent_pages(rows, page_char_limit, md_clean_enabled=True)`；`db.sent_pages_today(page_char_limit, md_clean_enabled=True)`——两签名后续调用方（Task 3 无关；/status 与 OutboundLoop 即本任务改齐）。
- 关键不变量：**清洗在 `split_text` 之前**（`_drain_once` 文本行）——分页后清洗会让单页增量越过 `MAX_PAGE_BYTES=15000` 字节硬闸。

- [ ] **Step 1: 写失败测试**

`tests/test_outbound.py` 末尾追加：

```python
# ---------------- M5C2：出站 Markdown 清洗 ----------------

async def test_outbound_cleans_markdown(db):
    """文本行先 md_clean 再分页——fake iLink 收到清洗后纯文本（outbox 存原文）。"""
    il = FakeILink()
    db.insert_message(common_msg("u@im.wechat", "CTX"))
    loop = OutboundLoop(db, il, FakeCfg(),
                        token_ref={"token": "T", "base_url": ""},
                        typing_state={})
    db.enqueue(None, "u@im.wechat", "## 标题\n**粗** 与 `code`")
    task = asyncio.create_task(loop.run_forever())
    try:
        assert await wait_until(lambda: db.get_outbox(1).state == "sent")
        assert il.sent[0][2] == "【标题】\n粗 与 「code」"
        row = db._conn.execute("SELECT text FROM outbox WHERE id=1").fetchone()
        assert row["text"] == "## 标题\n**粗** 与 `code`"   # 原文留存
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_outbound_md_clean_off_raw(db):
    class CfgOff(FakeCfg):
        def __init__(self):
            super().__init__()
            self.throttle["md_clean"] = False

    il = FakeILink()
    db.insert_message(common_msg("u@im.wechat", "CTX"))
    loop = OutboundLoop(db, il, CfgOff(),
                        token_ref={"token": "T", "base_url": ""},
                        typing_state={})
    db.enqueue(None, "u@im.wechat", "**粗**")
    task = asyncio.create_task(loop.run_forever())
    try:
        assert await wait_until(lambda: db.get_outbox(1).state == "sent")
        assert il.sent[0][2] == "**粗**"   # 开关关闭：原文直发
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_outbound_cleans_caption(db, monkeypatch, tmp_path):
    """caption 同过 _mdc（M5B caption 单发整条——清洗点三处之一）。"""
    from types import SimpleNamespace
    import gateway.outbound as ob

    async def fake_upload(ilink, path, to_user, token, base_url=None,
                          media_type=None):
        return SimpleNamespace(download_param="dp", aes_key=b"k" * 16,
                               size_cipher="1")

    monkeypatch.setattr(ob, "upload_media", fake_upload)

    class FakeImgILink(FakeILink):
        async def send_image_message(self, to_user, ctx, download_param="",
                                     aes_key_hex="", size_cipher="",
                                     token=None, base_url=None):
            self.sent.append((to_user, ctx, "IMG"))
            return True

    il = FakeImgILink()
    db.insert_message(common_msg("u@im.wechat", "CTX"))
    p = tmp_path / "a.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n")
    db.enqueue_media(None, "u@im.wechat", str(p), caption="**图注**")
    loop = OutboundLoop(db, il, FakeCfg(),
                        token_ref={"token": "T", "base_url": ""},
                        typing_state={})
    task = asyncio.create_task(loop.run_forever())
    try:
        assert await wait_until(lambda: db.get_outbox(1).state == "sent")
        assert il.sent[0][2] == "图注"      # caption 清洗后
        assert il.sent[1][2] == "IMG"       # 图片条随后
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def test_outbox_sent_pages_md_clean_param(db):
    """折算口径：md_clean_enabled=True 时文本行按清洗后页数折算（四处一致）。"""
    from common.text import outbox_sent_pages
    from common.mdclean import md_clean
    raw = "**x**" * 400 + "y" * 1000      # 原文 3000 字符、清洗后 1400
    db.enqueue(None, "u@im.wechat", raw)
    db.mark_sent(1)
    rows = db._conn.execute(
        "SELECT kind, text, caption FROM outbox").fetchall()
    assert outbox_sent_pages(rows, 2000, md_clean_enabled=True) == \
        len(split_text(md_clean(raw), 2000))
    assert outbox_sent_pages(rows, 2000, md_clean_enabled=False) == \
        len(split_text(raw, 2000))
    assert db.sent_pages_today(2000) == \
        len(split_text(md_clean(raw), 2000))   # db 层透传默认 True
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_outbound.py -v -k "cleans or md_clean"`
Expected: 新增用例 FAIL（`il.sent[0][2]` 收到原始 Markdown / `outbox_sent_pages` 不接受第三个参数 TypeError）

- [ ] **Step 3: 实现四处接入**

`gateway/outbound.py`——import 区加：

```python
from common.mdclean import md_clean
```

`__init__`（[outbound.py:32-34](../../../gateway/outbound.py#L32-L34) 附近）：

```python
        self._mdclean = bool(self._cfg.throttle.get("md_clean", True))
        self._sent_today = self._db.sent_pages_today(
            int(self._cfg.throttle["page_char_limit"]),
            md_clean_enabled=self._mdclean)
```

`_drain_once` 日界重算（[outbound.py:69-70](../../../gateway/outbound.py#L69-L70)）：

```python
            self._sent_today = self._db.sent_pages_today(
                int(self._cfg.throttle["page_char_limit"]),
                md_clean_enabled=self._mdclean)
```

`_drain_once` 文本行（[outbound.py:116](../../../gateway/outbound.py#L116)）：

```python
            pages = split_text(self._mdc(item.text),
                               self._cfg.throttle["page_char_limit"])
```

`_send_media` caption（[outbound.py:210](../../../gateway/outbound.py#L210)）与 `_send_file_media` caption（[outbound.py:265](../../../gateway/outbound.py#L265)），两处同改：

```python
        caption = self._mdc((item.caption or "").strip())
```

类内加私有 helper（放 `_alert_all` 之后）：

```python
    def _mdc(self, text: str) -> str:
        """出站 Markdown 清洗（M5C2）：throttle.md_clean 开关（默认开）。
        清洗必须发生在 split_text 之前——分页后清洗会让单页增量越过
        MAX_PAGE_BYTES 字节硬闸（16384B 静默丢消息）。"""
        return md_clean(text) if self._mdclean else text
```

`common/text.py`——顶部加 `from common.mdclean import md_clean`，`outbox_sent_pages`（[text.py:37-52](../../../common/text.py#L37-L52)）改：

```python
def outbox_sent_pages(rows, page_char_limit: int,
                      md_clean_enabled: bool = True) -> int:
    """已送达 outbox 行折算微信侧实际发送条数（出站日计数口径，gateway 出站
    协程运行时与 bridge /status、重启恢复共用——三处必须同一折算）：
    文本行 = len(split_text(md_clean 后文本))——与运行时「先清洗后分页」一致
    （M5C2 起四处口径同步，折算仍是近似值：重试重发的页运行时会再计）；
    图片/文件行（kind='image'/'file'）= 媒体条 1 条 + caption（非空时）1 条。

    行需含 kind/text/caption 键（sqlite3.Row 或 dict 均可）。"""
    n = 0
    for r in rows:
        if r["kind"] in ("image", "file"):
            n += 1 + (1 if str(r["caption"] or "").strip() else 0)
        else:
            t = md_clean(r["text"]) if md_clean_enabled else r["text"]
            n += len(split_text(t, page_char_limit))
    return n
```

`common/db.py` `sent_pages_today`（[db.py:569-577](../../../common/db.py#L569-L577)）：

```python
    def sent_pages_today(self, page_char_limit: int,
                         md_clean_enabled: bool = True) -> int:
        """今日（本地零点起）已送达的微信侧发送条数——出站熔断计数的重启恢复
        与 /status 展示共用。折算口径见 common.text.outbox_sent_pages（M5C2 起
        文本行同过 md_clean，md_clean_enabled 与运行时开关一致）；
        迁移前的历史 sent 行 sent_at 为 NULL 不计（当日略低估，次日归零）。"""
        rows = self._conn.execute(
            "SELECT kind, text, caption FROM outbox "
            "WHERE state='sent' AND sent_at IS NOT NULL AND sent_at>=?",
            (local_midnight_ts(),)).fetchall()
        return outbox_sent_pages(rows, page_char_limit, md_clean_enabled)
```

`gateway/bridge.py:145`（/status）：

```python
        sent = db.sent_pages_today(int(config.throttle["page_char_limit"]),
                                   md_clean_enabled=bool(
                                       config.throttle.get("md_clean", True)))
```

`common/config.py:15-17`：

```python
_DEFAULT_THROTTLE = {"min_send_interval_s": 1.0, "progress_window_s": 2.5,
                     "page_char_limit": 2000, "daily_send_limit": 500,
                     "merge_window_s": 2.0, "md_clean": True}
```

`gateway/config.example.json` throttle 节加 `"md_clean": true`。

- [ ] **Step 4: 跑测试确认通过（含既有回归）**

Run: `python -m pytest tests/test_outbound.py tests/test_db.py tests/test_bridge.py tests/test_e2e.py -v`
Expected: 全部 PASS（既有用例的纯文本断言不受清洗影响——md_clean 无损性由 Task 1 保证；`test_db.py` 的 `sent_pages_today` 折算断言用纯 "x" 文本，清洗不变）

- [ ] **Step 5: Commit**

```bash
git add gateway/outbound.py common/text.py common/db.py gateway/bridge.py common/config.py gateway/config.example.json tests/test_outbound.py
git commit -m "feat(M5C2): 出站清洗接入——_drain_once 先清洗后分页 + caption + 折算四处同步"
```

---

### Task 3: `/config set` bool 类型支持 + `throttle.md_clean` 白名单键

**Files:**
- Modify: `gateway/proxy.py`（`CONFIG_USAGE`、`_is_bool`/`_parse_bool`、`CONFIG_KEYS`、`_config_set` 校验分支、`_THROTTLE_LABELS`）
- Test: `tests/test_proxy.py`（增用例）

**Interfaces:**
- Produces: `CONFIG_KEYS["throttle.md_clean"]` 可经微信 `/config set throttle.md_clean true|false` 写入（重启生效）。

- [ ] **Step 1: 写失败测试**

`tests/test_proxy.py` 的 `/config set` 段（`test_config_set_merge_window_zero_disables` 之后）追加：

```python
async def test_config_set_bool_md_clean(db, tmp_path):
    """M5C2：throttle.md_clean 布尔键——true/false 可写、非法值拒绝、
    JSON 落盘为 true/false 布尔而非字符串。"""
    _write_gateway_config(tmp_path)
    reply = await execute_proxy(
        db, _route("config", "set throttle.md_clean false"), FakeCfg(tmp_path))
    assert "已写入" in reply and "重启生效" in reply
    assert _read_gateway_config(tmp_path)["throttle"]["md_clean"] is False
    reply = await execute_proxy(
        db, _route("config", "set throttle.md_clean true"), FakeCfg(tmp_path))
    assert "已写入" in reply
    assert _read_gateway_config(tmp_path)["throttle"]["md_clean"] is True
    reply = await execute_proxy(
        db, _route("config", "set throttle.md_clean 开"), FakeCfg(tmp_path))
    assert "不是合法" in reply
    assert _read_gateway_config(tmp_path)["throttle"]["md_clean"] is True  # 未改


async def test_config_overview_shows_md_clean(db, tmp_path):
    _write_gateway_config(tmp_path)
    reply = await execute_proxy(db, _route("config"), FakeCfg(tmp_path))
    assert "md_clean" in reply
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_proxy.py -v -k "md_clean"`
Expected: FAIL——"键 throttle.md_clean 不开放微信修改" / 概览无 md_clean

- [ ] **Step 3: 实现**

`gateway/proxy.py`：

`CONFIG_USAGE`（[proxy.py:17-22](../../../gateway/proxy.py#L17-L22)）中 `daily_send_limit/merge_window_s` 后补 `/md_clean`：

```python
CONFIG_USAGE = ("用法：/config — 概览；/config set <键> <值>（可改键："
                "throttle.min_send_interval_s/progress_window_s/"
                "page_char_limit/daily_send_limit/merge_window_s/md_clean、"
                "budget.max_turns/max_usd、"
                "worker.concurrency、cron.disk_threshold_pct/cpu_threshold_pct/"
                "mem_threshold_pct/load_sustain_min/cert_warn_days/"
                "alert_silence_h/queue_backlog_warn；重启生效）")
```

`_THROTTLE_LABELS`（[proxy.py:265-272](../../../gateway/proxy.py#L265-L272)）加一行：

```python
    ("md_clean", "Markdown清洗(md_clean)"),
```

`_is_float` 之后（[proxy.py:284](../../../gateway/proxy.py#L284) 附近）加：

```python
def _parse_bool(s: str) -> bool:
    return s.lower() == "true"
```

`CONFIG_KEYS`（[proxy.py:289-305](../../../gateway/proxy.py#L289-L305)）加：

```python
    "throttle.md_clean": (_parse_bool, lambda v: isinstance(v, bool),
                          "布尔(true/false)"),
```

`_config_set` 类型校验分支（[proxy.py:367-368](../../../gateway/proxy.py#L367-L368)）改为三分支（注意判断函数同一性 `parser is _parse_bool`，`parser is bool` 永远为 False——CONFIG_KEYS 里存的是 `_parse_bool` 函数对象而非 `bool` 类型）：

```python
    if parser is _parse_bool:
        if val.lower() not in ("true", "false"):
            return f"值 {val} 不是合法{type_name}。"
    elif not (_is_int(val) if parser is int else _is_float(val)):
        return f"值 {val} 不是合法{type_name}。"
```

（后续 `v = parser(val)` 起不变——bool 路径 `_parse_bool` 已在白名单值上调用，`parser is float` 的 isfinite 检查自然跳过。）

- [ ] **Step 4: 跑测试确认通过（含既有 proxy 回归）**

Run: `python -m pytest tests/test_proxy.py -v`
Expected: 全部 PASS

- [ ] **Step 5: Commit**

```bash
git add gateway/proxy.py tests/test_proxy.py
git commit -m "feat(M5C2): /config set 支持 bool 键 + throttle.md_clean 白名单"
```

---

### Task 4: router 内置别名 `/t /s /c /cs`

**Files:**
- Modify: `gateway/router.py`（`BUILTIN_ALIASES` 常量 + `route()` 映射）
- Test: `tests/test_router.py`（增用例）

**Interfaces:**
- Produces: `BUILTIN_ALIASES = {"t": "tasks", "s": "status", "c": "cancel", "cs": "sessions"}`——Task 5 的 bridge `_alias` 撞名提示要用（import 它）。
- 语义：内置映射在 BRIDGE/ILINK/PROXY 判定**之前**、且先于动态 slash_commands（`/t` 即便 Claude 也有 `/t` 命令也映射到 tasks）。

- [ ] **Step 1: 写失败测试**

`tests/test_router.py` 追加：

```python
# ---------------- M5C3：内置短别名 ----------------

def test_builtin_aliases():
    for short, full in [("t", "tasks"), ("s", "status"),
                        ("c", "cancel"), ("cs", "sessions")]:
        r = route(f"/{short}", set())
        assert r.kind == "bridge" and r.command == full, short


def test_builtin_alias_args_follow():
    r = route("/c 5", set())
    assert r.kind == "bridge" and r.command == "cancel" and r.args == "5"


def test_builtin_alias_beats_slash_commands():
    # 内置映射先于动态 slash 清单：claude 若也暴露 /t 命令不遮蔽内置别名
    r = route("/t", {"t", "tasks"})
    assert r.kind == "bridge" and r.command == "tasks"


def test_builtin_alias_target_not_overridden():
    # 映射目标必须仍是合法桥命令（防常量改错后掉进 unknown）
    from gateway.router import BUILTIN_ALIASES, BRIDGE_COMMANDS
    assert set(BUILTIN_ALIASES.values()) <= BRIDGE_COMMANDS
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_router.py -v -k "builtin"`
Expected: FAIL——`/t` 判 unknown（ImportError: BUILTIN_ALIASES）

- [ ] **Step 3: 实现**

`gateway/router.py`——常量区（[router.py:8](../../../gateway/router.py#L8) `BRIDGE_COMMANDS` 之后）加：

```python
# M5C3 内置短别名：route() 开头静态映射（纯函数）。用户自定义别名在
# gateway/app.py route 调用前查 KV 展开（先于此层——同名时用户定义覆盖内置）。
BUILTIN_ALIASES = {"t": "tasks", "s": "status", "c": "cancel",
                   "cs": "sessions"}
```

`route()`（[router.py:38-41](../../../gateway/router.py#L38-L41) `if not name:` 判空之后、BRIDGE_COMMANDS 判定之前）插一行：

```python
    name = BUILTIN_ALIASES.get(name, name)
```

- [ ] **Step 4: 跑测试确认通过（含既有 router 回归）**

Run: `python -m pytest tests/test_router.py -v`
Expected: 全部 PASS

- [ ] **Step 5: Commit**

```bash
git add gateway/router.py tests/test_router.py
git commit -m "feat(M5C3): router 内置短别名 /t /s /c /cs（纯函数静态映射）"
```

---

### Task 5: bridge `/alias` 命令（add/del/list + 撞名规则）

**Files:**
- Modify: `gateway/router.py:8`（`BRIDGE_COMMANDS` 加 `"alias"`）
- Modify: `gateway/bridge.py`（顶部 import、`BRIDGE_HELP`、`execute_bridge` 分支、`_alias` 函数）
- Test: `tests/test_bridge.py`（增用例）

**Interfaces:**
- Consumes: `BUILTIN_ALIASES`（Task 4）、`BRIDGE_COMMANDS/ILINK_COMMANDS/PROXY_COMMANDS`（router 既有）。
- Produces: `/alias add <名> <内容…>`、`/alias del <名>`、`/alias list`——KV `alias:<user>` 单键 JSON dict（Task 6 的 `_expand_alias` 读同一 KV）。

- [ ] **Step 1: 写失败测试**

`tests/test_bridge.py` 追加（复用文件内既有 `_route`/`FakePool`/`FakeCfg` 与 conftest `db` fixture）：

```python
# ---------------- M5C3：/alias 自定义快捷命令 ----------------

def _load_aliases(db, user="u@im.wechat"):
    return json.loads(db.get_state(f"alias:{user}") or "{}")


async def test_alias_add_and_list(db):
    reply = await execute_bridge(db, FakePool([]), _route("alias"),
                                 "u@im.wechat", FakeCfg())
    assert "暂无自定义别名" in reply and "/t=/tasks" in reply
    reply = await execute_bridge(
        db, FakePool([]), _route("alias", "add go 跑全量测试并总结"),
        "u@im.wechat", FakeCfg())
    assert "已定义 /go" in reply
    assert _load_aliases(db) == {"go": "跑全量测试并总结"}
    reply = await execute_bridge(db, FakePool([]), _route("alias", "list"),
                                 "u@im.wechat", FakeCfg())
    assert "/go → 跑全量测试并总结" in reply
    assert any(r["kind"] == "alias_add"
               for r in db._conn.execute("SELECT kind FROM audit_log"))


async def test_alias_del(db):
    await execute_bridge(db, FakePool([]), _route("alias", "add go x"),
                         "u@im.wechat", FakeCfg())
    reply = await execute_bridge(db, FakePool([]), _route("alias", "del go"),
                                 "u@im.wechat", FakeCfg())
    assert "已删除别名 /go" in reply
    assert _load_aliases(db) == {}
    reply = await execute_bridge(db, FakePool([]), _route("alias", "del go"),
                                 "u@im.wechat", FakeCfg())
    assert "没有别名 /go" in reply


async def test_alias_add_validation(db):
    # 系统命令撞名拒绝（桥/运维/代理/alias 自身）
    for bad in ("tasks", "time", "config", "alias"):
        reply = await execute_bridge(
            db, FakePool([]), _route("alias", f"add {bad} x"),
            "u@im.wechat", FakeCfg())
        assert "系统命令" in reply, bad
    # 名超长
    reply = await execute_bridge(
        db, FakePool([]), _route("alias", f"add {'n' * 17} x"),
        "u@im.wechat", FakeCfg())
    assert "1~16" in reply
    # 值超长
    reply = await execute_bridge(
        db, FakePool([]), _route("alias", f"add ok {'v' * 2001}"),
        "u@im.wechat", FakeCfg())
    assert "1~2000" in reply
    # 用法缺参
    reply = await execute_bridge(db, FakePool([]), _route("alias", "add onlyname"),
                                 "u@im.wechat", FakeCfg())
    assert "用法" in reply


async def test_alias_can_override_builtin_and_warns_slash(db):
    # 内置别名可覆盖（t/s/c/cs 不在禁止集）——附注提示
    reply = await execute_bridge(
        db, FakePool([]), _route("alias", "add t /status"), "u@im.wechat",
        FakeCfg())
    assert "已定义 /t" in reply and "覆盖内置" in reply
    # 撞 Claude 动态命令：允许但提示
    db.set_state("slash_commands", json.dumps(["review"]))
    reply = await execute_bridge(
        db, FakePool([]), _route("alias", "add review 看代码"), "u@im.wechat",
        FakeCfg())
    assert "已定义 /review" in reply and "重名" in reply


async def test_alias_count_limit(db):
    for i in range(50):
        await execute_bridge(db, FakePool([]), _route("alias", f"add a{i} v"),
                             "u@im.wechat", FakeCfg())
    reply = await execute_bridge(
        db, FakePool([]), _route("alias", "add overflow v"), "u@im.wechat",
        FakeCfg())
    assert "上限" in reply
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_bridge.py -v -k "alias"`
Expected: FAIL——route("alias") 未进 BRIDGE_COMMANDS，execute_bridge 返回 "未知桥命令 alias"（测试直调 _route 构造 bridge kind 则走到尾部 default）

- [ ] **Step 3: 实现**

`gateway/router.py:8`：

```python
BRIDGE_COMMANDS = {"cancel", "tasks", "status", "cd", "sessions", "policy",
                   "bg", "new", "adopt", "delete", "cron", "alias"}
```

`gateway/bridge.py`——顶部 import 区（[bridge.py:2-5](../../../gateway/bridge.py#L2-L5)）加：

```python
from gateway.router import (BUILTIN_ALIASES, BRIDGE_COMMANDS, ILINK_COMMANDS,
                            PROXY_COMMANDS)
```

（router 只 import difflib/dataclasses，无环。）

`BRIDGE_HELP`（[bridge.py:7-19](../../../gateway/bridge.py#L7-L19)）加条目：

```python
    "alias": "/alias add <名> <内容> — 自定义快捷命令（del <名>、list 查看；"
             "内置：/t=/tasks /s=/status /c=/cancel /cs=/sessions）",
```

`execute_bridge`（[bridge.py:233-234](../../../gateway/bridge.py#L233-L234) `"cron"` 分支之后、`return f"未知桥命令 {cmd}"` 之前）加：

```python
    if cmd == "alias":
        return _alias(db, route.args.strip(), from_user)
```

文件尾部（`_remain_text` 等私有函数区）加：

```python
def _alias(db, arg: str, from_user: str) -> str:
    """/alias：用户自定义快捷命令（M5C3，spec §3.6）。存 KV alias:<user> 单键
    JSON dict（merge_pending 同构先例）。撞名规则：系统命令（桥/运维/代理/
    alias 自身）禁止；内置别名（t/s/c/cs）可覆盖（app 层用户展开先于内置映射，
    天然生效）；撞 Claude 动态命令允许但回执提示（用户显式意图优先）。"""
    parts = arg.split(None, 1)
    op = parts[0] if parts else "list"
    rest = parts[1] if len(parts) > 1 else ""
    key = f"alias:{from_user}"

    def _load() -> dict:
        try:
            return json.loads(db.get_state(key) or "{}")
        except ValueError:
            return {}

    if op == "list":
        aliases = _load()
        if not aliases:
            return ("暂无自定义别名。内置：/t=/tasks /s=/status "
                    "/c=/cancel /cs=/sessions")
        lines = [f"快捷命令（{len(aliases)} 条）："]
        for name, value in sorted(aliases.items()):
            lines.append(f"· /{name} → {' '.join(value.split())[:30]}")
        return "\n".join(lines)

    if op == "add":
        sub = rest.split(None, 1)
        if len(sub) != 2:
            return "用法：/alias add <名> <内容>（内容可含空格；del/list 见 /help）"
        name, value = sub[0], sub[1].strip()
        if not name or len(name) > 16:
            return "别名名须为 1~16 个字符（不含空格）。"
        if not value or len(value) > 2000:
            return "别名内容须为 1~2000 字符。"
        reserved = BRIDGE_COMMANDS | ILINK_COMMANDS | PROXY_COMMANDS | {"alias"}
        if name in reserved:
            return f"/{name} 是系统命令，不能用作别名。"
        aliases = _load()
        if name not in aliases and len(aliases) >= 50:
            return "别名已达上限（50 条），请先 /alias del 清理。"
        try:
            slash = set(json.loads(db.get_state("slash_commands") or "[]"))
        except ValueError:
            slash = set()
        aliases[name] = value
        db.set_state(key, json.dumps(aliases, ensure_ascii=False))
        db.audit("alias_add", f"user={from_user} name={name}")
        note = ""
        if name in BUILTIN_ALIASES:
            note = "（已覆盖内置同名别名）"
        elif name in slash:
            note = f"（注意：与 Claude 命令 /{name} 重名，别名优先）"
        return f"✅ 已定义 /{name} → {' '.join(value.split())[:30]}{note}"

    if op == "del":
        if not rest:
            return "用法：/alias del <名>"
        name = rest.split()[0]
        aliases = _load()
        if name not in aliases:
            return f"没有别名 /{name}（/alias list 查看）。"
        del aliases[name]
        db.set_state(key, json.dumps(aliases, ensure_ascii=False))
        db.audit("alias_del", f"user={from_user} name={name}")
        return f"已删除别名 /{name}。"

    return "用法：/alias add <名> <内容> | /alias del <名> | /alias list"
```

- [ ] **Step 4: 跑测试确认通过（含 /help 生成回归）**

Run: `python -m pytest tests/test_bridge.py tests/test_router.py -v`
Expected: 全部 PASS（`/help` 由 BRIDGE_HELP 动态合并，新条目自动出现）

- [ ] **Step 5: Commit**

```bash
git add gateway/router.py gateway/bridge.py tests/test_bridge.py
git commit -m "feat(M5C3): /alias 桥命令——add/del/list + 撞名规则 + KV 持久化"
```

---

### Task 6: app 用户别名展开（route 前、一层）

**Files:**
- Modify: `gateway/app.py`（`_expand_alias` 模块级函数 + `handle_inbound` 调用点）
- Test: `tests/test_alias.py`（新建，harness 复用 test_merge.py 形态）

**Interfaces:**
- Consumes: KV `alias:<user>`（Task 5 写入）。
- Produces: `_expand_alias(db, from_user, text) -> str | None`——返回 None 表示无别名（原 text 走 route）。
- 语义：展开结果**不再二次展开**（调用方只调一次，防 `/a`→`/b`→`/a` 循环）；展开后按 route 正常分发（chat 进合并窗口/bridge 秒回/forward 转发），与直接发该文本完全一致。

- [ ] **Step 1: 写失败测试**

创建 `tests/test_alias.py`：

```python
"""M5C3 用户别名展开：route 前一层展开；展开后行为与直接发该文本一致。"""
import asyncio
import json

from common.db import Database
from gateway.app import _expand_alias, handle_inbound

USER = "u@im.wechat"


class Cfg:
    def __init__(self, tmp_path, window=2.0):
        self.repo_root = tmp_path
        self.whitelist = {USER}
        self.default_cwd = str(tmp_path)
        self.throttle = {"min_send_interval_s": 0.0, "progress_window_s": 0.0,
                         "page_char_limit": 2000, "daily_send_limit": 500,
                         "merge_window_s": window}


def _msg(msg_id, text):
    return {"message_id": msg_id, "seq": msg_id, "from_user_id": USER,
            "message_type": 1, "context_token": "CTX",
            "item_list": [{"type": 1, "text_item": {"text": text}}]}


def _task_prompts(db):
    return [r["prompt"] for r in db._conn.execute(
        "SELECT prompt FROM tasks ORDER BY id")]


def _outbox_texts(db):
    return [r["text"] for r in db._conn.execute(
        "SELECT text FROM outbox ORDER BY id")]


def _set_alias(db, name, value):
    cur = json.loads(db.get_state(f"alias:{USER}") or "{}")
    cur[name] = value
    db.set_state(f"alias:{USER}", json.dumps(cur, ensure_ascii=False))


# ---- _expand_alias 纯逻辑 ----

def test_expand_alias_hit_with_args(tmp_path):
    db = Database(tmp_path / "t.db"); db.ensure_schema()
    _set_alias(db, "go", "跑全量测试")
    assert _expand_alias(db, USER, "/go") == "跑全量测试"
    assert _expand_alias(db, USER, "/go 只跑单元") == "跑全量测试 只跑单元"


def test_expand_alias_miss_cases(tmp_path):
    db = Database(tmp_path / "t.db"); db.ensure_schema()
    assert _expand_alias(db, USER, "普通文本") is None      # 非斜杠
    assert _expand_alias(db, USER, "/") is None             # 裸斜杠
    assert _expand_alias(db, USER, "/nosuch") is None       # 未命中
    db.set_state(f"alias:{USER}", "{oops")                  # 坏 JSON 容错
    assert _expand_alias(db, USER, "/go") is None


def test_expand_alias_empty_value(tmp_path):
    db = Database(tmp_path / "t.db"); db.ensure_schema()
    _set_alias(db, "bad", "")
    assert _expand_alias(db, USER, "/bad") is None


# ---- handle_inbound 集成 ----

async def test_alias_expands_to_chat_enters_merge_window(tmp_path):
    """/go 展开为 chat 文本 → 与直接发该文本一致：进合并窗口。"""
    db = Database(tmp_path / "t.db"); db.ensure_schema()
    _set_alias(db, "go", "跑全量测试")
    cfg = Cfg(tmp_path, window=0.05)
    await handle_inbound(db, cfg, None, None, _msg(1, "/go"), ilink=None)
    assert _task_prompts(db) == []                       # 窗口内未建任务
    await asyncio.sleep(0.15)
    assert _task_prompts(db) == ["跑全量测试"]            # flush 后 prompt=展开文本
    # 入站落盘存原始 /go（审计看用户发了什么）
    assert [r["text"] for r in db._conn.execute(
        "SELECT text FROM messages")][0] == "/go"


async def test_alias_expands_to_bridge_command(tmp_path):
    """/自定义 t 映射 /tasks：展开为斜杠 → bridge 秒回，不建任务。"""
    db = Database(tmp_path / "t.db"); db.ensure_schema()
    _set_alias(db, "t", "/tasks")
    cfg = Cfg(tmp_path, window=0.05)
    await handle_inbound(db, cfg, None, None, _msg(1, "/t"), ilink=None)
    assert _task_prompts(db) == []                       # bridge 不建任务
    assert any("没有运行中或排队的任务" in t for t in _outbox_texts(db))


async def test_alias_expansion_single_layer(tmp_path):
    """/a 展开 /b 后不再展开 /b（一层防循环）——/b 未定义故落 unknown 提示。"""
    db = Database(tmp_path / "t.db"); db.ensure_schema()
    _set_alias(db, "a", "/b")
    cfg = Cfg(tmp_path, window=0.05)
    await handle_inbound(db, cfg, None, None, _msg(1, "/a"), ilink=None)
    await asyncio.sleep(0.15)
    assert any("未知命令 /b" in t for t in _outbox_texts(db))


async def test_builtin_alias_without_user_override(tmp_path):
    """无用户覆盖时 /t 走 router 内置映射（Task 4）→ bridge tasks。"""
    db = Database(tmp_path / "t.db"); db.ensure_schema()
    cfg = Cfg(tmp_path, window=0.05)
    await handle_inbound(db, cfg, None, None, _msg(1, "/t"), ilink=None)
    assert _task_prompts(db) == []
    assert any("没有运行中或排队的任务" in t for t in _outbox_texts(db))
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_alias.py -v`
Expected: FAIL——`ImportError: cannot import name '_expand_alias'`

- [ ] **Step 3: 实现**

`gateway/app.py`——`_merge_window_s`（[app.py:54](../../../gateway/app.py#L54)）之前加模块级函数：

```python
def _expand_alias(db, from_user: str, text: str) -> str | None:
    """用户别名展开（M5C3）：仅斜杠消息；KV alias:<user> 命中返回「值 + 空格 +
    附加参数」。未命中/非斜杠/坏 JSON 返回 None。展开结果不再二次展开——
    调用方只调一次（防 /a→/b→/a 链式循环；内置别名在 router 层兜底）。"""
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
        return None          # 坏 KV 容错：当无别名，不炸入站
    value = aliases.get(parts[0])
    if not value:
        return None
    args = parts[1] if len(parts) > 1 else ""
    return f"{value} {args}".strip()
```

`handle_inbound` 的 route 调用处（[app.py:331-334](../../../gateway/app.py#L331-L334)）改为：

```python
    if not text and not media_lines:
        return   # 空消息（无 text_item 亦无已知媒体——贴纸/未知 item）不建任务、
        # 不进合并窗口（防空 prompt + 误导性「正在合并」ACK）
    if text.startswith("/"):
        expanded = _expand_alias(db, from_user, text)
        if expanded is not None:
            text = expanded   # 展开后照常 route（一次，不再展开）
    r = route(text, slash)
```

- [ ] **Step 4: 跑测试确认通过（含合并窗口回归）**

Run: `python -m pytest tests/test_alias.py tests/test_merge.py -v`
Expected: 全部 PASS

- [ ] **Step 5: Commit**

```bash
git add gateway/app.py tests/test_alias.py
git commit -m "feat(M5C3): 用户别名 route 前一层展开——展开后行为与直发一致"
```

---

### Task 7: E2E + 文档同步 + 全量回归

**Files:**
- Modify: `tests/test_e2e.py`（增两个 E2E 用例）
- Modify: `README.md`（命令表 + 配置键说明）
- Modify: `CLAUDE.md`（M5C2/M5C3 功能清单、测试计数、组件清单）

**Interfaces:**
- Consumes: 前六个任务的全部产物。

- [ ] **Step 1: 建 fixture + 写 E2E 失败测试**

创建 `tests/fixtures/md_result_stream.jsonl`（fake claude 回放流——result 带 Markdown 全语法）：

```jsonl
{"type": "system", "subtype": "init", "session_id": "md-1", "slash_commands": ["review"]}
{"type":"result","subtype":"success","result":"## 部署报告\n\n**状态**：`成功`，详见 [日志](http://x/y)。\n\n| 环境 | 版本 |\n|---|---|\n| prod | 2.1.235 |\n\n```bash\nsystemctl restart daoyu\n```","total_cost_usd":0.1,"is_error":false}
```

`tests/test_e2e.py` 追加（复用文件内 `FakeCfg`/`inbound`/`_texts`/`_wait_done`）：

```python
# ---------------- M5C2/M5C3：清洗留存不变量 + 别名全链路 ----------------

async def test_e2e_markdown_result_kept_raw_in_outbox(tmp_path, monkeypatch):
    """M5C2：fake claude 回 Markdown 全语法 → outbox 恒存**原文**（清洗发生在
    投递层——test_outbound.test_outbound_cleans_markdown 已断言清洗后投递，
    两段拼起来即全链；此处钉住「原文留存」与清洗函数对该产物的正确性）。"""
    cfg = FakeCfg(tmp_path, monkeypatch)   # 先构造（内部 setenv 默认流）
    monkeypatch.setenv("FAKE_CLAUDE_SCRIPT",   # 再覆盖为 Markdown 回放流
                       str(FIXTURES / "md_result_stream.jsonl"))
    db = Database(tmp_path / "e2e.db")
    db.ensure_schema()
    runner = TaskRunner(db, cfg, process_registry={})
    pool = WorkerPool(db, cfg, runner=runner, concurrency=2, poll_interval_s=0.01)
    loop_task = asyncio.create_task(pool.run_forever())
    try:
        await handle_inbound(db, cfg, pool, None, inbound(1, "跑部署"))
        await _wait_done(db, timeout=10)
        md = ("## 部署报告\n\n**状态**：`成功`，详见 [日志](http://x/y)。\n\n"
              "| 环境 | 版本 |\n|---|---|\n| prod | 2.1.235 |\n\n"
              "```bash\nsystemctl restart daoyu\n```")
        assert any(t == md for t in _texts(db))       # outbox 原文
        from common.mdclean import md_clean
        assert md_clean(md) == (
            "【部署报告】\n\n状态：「成功」，详见 日志(http://x/y)。\n\n"
            "• 环境：prod\n• 版本：2.1.235\n\n"
            "    systemctl restart daoyu")
    finally:
        loop_task.cancel()
        await asyncio.gather(loop_task, return_exceptions=True)


async def test_e2e_alias_full_pipeline(tmp_path, monkeypatch):
    """/alias add go <prompt>（桥命令秒回）→ /go 展开建任务 → fake claude 收到
    展开后 prompt（stdin.log）；/t 内置别名等价 /tasks 秒回。"""
    cfg = FakeCfg(tmp_path, monkeypatch)
    db = Database(tmp_path / "e2e.db")
    db.ensure_schema()
    runner = TaskRunner(db, cfg, process_registry={})
    pool = WorkerPool(db, cfg, runner=runner, concurrency=2, poll_interval_s=0.01)
    loop_task = asyncio.create_task(pool.run_forever())
    try:
        await handle_inbound(db, cfg, pool, None, inbound(1, "/alias add go 跑全量测试并总结"))
        assert any("已定义 /go" in t for t in _texts(db))
        await handle_inbound(db, cfg, pool, None, inbound(2, "/go"))
        await _wait_done(db, timeout=10)
        # fake claude 的 stdin 收到展开后 prompt（不是 /go）
        assert (tmp_path / "stdin.log").read_text(encoding="utf-8") == \
            "跑全量测试并总结"
    finally:
        loop_task.cancel()
        await asyncio.gather(loop_task, return_exceptions=True)


async def test_e2e_builtin_alias_t(tmp_path, monkeypatch):
    cfg = FakeCfg(tmp_path, monkeypatch)
    db = Database(tmp_path / "e2e.db")
    db.ensure_schema()
    pool = WorkerPool(db, cfg, concurrency=2)   # 真实接线；不启动调度循环
    await handle_inbound(db, cfg, pool, None, inbound(1, "/t"))
    assert any("没有运行中或排队的任务" in t for t in _texts(db))
    assert _count(db, "tasks") == 0             # bridge 秒回不入队
```

（`test_e2e_markdown_result_kept_raw_in_outbox` 里 setenv 必须在 `FakeCfg(...)` **之后**——FakeCfg.__init__ 会 setenv 默认回放流，后设的覆盖先生效，fake claude 子进程启动时才读 env，顺序正确。）

- [ ] **Step 2: 跑 E2E 确认通过**

Run: `python -m pytest tests/test_e2e.py -v -k "alias or markdown"`
Expected: PASS（前六个任务已完成时这些用例应当直接绿——若 FAIL 说明前六任务有集成缺口，回头修）

- [ ] **Step 3: 文档同步**

`README.md`：
- 命令表（含 `/cron` 的表格）加一行：`/alias add <名> <内容> | del <名> | list — 自定义快捷命令（内置 /t=/tasks /s=/status /c=/cancel /cs=/sessions）`
- 配置键说明处（`merge_window_s` 附近）加：`throttle.md_clean`（默认 true）——出站 Markdown 清洗开关，关闭则原文直发，重启生效。

`CLAUDE.md`：
- 「当前状态」段追加 M5C2/M5C3 完成句（含测试总数——以 Step 4 实际输出为准）。
- 在 M5C1 功能清单之后加两段清单（内容照 spec §3.1/§3.4-3.6 摘要：md_clean 规则要点、投递前清洗+折算四处、内置别名+双层展开+撞名规则）。
- 「常用命令」节测试计数更新。
- 「统一命令总线」节桥命令列表加 `/alias`。

- [ ] **Step 4: 全量回归**

Run: `python -m pytest`
Expected: 全部 PASS，总数 = 434 + 新增（test_mdclean 15 + test_outbound 4 + test_proxy 2 + test_router 4 + test_bridge 5 + test_alias 8 + test_e2e 2 ≈ 469，以实际为准）

- [ ] **Step 5: Commit**

```bash
git add tests/test_e2e.py README.md CLAUDE.md
git commit -m "feat(M5C2/M5C3): E2E 别名全链路 + 文档同步（README/CLAUDE.md）"
```
