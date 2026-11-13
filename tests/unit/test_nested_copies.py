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
        # 嵌套列表不动。
        "- Parent item with a nested list\n  - child entry\n\nParent item with a nested list child entry",
        # 只有一行。
        "> A single quoted line without any copy.",
        # 引文中间一句正文又写了一遍（开头、结尾都多出字）：不是导出的两份。
        "> Opening words of the quote come first. The middle sentence is repeated on purpose by the author. "
        "Closing words.\n\nThe middle sentence is repeated on purpose by the author.",
        # 结尾接上的那截太短：正文碰巧把列表项最后半句又写了一遍。
        "- A longer list item that keeps going for quite a while here. Short end here.\n\nShort end here.",
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


_IMG = "/media/ab/" + "a" * 64 + ".png"
_CHART = "/media/cd/" + "c" * 64 + ".png"


def test_quote_written_in_paragraphs_is_one_copy():
    """纯文字那份也分段（段间是「>」空行）：整条引文算一个外层，带格式那几段放回去。"""
    md = (
        "> Q: How do you think about the users who are not logged in?\n"
        ">\n"
        "> CEO: We will use the same ad units.\n"
        "\n"
        "**Q:** How do you think about the users who are not logged in?\n"
        "\n"
        "**CEO:** We will use the same [ad units](https://example.invalid/ads).\n"
        "\n"
        "My own commentary follows."
    )
    assert fold_nested_copies(md) == (
        "> **Q:** How do you think about the users who are not logged in?\n"
        ">\n"
        "> **CEO:** We will use the same [ad units](https://example.invalid/ads).\n"
        "\n"
        "My own commentary follows."
    )


def test_images_between_the_formatted_copies_go_back_into_the_quote():
    """引文里的图在纯文字那份里没有，夹在带格式那几段中间：跟着放回引文，不算字；连着两遍的只留一张。"""
    md = (
        "> The first computer games were little more than text:\n"
        "> Images followed, usually of the bitmap variety.\n"
        "\n"
        "The first computer games were little more than *text*:\n"
        "\n"
        "[](https://example.invalid/full-size.png)\n"
        "\n"
        f"![A screenshot of an old game]({_IMG})\n"
        "\n"
        f"![A screenshot of an old game]({_IMG})\n"
        "\n"
        "Images followed, usually of the [bitmap](https://example.invalid/bitmap) variety.\n"
        "\n"
        f"![A chart drawn after the quote]({_CHART})\n"
        "\n"
        "My own commentary follows."
    )
    folded = fold_nested_copies(md)
    assert folded == (
        "> The first computer games were little more than *text*:\n"
        ">\n"
        "> [](https://example.invalid/full-size.png)\n"
        ">\n"
        f"> ![A screenshot of an old game]({_IMG})\n"
        ">\n"
        "> Images followed, usually of the [bitmap](https://example.invalid/bitmap) variety.\n"
        "\n"
        # 最后一段后面的图不知道是不是引文里的，留在外面。
        f"![A chart drawn after the quote]({_CHART})\n"
        "\n"
        "My own commentary follows."
    )
    html = str(render_body(folded, {_IMG: _IMG, _CHART: _CHART}, None))
    quote = html.split("<blockquote>", 1)[1].split("</blockquote>", 1)[0]
    assert quote.count("<img") == 1 and "bitmap</a> variety." in quote
    assert html.count("little more than") == 1


def test_the_same_image_written_twice_shows_once():
    md = (
        "Here is a chart of revenue by quarter:\n"
        "\n"
        f"![Revenue by quarter]({_IMG})\n"
        "\n"
        f"![Revenue by quarter]({_IMG})\n"
        "\n"
        "Revenue kept growing."
    )
    assert fold_nested_copies(md) == (
        f"Here is a chart of revenue by quarter:\n\n![Revenue by quarter]({_IMG})\n\nRevenue kept growing."
    )
    # 两张不一样的图、中间隔着字的同一张图，都照样留着。
    for kept in (
        f"![One]({_IMG})\n\n![Two]({_CHART})",
        f"![Same]({_IMG})\n\nWords in between.\n\n![Same]({_IMG})",
        f"![Same]({_IMG}) with a caption\n\n![Same]({_IMG}) with a caption",
    ):
        assert fold_nested_copies(kept) == kept, kept


def test_images_alone_do_not_make_a_copy():
    # 外层后面只有图、没有接得上的段落：不收。
    md = f"> A quoted line that is long enough.\n\n![Picture]({_IMG})\n\nSomething else entirely."
    assert fold_nested_copies(md) == md


