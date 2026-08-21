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
_QUOTE_RE = re.compile(r"^(\s*)((?:>\s?)+)(.*)$")
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
        # 连续多个 > 各转一个 ｜（嵌套引用，spec §3.1 备注）
        depth = m.group(2).count(">")
        return f"{m.group(1)}{'｜ ' * depth}{_inline(m.group(3))}"
    return _inline(line)


def _inline(line: str) -> str:
    """行内转写：行内代码先占位（内容不参与后续规则），粗→斜→删→图→链，
    去反斜杠转义，最后还原占位为「内容」（转义先于还原——行内代码内容
    里的 \\* 等得以在「」内原样保留）。"""
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
