"""长文本分页：gateway 出站与 worker 最终回复共用（放 common，避免 worker→gateway 反向依赖）。"""

from common.mdclean import md_clean

# 微信 iLink 单条文本硬上限：16384 字节 UTF-8（2026-08-20 真机实测钉死：
# 16384 ✓ / 16385 ✗ errmsg='prepare failed'，且 errcode 仍为 0 → 静默不投递）。
# 上限按字节而非字符（同批实证：中文 5450 字≈16350B ✓ / 5500 字≈16500B ✗；
# 纯 ASCII 12000 字=12000B ✓）。页字节数压在上限之下留余量（页码前缀+边界抖动），
# 无论 page_char_limit 配多大都保证单页不越线。
MAX_PAGE_BYTES = 15000


def split_text(text: str, limit: int) -> list[str]:
    """超长文本分页：字符数与 UTF-8 字节数**双上限**，逐字符累积、永不切碎字符。

    未超限原样返回；超限时每页加 "(第 i/N 页)" 前缀（前缀额外占空间，上限近似值）。
    字节上限 MAX_PAGE_BYTES 是硬保护——微信单条 16384 字节按字节计，中文最坏
    3 字节/字，即便 page_char_limit 调高，字节上限兜底防"errcode=0 静默丢消息"。
    """
    if len(text) <= limit and len(text.encode("utf-8")) <= MAX_PAGE_BYTES:
        return [text]
    pages, cur, chars, nbytes = [], [], 0, 0
    for ch in text:
        b = len(ch.encode("utf-8"))
        # cur 非空才切页：单字符（≤4B）不可能自身超限，避免死循环
        if cur and (chars + 1 > limit or nbytes + b > MAX_PAGE_BYTES):
            pages.append("".join(cur))
            cur, chars, nbytes = [], 0, 0
        cur.append(ch)
        chars += 1
        nbytes += b
    if cur:
        pages.append("".join(cur))
    if len(pages) == 1:
        return pages
    return [f"(第 {i}/{len(pages)} 页)\n{p}" for i, p in enumerate(pages, 1)]


def outbox_sent_pages(rows, page_char_limit: int,
                      md_clean_enabled: bool = False) -> int:
    """已送达 outbox 行折算微信侧实际发送条数（出站日计数口径，gateway 出站
    协程运行时与 bridge /status、重启恢复共用——三处必须同一折算）：
    文本行 = len(split_text(md_clean 后文本))——与运行时「先清洗后分页」一致
    （M5C2 起四处口径同步，折算仍是近似值：重试重发的页运行时会再计）；
    图片/文件行（kind='image'/'file'）= 媒体条 1 条 + caption（非空时）1 条。

    行需含 kind/text/caption 键（sqlite3.Row 或 dict 均可）。"""
    n = 0
    for r in rows:
        if r["kind"] in ("image", "file"):
            cap = str(r["caption"] or "").strip()
            if md_clean_enabled:
                # 与运行时同构：清洗后判空。md_clean 转写保内容（规则均要求
                # 紧贴非空白字符），非空 caption 清洗后必非空——此判定在现行
                # 规则下等价，加清洗只为口径绝对一致。
                cap = md_clean(cap).strip()
            n += 1 + (1 if cap else 0)
        else:
            t = md_clean(r["text"]) if md_clean_enabled else r["text"]
            n += len(split_text(t, page_char_limit))
    return n
