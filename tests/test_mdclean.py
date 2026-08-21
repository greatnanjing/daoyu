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
    assert md_clean(">> 嵌套引用") == "｜ ｜ 嵌套引用"   # 连续 > 各转一个 ｜（spec §3.1 备注）
    assert md_clean(">>> 三层") == "｜ ｜ ｜ 三层"
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