def test_quotes_and_lists_inside_the_copies_go_back_in_too():
    """原文的引文里还套着引文、列表：导出只写一遍，夹在带格式那几段中间，按整块的字接。"""
    md = (
        "> From the encyclopedia definition:\n"
        "> Web 2.0 refers to websites that emphasize user-generated content.\n"
        "> Seventeen years on there is more user-generated content than ever.\n"
        "\n"
        "From the [encyclopedia](https://example.invalid/wiki) definition:\n"
        "\n"
        "> Web 2.0 refers to websites that emphasize user-generated content.\n"
        "\n"
        "Seventeen years on there is more user-generated content than ever.\n"
        "\n"
        "Between the two quotes.\n"
        "\n"
        "> The plan has two parts:\n"
        "> Build the app\n"
        "> Ship it first on one phone\n"
        "> Then on the other\n"
        "> Grow the team\n"
        "\n"
        "The plan has [two parts](https://example.invalid/plan):\n"
        "\n"
        "1. Build the app\n"
        "    - Ship it first on one phone\n"
        "    - Then on the other\n"
        "2. Grow the team\n"
        "\n"
        "My own commentary follows."
    )
    folded = fold_nested_copies(md)
    assert folded == (
        "> From the [encyclopedia](https://example.invalid/wiki) definition:\n"
        ">\n"
        "> > Web 2.0 refers to websites that emphasize user-generated content.\n"
        ">\n"
        "> Seventeen years on there is more user-generated content than ever.\n"
        "\n"
        "Between the two quotes.\n"
        "\n"
        "> The plan has [two parts](https://example.invalid/plan):\n"
        ">\n"
        "> 1. Build the app\n"
        ">     - Ship it first on one phone\n"
        ">     - Then on the other\n"
        "> 2. Grow the team\n"
        "\n"
        "My own commentary follows."
    )
    html = _html(folded)
    assert html.count("user-generated content.") == 1 and html.count("Ship it first") == 1
    first, second = html.split("<p>Between the two quotes.</p>")
    assert first.count("<blockquote>") == 2 and "definition:</p>\n<blockquote>\n<p>Web 2.0" in first
    # 嵌套列表的缩进跟着带过去，还是两层。
    assert second.count("<blockquote>") == 1 and "<li>Build the app<ul>" in second
    assert "Grow the team</li>\n</ol>\n</blockquote>" in second


def test_a_quote_inside_a_quote_written_twice_folds_inside_out():
    """引文里套着的引文自己也写了两遍：先收里面那条，外层下一轮才接得上；只有「[…]」的段落不占字。"""
    md = (
        "> This is why the copy works for the company and its users.\n"
        "> You cannot just recreate another product.\n"
        "> […]\n"
        "> You have a completely different audience.\n"
        "> That right there is the tell.\n"
        "\n"
        "This is why the copy [works](https://example.invalid/works) for the company and its users.\n"
        "\n"
        "> You cannot just recreate another product.\n"
        "> […]\n"
        "> You have a completely different audience.\n"
        "\n"
        "You cannot just recreate another product.\n"
        "\n"
        "[…]\n"
        "\n"
        "You have a *completely* different audience.\n"
        "\n"
        "That right there is the tell."
    )
    assert fold_nested_copies(md) == (
        "> This is why the copy [works](https://example.invalid/works) for the company and its users.\n"
        ">\n"
        "> > You cannot just recreate another product.\n"
        "> >\n"
        "> > […]\n"
        "> >\n"
        "> > You have a *completely* different audience.\n"
        ">\n"
        "> That right there is the tell."
    )


def test_a_quote_written_twice_at_the_end_of_another_quote_keeps_its_formatted_copy():
    """里面那条引文在外层的最后：外层只拿它纯文字那份也接得上，这时也先收里面那条，带格式的那份不落在外面。"""
    md = (
        "> The release explains the change and ends with a quote from the team.\n"
        "> We built this for people who read a lot.\n"
        "\n"
        "The release explains the change and ends with a [quote](https://example.invalid/q) from the team.\n"
        "\n"
        "> We built this for people who read a lot.\n"
        "\n"
        "We built this for people who *read* a lot.\n"
        "\n"
        "My own commentary follows."
    )
    assert fold_nested_copies(md) == (
        "> The release explains the change and ends with a [quote](https://example.invalid/q) from the team.\n"
        ">\n"
        "> > We built this for people who *read* a lot.\n"
        "\n"
        "My own commentary follows."
    )


