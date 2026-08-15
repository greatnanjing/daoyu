"""长文本分页：gateway 出站与 worker 最终回复共用（放 common，避免 worker→gateway 反向依赖）。"""


def split_text(text: str, limit: int) -> list[str]:
    """超长文本分页（M1 按字符数切，UTF-16 代理对安全——不切字节）。

    未超限原样返回；超限时每页加 "(第 i/N 页)" 前缀（前缀额外占字符，上限近似值）。
    """
    if len(text) <= limit:
        return [text]
    pages = [text[i:i + limit] for i in range(0, len(text), limit)]
    return [f"(第 {i}/{len(pages)} 页)\n{p}" for i, p in enumerate(pages, 1)]
