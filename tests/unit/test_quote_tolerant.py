"""落点：分块的字和渲染出的页面差几个字时，照样标到那几段（检索按片段直达，A）。

线上 11-16 量过：点检索结果进文章，100 个链接只有 51 个标中，没标中的里分块的字九成以上都在页面上，
差的是有序列表的序号、[!NOTE] 标记、归档头那一行、说话人的写法。数字一个也不容。
"""

from html import escape

from researchlib.chunks import make_chunks
from researchlib.render import render_body
from researchlib.web.quoting import mark_quoted, mark_quoted_parts, occurrence_of

LEAD = "王小明认为渠道费用的变化决定了这门生意的利润结构，经营现金流跟着走，"
BODY = "门店扩张放缓以后，单店收入的增长主要来自客单价，而不是来客数，这一点在年报里写得很清楚。"


def _html(md: str) -> str:
    return str(render_body(md, {}, None))


def _marked_text(html: str) -> list[str]:
    return [block for block in html.split("</") if "is-quoted" in block]


def test_ordered_list_numbers_missing_on_page():
    md = f"{LEAD}\n\n1. {BODY}\n2. 第二条：会员费收入稳定，续费率高，和商品毛利是两回事。\n\n后面一段讲别的公司。"
    html = _html(md)
    assert "<ol>" in html
    chunk = f"{LEAD}\n1. {BODY}\n2. 第二条：会员费收入稳定，续费率高，和商品毛利是两回事。"
    marked, found = mark_quoted(html, chunk)
    assert found
    assert "后面一段讲别的公司" not in "".join(_marked_text(marked))


def test_note_marker_and_stray_stars():
    html = f"<p>{LEAD}</p><blockquote><p>{BODY}</p></blockquote><p>无关的结尾。</p>"
    chunk = f"{LEAD}\n[!NOTE]\n* {BODY}"
    marked, found = mark_quoted(html, chunk)
    assert found and marked.count("is-quoted") == 2


def test_archive_header_line_and_image_names():
    html = f"<p>{LEAD}</p><p>{BODY}</p>"
    chunk = (
        "原文链接：https://example.invalid/abc123.html·原始HTML：raw/articles/abc123.html\n"
        f"{LEAD}\n截屏2025-05-18 19.39.45.png\n{BODY}"
    )
    marked, found = mark_quoted(html, chunk)
    assert found and marked.count("is-quoted") == 2


def test_speaker_labels_written_differently():
    question = "读者问：门店扩张放缓以后，单店收入靠什么涨？是客单价还是来客数？这一点我一直没想明白。"
    answer = "作者答：主要靠客单价。来客数这几年基本持平，年报里写得很清楚，你可以去翻第三节。"
    html = (
        "<p>前面一段讲别的公司的事情，和这里无关。</p>"
        f"<p>网友：{question[4:]}</p><p>王小明：{answer[4:]}</p><p>后面也无关。</p>"
    )
    marked, found = mark_quoted(html, f"{question}\n{answer}")
    assert found and marked.count("is-quoted") == 2
    assert "前面一段" not in "".join(_marked_text(marked))


def test_numbers_are_never_tolerated():
    page = f"<p>{LEAD}{BODY}利润增长90%，现金流也好转。</p>"
    for wrong in ("利润下降80%", "利润增长9%", "利润增长90亿"):
        chunk = f"{LEAD}{BODY}{wrong}，现金流也好转。"
        assert mark_quoted(page, chunk) == (page, False), wrong
    # 序号以外挨着数字的差异也不容：10m 和 10M 不是一回事。
    page = f"<p>{LEAD}{BODY}收入为10M。</p>"
    assert mark_quoted(page, f"{LEAD}{BODY}收入为10m。") == (page, False)


def test_different_wording_is_not_a_match():
    page = f"<p>{LEAD}</p><p>门店扩张放缓以后，公司把重心放到了线上，外卖占比明显上升，这一点年报里也提了。</p>"
    assert mark_quoted(page, f"{LEAD}\n{BODY}") == (page, False)


def test_same_passage_twice_uses_its_position():
    passage = f"{LEAD}{BODY}"
    page = f"<p>{passage}</p><p>中间隔了一段别的。</p><p>{passage}</p>"
    assert mark_quoted(page, passage) == (page, False)  # 不知道是哪一处就不标
    marked, found = mark_quoted(page, passage, occurrence=1)
    assert found
    first, _rest = marked.split("中间隔了一段别的。")
    assert "is-quoted" not in first and marked.count("is-quoted") == 1
    assert occurrence_of(["开头一块。", passage + "还有下文。", passage], 2) == 1
    assert occurrence_of(["开头一块。", passage], 1) == 0


def test_fuzzy_match_with_copies_written_twice():
    """导出写了两遍的引文页面上收成一份，块里还有两遍，中间还夹着缺图占位。"""
    quote = "信用卡是多边网络的好例子：发卡行、持卡人和商户都在里面，各方都从彼此身上获益，网络在中间收费。"
    html = f"<p>{LEAD}</p><blockquote><p>{quote}</p></blockquote><p>{BODY}</p>"
    chunk = f"{LEAD}\n{quote}\n{quote[:20]}\n[外部图片已省略]\n{quote[20:]}\n{BODY}"
    marked, found = mark_quoted(html, chunk)
    assert found and marked.count("is-quoted") == 3


def test_parts_and_several_chunks_at_once():
    body = "\n\n".join(f"第 {index} 段：王小明讲渠道费用和会员费，" + "经营数据见年报。" * 12 for index in range(1, 16))
    chunks = [text for _heading, text in make_chunks("札记", body)]
    assert len(chunks) >= 3
    html = _html(body)
    pieces = html.split("</p>")
    parts = [piece + "</p>" for piece in pieces if piece.strip()]
    marked, found = mark_quoted_parts(parts, [(chunks[1], None), (chunks[2], None)])
    assert found
    flagged = [part for part in marked if "is-quoted" in part]
    expected = len(chunks[1].split("\n")) + len(chunks[2].split("\n"))
    assert len(flagged) == expected
    untouched = [part for part, before in zip(marked, parts, strict=True) if "is-quoted" not in part]
    assert all(part in parts for part in untouched)
    assert mark_quoted_parts(parts, [("页面上没有的一段话，模型也不该编出来。", None)]) == (parts, False)


def test_html_entities_in_chunk():
    text = "AT&T 和 R&D 的支出占收入一成以上，这一点和同行差不多，管理层说明年还会再加。"
    html = f"<p>{escape(text)}</p>"
    assert mark_quoted(html, text)[1]
