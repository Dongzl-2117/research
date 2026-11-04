"""导出时引文、列表项写了两遍：单看原文时收成一份，带格式的那份放回去。"""

from researchlib.chunks import make_chunks
from researchlib.nested_copies import fold_nested_copies
from researchlib.render import render_body
from researchlib.web.quoting import mark_quoted


def _html(md: str) -> str:
    return str(render_body(md, {}, None))


def test_quote_followed_by_its_copy_shows_once():
    md = "Opening paragraph.\n\n> A note on these numbers.\n\nA note on these numbers.\n\nNext paragraph here."
    folded = fold_nested_copies(md)
    assert folded == "Opening paragraph.\n\n> A note on these numbers.\n\nNext paragraph here."
    html = _html(folded)
    assert html.count("A note on these numbers.") == 1
    assert "<blockquote>" in html


def test_two_paragraph_quote_takes_back_both_copies_with_links():
    md = (
        "> The first quoted paragraph talks about the ads business , at length.\n"
        "> The second quoted paragraph ends the quote.\n"
        "\n"
        "The first quoted paragraph talks about the [ads business](https://example.com/ads), at length.\n"
        "\n"
        "The second quoted paragraph ends the quote.\n"
        "\n"
        "My own commentary follows."
    )
    folded = fold_nested_copies(md)
    assert folded == (
        "> The first quoted paragraph talks about the [ads business](https://example.com/ads), at length.\n"
        ">\n"
        "> The second quoted paragraph ends the quote.\n"
        "\n"
        "My own commentary follows."
    )
    html = _html(folded)
    assert html.count("ends the quote.") == 1
    quote = html.split("<blockquote>", 1)[1].split("</blockquote>", 1)[0]
    assert quote.count("<p>") == 2
    assert 'href="https://example.com/ads"' in quote
    assert "My own commentary follows." not in quote


def test_list_item_takes_the_formatted_copy_and_its_second_paragraph():
    md = (
        "Each of the three businesses is in a similar spot.\n"
        "\n"
        "- Search: The first point is about the ads business , which keeps growing. "
        "The second paragraph of the same item adds a caveat.\n"
        "\n"
        "**Search:** The first point is about the [ads business](https://example.com/ads), which keeps growing.\n"
        "\n"
        "The second paragraph of the same item adds a caveat.\n"
        "\n"
        "- Video: The plain item has no copy at all.\n"
        "\n"
        "Closing thoughts."
    )
    folded = fold_nested_copies(md)
    assert folded == (
        "Each of the three businesses is in a similar spot.\n"
        "\n"
        "- **Search:** The first point is about the [ads business](https://example.com/ads), which keeps growing.\n"
        "\n"
        "    The second paragraph of the same item adds a caveat.\n"
        "\n"
        "- Video: The plain item has no copy at all.\n"
        "\n"
        "Closing thoughts."
    )
    html = _html(folded)
    assert html.count("adds a caveat.") == 1
    assert html.count("which keeps growing.") == 1
    item = html.split("<li>", 1)[1].split("</li>", 1)[0]
    assert "<strong>Search:</strong>" in item
    assert 'href="https://example.com/ads"' in item
    assert "adds a caveat." in item
    assert "Closing thoughts." in html.split("</ul>")[-1]


def test_footnotes_keep_the_linked_return_arrow():
    md = (
        "Body text.\n"
        "\n"
        "- Kind of like this one ↩\n"
        "\n"
        "Kind of like this one [↩](#rf1-1000)\n"
        "\n"
        "- Also known as the long tail ↩\n"
        "\n"
        "Also known as the [long tail](https://example.com/tail) [↩](#rf2-1000)"
    )
    folded = fold_nested_copies(md)
    assert folded == (
        "Body text.\n"
        "\n"
        "- Kind of like this one [↩](#rf1-1000)\n"
        "\n"
        "- Also known as the [long tail](https://example.com/tail) [↩](#rf2-1000)"
    )
    html = _html(folded)
    assert html.count("<li>") == 2
    assert html.count("Kind of like this one") == 1


def test_chinese_quote_copy_is_folded_too():
    md = "> 这一段引文在导出时又写了一遍。\n\n这一段引文在导出时又写了一遍。\n\n后面是正文。"
    assert fold_nested_copies(md) == "> 这一段引文在导出时又写了一遍。\n\n后面是正文。"


def test_leaves_everything_else_alone():
    cases = [
        # 太短：碰巧一样的短句不当成两份。
        "> Yes.\n\nYes.",
        # 差一个词就不是副本。
        "> The quoted paragraph says one thing.\n\nThe quoted paragraph says another thing.",
        # 目录列表后面的小节标题：不紧挨着它自己那一项。
        "- Introduction to the topic\n- The problem with pricing\n\nIntroduction to the topic\n\nBody.",
        # 后面那段更长：是接着往下写，不是副本。
        "- The plan is simple enough\n\nThe plan is simple enough, and here is why it works.",
        # 围栏代码里的「>」不是引文。
        "```\n> quoted inside code block\n\nquoted inside code block\n```",
        # 引文后面紧跟着的是列表，不是普通段落。
        "> First item of the quoted list\n\n- First item of the quoted list",
        # 嵌套列表不动。
        "- Parent item with a nested list\n  - child entry\n\nParent item with a nested list child entry",
        # 只有一行。
        "> A single quoted line without any copy.",
        "",
    ]
    for md in cases:
        assert fold_nested_copies(md) == md, md


def test_unrelated_paragraph_after_the_copy_stays():
    md = "> Quoted words worth repeating.\n\nQuoted words worth repeating.\n\nQuoted words worth repeating."
    # 只收紧跟着的那一份；再往后同样的一段是正文自己写的，留着。
    assert fold_nested_copies(md) == "> Quoted words worth repeating.\n\nQuoted words worth repeating."


def test_quoted_chunk_highlight_continues_past_a_folded_copy():
    """检索、问库的段落块是照库里原样切的，还是两遍。页面收成一份后，高亮照样标到块的最后一段。"""
    body = (
        "The opening paragraph sets up the argument about three businesses.\n\n"
        "> A note on these numbers comes first, and it is long enough to matter here.\n\n"
        "A note on these numbers comes first, and it is long enough to matter here.\n\n"
        "- Search: The first point is about the ads business , which keeps growing every quarter.\n\n"
        "**Search:** The first point is about the [ads business](https://example.com/ads), "
        "which keeps growing every quarter.\n\n"
        "The closing paragraph wraps it up and says what to watch next."
    )
    chunk = make_chunks("Three businesses", body)[0][1]
    marked, found = mark_quoted(_html(fold_nested_copies(body)), chunk)
    assert found and marked.count("is-quoted") == 4
    # 正文自己连写两遍的段落（不是引文、列表项，不收），两段照样都标上。
    twice = "Words written twice on purpose."
    plain = f"Opening words that start the chunk.\n\n{twice}\n\n{twice}\n\nEnd."
    marked, found = mark_quoted(_html(plain), make_chunks("T", plain)[0][1])
    assert found and marked.count("is-quoted") == 4