def test_item_lead_in_stays_and_the_nested_quote_goes_back_in():
    """原文的列表项先是一句导语，下面套着引文。导出把整项（导语连着引文）写成一行纯文字，带格式的那份
    只写了引文，导语不再写。收的时候导语留在列表项里，引文套回这一项下面。"""
    md = (
        "A few additional points worth noting:\n\n"
        "- Video is not a separate unit; it is a setting you bid on. The chief explains: "
        "Video does displace other posts in the feed. Video is just a format that is bid into the auction.\n\n"
        "> Video does displace other posts in the [feed](https://example.com/feed). "
        "Video is just a format that is bid into the auction.\n\n"
        "My own commentary follows."
    )
    folded = fold_nested_copies(md)
    assert folded == (
        "A few additional points worth noting:\n\n"
        "- Video is not a separate unit; it is a setting you bid on. The chief explains:\n\n"
        "    > Video does displace other posts in the [feed](https://example.com/feed). "
        "Video is just a format that is bid into the auction.\n\n"
        "My own commentary follows."
    )
    html = _html(folded)
    assert html.count("Video does displace") == 1
    item = html.split("<li>", 1)[1].split("</li>", 1)[0]
    assert "<p>Video is not a separate unit; it is a setting you bid on. The chief explains:</p>" in item
    assert "<blockquote>" in item and 'href="https://example.com/feed"' in item
    assert "My own commentary follows." not in item


def test_item_lead_in_with_paragraphs_and_an_image_after_it():
    """导语后面带格式的是几段普通段落，第一段里还夹着图（图的替代文字纯文字那份里没有，不算字）。"""
    md = (
        "- This slide was amusing with regards to who it did not include: The music service has not said "
        "whether it plans to use the new feature. Here is more evidence that the two companies are friends.\n\n"
        f"![The list of music services]({_IMG}) The music service has not said whether it plans to use the "
        "new feature.\n\n"
        "Here is more evidence that the two [companies](https://example.com/c) are friends.\n\n"
        "Next paragraph of the article."
    )
    folded = fold_nested_copies(md)
    assert folded.count("has not said whether") == 1 and folded.count("more evidence") == 1
    assert folded.startswith("- This slide was amusing with regards to who it did not include:\n\n    ![")
    assert "\n\n    Here is more evidence that the two [companies]" in folded
    html = str(render_body(folded, {_IMG: _IMG}, None))
    item = html.split("<li>", 1)[1].split("</li>", 1)[0]
    assert item.count("<p>") == 3 and "<img" in item


def test_tweet_signature_stays_after_the_formatted_text():
    """推文：引文里正文一段，署名直接写在引文里。导出带格式的那份只写了正文，署名留在引文末尾。"""
    md = (
        "Here is what one investor wrote:\n\n"
        "> This has been the story of every company that has gone there. Why does nobody learn it?\n"
        "> https://t.co/abc123\n"
        "> — 王小明 (@wangxm)\n"
        "> May 21, 2025\n\n"
        "This has been the story of every company that has gone there. Why does nobody learn it? "
        "[https://t.co/abc123](https://t.co/abc123)\n\n"
        "What I believe he means is something else."
    )
    assert fold_nested_copies(md) == (
        "Here is what one investor wrote:\n\n"
        "> This has been the story of every company that has gone there. Why does nobody learn it? "
        "[https://t.co/abc123](https://t.co/abc123)\n"
        ">\n"
        "> — 王小明 (@wangxm)\n"
        "> May 21, 2025\n\n"
        "What I believe he means is something else."
    )


def test_nested_list_written_flat_goes_back_under_its_item():
    """原文的列表项下面挂着嵌套列表：纯文字那份把嵌套列表的字连在这一项后面，带格式的那几项顶格写，
    和后面正文自己的项挨着。"""
    md = (
        "- There will be three tiers of games on the service: All games go to subscribers of the new package. "
        "A smaller number of games go to the cheaper plan.\n\n"
        "- All games go to subscribers of the new package.\n\n"
        "- A smaller number of games go to the cheaper plan.\n\n"
        "- The league keeps the right to sell local rights to others."
    )
    assert fold_nested_copies(md) == (
        "- There will be three tiers of games on the service:\n\n"
        "    - All games go to subscribers of the new package.\n\n"
        "    - A smaller number of games go to the cheaper plan.\n\n"
        "- The league keeps the right to sell local rights to others."
    )


def test_long_interview_quote_with_many_paragraphs_folds():
    """访谈记录这类长引文一条几十段（线上 22699 一条 40 多段）：以前一条只认 12 段，整段记录显示两遍。"""
    lines = [f"Speaker {number}: This is paragraph number {number} of a long interview transcript."
             for number in range(30)]
    copies = [f"**Speaker {number}:** This is paragraph number {number} of a long interview transcript."
              for number in range(30)]
    md = "> " + "\n>\n> ".join(lines) + "\n\n" + "\n\n".join(copies) + "\n\nMy own commentary follows."
    folded = fold_nested_copies(md)
    assert folded.count("paragraph number 29 ") == 1 and folded.count("**Speaker") == 30
    assert folded.startswith("> **Speaker 0:**") and folded.endswith("\n\nMy own commentary follows.")
