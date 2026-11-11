"""导出里写了两遍的引文、列表项、脚注：对照页和只看中文页按原文页同一条规则收成一份。

Stratechery 的归档导出先把整条引文、整个列表项、每条脚注写成一行纯文字（链接、加粗都没了，
脚注末尾的「↩」也不是链接），再把里面每一段带着格式写一遍（``researchlib.nested_copies``）。
原文页早就按「去掉格式后正好相等」收成一份。对照页以前只收 80 字以上的长重复：短引文、短脚注
左右两栏各出现两次，收长的时候还常留下纯文字那份，丢了分段、加粗和链接；只看中文页两份都在。
"""

import re

from researchlib.render import render_body
from researchlib.translate import provenance as pr
from researchlib.translate.display import (
    _fold_copy_pairs,
    article_pairs,
    content_gaps,
    drop_repeated_images,
    fold_translated_copies,
    join_split_paragraphs,
)
from researchlib.translate.pair import BilingualPair, stamp_paragraph_ids
from researchlib.translate.segments import splice_segments

_MD = (
    "The body paragraph mentions two notes at the end of the piece.\n\n"
    "> A note on these numbers comes first.\n\n"
    "A note on these [numbers](https://example.invalid/numbers) comes first.\n\n"
    "1. The first point is about the ads business , which keeps growing.\n\n"
    "The first point is about the **ads business**, which keeps growing.\n\n"
    "2. The second point is about video and the new app.\n\n"
    "The second point is about [video](https://example.invalid/video) and the new app.\n\n"
    "- I mostly listen to sports podcasts, so there is no need to email me ↩\n\n"
    "I mostly listen to sports podcasts, so there is no need to email me [↩](#rf1-1)\n\n"
    "- The second footnote is short ↩\n\n"
    "The second footnote is short [↩](#rf2-1)\n"
)
# 两份各自译：纯文字那份的中文和带格式那份只差几个字。
_ZH = {
    0: "<p>正文这一段在结尾提到两条注释。</p>",
    1: "<p>先说一下这些数字。</p>",
    2: '<p>先说一下这些<a href="https://example.invalid/numbers">数字</a>。</p>',
    3: "<p>第一点是广告业务，它还在增长。</p>",
    4: "<p>第一点是<strong>广告业务</strong>，它还在增长。</p>",
    5: "<p>第二点是视频和新应用。</p>",
    6: '<p>第二点是<a href="https://example.invalid/video">视频</a>和新应用。</p>',
    7: "<p>我主要听体育播客，所以不用发邮件问我 ↩</p>",
    8: '<p>我主要听体育播客，所以不用发邮件来问我 <a href="#rf1-1">↩</a></p>',
    9: "<p>第二条注释很短 ↩</p>",
    10: '<p>第二条注释很短 <a href="#rf2-1">↩</a></p>',
}
_EN_TEXTS = ("A note on these", "The first point is about", "The second point is about", "I mostly listen",
             "The second footnote")
_ZH_TEXTS = ("先说一下这些", "第一点是", "第二点是", "我主要听体育播客", "第二条注释很短")


def _render(md: str = _MD, zh: dict[int, str] = _ZH, images: dict[str, str] | None = None):
    """和文章页一样：两边都编好段落号，对照页拿没并句的译文，只看中文页拿并好句的。"""
    source = stamp_paragraph_ids(str(render_body(md, images or {})))
    manifest = pr.source_manifest(source)
    target = splice_segments(manifest["source_snapshot"], zh)
    view = article_pairs(source, target, provenance=pr.receipt(manifest, target))
    return source, stamp_paragraph_ids(join_split_paragraphs(target)), view


def _ids(value: str) -> set[str]:
    return set(re.findall(r'\bid="(p-\d+)"', value))


def test_short_quote_items_and_footnotes_show_once_with_their_formatting():
    source, _zh, view = _render()
    assert view.correspondence == "mapped" and not view.notice
    assert len(view.pairs) == 6
    en = "".join(pair.en for pair in view.pairs)
    zh = "".join(pair.zh or "" for pair in view.pairs)
    assert all(en.count(text) == 1 for text in _EN_TEXTS)
    assert all(zh.count(text) == 1 for text in _ZH_TEXTS)
    quote, first, second, note, short = view.pairs[1:]
    # 留下的是带链接、加粗的那份，套回引文、列表里。
    assert quote.en.endswith(
        '<blockquote><p>A note on these <a href="https://example.invalid/numbers">numbers</a> comes first.</p>'
        "</blockquote>"
    )
    assert quote.zh == '<blockquote><p>先说一下这些<a href="https://example.invalid/numbers">数字</a>。</p></blockquote>'
    assert "<ol><li><p>The first point is about the <strong>ads business</strong>" in first.en
    assert first.zh == "<ol><li><p>第一点是<strong>广告业务</strong>，它还在增长。</p></li></ol>"
    # 有序列表的序号跟着外壳走。
    assert second.en.count('<ol start="2">') == 1 and second.zh.startswith('<ol start="2"><li>')
    # 脚注的「↩」是回到正文的链接，纯文字那份没有。
    assert '<ul><li><p>I mostly listen to sports podcasts, so there is no need to email me <a href="#rf1-1">' in note.en
    assert note.zh == '<ul><li><p>我主要听体育播客，所以不用发邮件来问我 <a href="#rf1-1">↩</a></p></li></ul>'
    assert '<a href="#rf2-1">↩</a>' in short.zh
    # 原文的词、链接、列表序号一样不少；收掉那几对的段落号留在页面上。
    assert content_gaps(source, en) == 0
    assert _ids(source) <= _ids(en + zh) | {pair.anchor for pair in view.pairs}


def test_only_chinese_page_shows_each_copy_once():
    source, zh, _view = _render()
    folded = fold_translated_copies(source, zh)
    assert all(folded.count(text) == 1 for text in _ZH_TEXTS)
    assert '<blockquote id="p-2">' in folded and '<p id="p-3">先说一下这些<a' in folded
    assert '<ol id="p-6" start="2">' in folded
    assert '<li><p id="p-9">我主要听体育播客，所以不用发邮件来问我 <a href="#rf1-1">↩</a></p></li>' in folded
    # 段落号一个不少，链接到收掉那份的锚点照样跳得到。
    assert _ids(folded) == _ids(zh)
    assert folded.count("<li>") == 4 and "<li>我主要听" not in folded


def test_only_chinese_page_keeps_a_sentence_the_formatted_copy_left_out():
    zh = dict(_ZH)
    zh[3] = "<p>第一点是广告业务，它还在增长。广告主越来越多。</p>"
    source, joined, view = _render(zh=zh)
    folded = fold_translated_copies(source, joined)
    assert folded.count("第一点是") == 1
    assert "第一点是<strong>广告业务</strong>，它还在增长。</p><p>广告主越来越多。</p></li>" in folded
    first = view.pairs[2]
    assert first.zh == "<ol><li><p>第一点是<strong>广告业务</strong>，它还在增长。</p><p>广告主越来越多。</p></li></ol>"


def test_only_chinese_page_lines_up_after_lines_split_at_br():
    """原文里有按 <br> 拆开译的段：顶层块数不一样，按译制时的切法比，照样收。"""
    lines = "<br>\n".join(f"Line {number} of a short poem goes here." for number in range(4))
    md = f"Opening words.\n\n{lines}\n\n" + _MD.split("\n\n", 1)[1]
    source = stamp_paragraph_ids(str(render_body(md, {})))
    manifest = pr.source_manifest(source)
    leaves = len(pr.iter_segments(manifest["source_snapshot"]))
    zh = {0: "<p>开头几句。</p>"}
    zh.update({number: f"<p>第{number}行。</p>" for number in range(1, 5)})
    zh.update({index + 4: value for index, value in _ZH.items() if index})
    assert sorted(zh) == list(range(leaves))
    target = splice_segments(manifest["source_snapshot"], zh)
    joined = stamp_paragraph_ids(join_split_paragraphs(target))
    assert len(re.findall(r'\bid="p-\d+"', joined)) != len(re.findall(r'\bid="p-\d+"', source))
    folded = fold_translated_copies(source, joined)
    assert all(folded.count(text) == 1 for text in _ZH_TEXTS)


def test_only_chinese_page_is_left_alone_when_blocks_do_not_line_up():
    source, zh, _view = _render()
    shifted = "<p>多出来的一段。</p>" + zh
    assert fold_translated_copies(source, shifted) == shifted
    plain = "<p>没有引文和列表的一篇。</p>"
    assert fold_translated_copies("<p>No quotes or lists here at all.</p>", plain) is plain


def _pairs(*items: tuple[str, str]) -> list[BilingualPair]:
    return [BilingualPair(en=en, zh=zh) for en, zh in items]


def test_copies_that_do_not_match_exactly_are_left_to_the_old_rules():
    one_word_off = _pairs(
        ("<ul><li>The plain item says one thing here.</li></ul>", "<ul><li>纯文字那份说了一件事。</li></ul>"),
        ("<p>The plain item says <em>another</em> thing here.</p>", "<p>带格式那份说了另一件事。</p>"),
    )
    assert _fold_copy_pairs(one_word_off) == one_word_off
    # 太短的不认：「Yes.」碰巧和下一段一样，不当成导出写的两份。
    short = _pairs(("<blockquote><p>Yes.</p></blockquote>", "<blockquote><p>是的。</p></blockquote>"),
                   ("<p>Yes.</p>", "<p>是的。</p>"))
    assert _fold_copy_pairs(short) == short
    # 一对里有两个列表项（两边对不齐并成一对）：不能只因为最后一项对得上就整对收掉。
    merged = _pairs(
        ("<ul><li>Another item.</li><li>The second item text.</li></ul>",
         "<ul><li>另一项。</li><li>第二项。</li></ul>"),
        ("<p>The second item text.</p>", "<p>第二项。</p>"),
    )
    assert _fold_copy_pairs(merged) == merged


def test_a_link_only_on_the_plain_copy_keeps_both():
    pairs = _pairs(
        ('<ul><li>The point links to <a href="https://example.invalid/x">a source</a> here.</li></ul>',
         '<ul><li>这一点链接到<a href="https://example.invalid/x">资料</a>。</li></ul>'),
        ("<p>The point links to a source here.</p>", "<p>这一点链接到资料。</p>"),
    )
    assert _fold_copy_pairs(pairs) == pairs


def test_repeat_markers_follow_the_pairs_that_stay():
    pairs = [
        BilingualPair(en="<p>Same paragraph twice.</p>", zh="<p>同一段两遍。</p>"),
        BilingualPair(en="<blockquote><p>A quoted line here.</p></blockquote>",
                      zh="<blockquote><p>引的一行。</p></blockquote>"),
        BilingualPair(en="<p>A quoted line here.</p>", zh="<p>引的一行。</p>"),
        BilingualPair(en="<p>Same paragraph twice.</p>", zh="<p>同一段两遍。</p>", repeat_of=0),
    ]
    folded = _fold_copy_pairs(pairs)
    assert len(folded) == 3
    assert folded[1].en == "<blockquote><p>A quoted line here.</p></blockquote>"
    assert folded[2].repeat_of == 0


def test_chinese_kept_only_on_the_plain_copy_is_moved_over_as_a_paragraph():
    """带格式那份没有中文（和英文一样，不单列）时，纯文字那份的中文挂过去，不带它的列表外壳。"""
    pairs = [
        BilingualPair(en="<ul><li>The plain copy of the item text.</li></ul>",
                      zh="<ul><li>这一项的纯文字那份。</li></ul>"),
        BilingualPair(en="<p>The plain copy of the <strong>item</strong> text.</p>", zh=None),
    ]
    folded = _fold_copy_pairs(pairs)
    assert len(folded) == 1
    assert folded[0].zh == "<ul><li><p>这一项的纯文字那份。</p></li></ul>"


_IMG = "/media/ab/" + "a" * 64 + ".png"
_CHART = "/media/cd/" + "c" * 64 + ".png"
_IMAGES = {_IMG: _IMG, _CHART: _CHART}
# 好几段的引文（纯文字那份也分段）、中间夹着图的引文（图写了两遍）、正文里连着两遍的同一张图。
_MD_MORE = (
    "Opening words of the article body here.\n\n"
    "> Q: How do you think about the users who are not logged in?\n>\n> CEO: We will use the same ad units.\n\n"
    "**Q:** How do you think about the users who are not logged in?\n\n"
    "**CEO:** We will use the same [ad units](https://example.invalid/ads).\n\n"
    "> The first computer games were little more than text:\n> Images followed, usually of the bitmap variety.\n\n"
    "The first computer games were little more than *text*:\n\n"
    f"![A screenshot of an old game]({_IMG})\n\n"
    f"![A screenshot of an old game]({_IMG})\n\n"
    "Images followed, usually of the [bitmap](https://example.invalid/bitmap) variety.\n\n"
    "Closing words after the quote end here.\n\n"
    f"![A chart of the numbers]({_CHART})\n\n"
    f"![A chart of the numbers]({_CHART})\n\n"
    "The chart above shows the trend.\n"
)
_ZH_MORE = {
    0: "<p>正文开头的几句话。</p>",
    1: "<p>问：你怎么看那些没登录的用户？</p>",
    2: "<p>CEO：我们会用同样的广告单元。</p>",
    3: "<p><strong>问：</strong>你怎么看那些没登录的用户？</p>",
    4: '<p><strong>CEO：</strong>我们会用同样的<a href="https://example.invalid/ads">广告单元</a>。</p>',
    5: "<p>最早的电脑游戏几乎只有文字：<br> 后来有了图像，多半是位图。</p>",
    6: "<p>最早的电脑游戏几乎只有<em>文字</em>：</p>",
    7: '<p>后来有了图像，多半是<a href="https://example.invalid/bitmap">位图</a>。</p>',
    8: "<p>引文后面收尾的几句话。</p>",
    9: "<p>上面这张图显示了趋势。</p>",
}
_MORE_TEXTS = ("你怎么看那些没登录的用户", "我们会用同样的", "最早的电脑游戏", "后来有了图像")


def test_quotes_in_paragraphs_and_with_images_show_once_on_the_bilingual_page():
    source, _zh, view = _render(_MD_MORE, _ZH_MORE, _IMAGES)
    assert view.correspondence == "mapped" and not view.notice
    en = "".join(pair.en for pair in view.pairs)
    zh = "".join(pair.zh or "" for pair in view.pairs)
    assert all(zh.count(text) == 1 for text in _MORE_TEXTS)
    assert en.count("not logged in") == 1 and en.count("little more than") == 1
    # 好几段的引文：一段一对，留带格式的，各套回引文外壳。
    question, answer = view.pairs[1:3]
    assert question.en.endswith("<blockquote><p><strong>Q:</strong> How do you think about the users who are not "
                                "logged in?</p></blockquote>")
    assert answer.zh == ('<blockquote><p><strong>CEO：</strong>我们会用同样的<a href="https://example.invalid/ads">'
                         "广告单元</a>。</p></blockquote>")
    # 夹在中间的图跟着放回引文；写了两遍的图只留一张，中文那侧对应的空段落也去掉。
    games = next(pair for pair in view.pairs if "Images followed" in pair.en)
    assert games.en.startswith("<blockquote><p><img") and games.en.count("<img") == 1
    assert games.zh.startswith("<blockquote><p></p><p>后来有了图像")
    chart = view.pairs[-1]
    assert chart.en.count("<img") == 1 and chart.zh == "<p></p><p>上面这张图显示了趋势。</p>"
    # 原文的词、链接、图一样不少；段落号都还在页面上。
    assert content_gaps(source, en) == 0
    assert _ids(source) <= _ids(en + zh) | {pair.anchor for pair in view.pairs}


def test_quotes_in_paragraphs_and_with_images_show_once_on_the_chinese_page():
    source, zh, _view = _render(_MD_MORE, _ZH_MORE, _IMAGES)
    folded = drop_repeated_images(fold_translated_copies(source, zh))
    assert all(folded.count(text) == 1 for text in _MORE_TEXTS)
    assert folded.count(_IMG) == 1 and folded.count(_CHART) == 1
    assert '<blockquote id="p-2"><p id="p-3"><strong>问：</strong>' in folded
    quote = folded.split('<blockquote id="p-5">', 1)[1].split("</blockquote>", 1)[0]
    assert "<em>文字</em>" in quote and _IMG in quote and "位图</a>" in quote
    assert _ids(folded) == _ids(zh)


def test_a_quote_next_to_another_quote_still_finds_its_copy():
    """连着的几对引文先合起来认；合起来对不上时，单独一对的引文照样认得出自己的副本。"""
    pairs = _pairs(
        ("<blockquote><p>An earlier quote that has no copy after it.</p></blockquote>",
         "<blockquote><p>前面一段没有副本的引文。</p></blockquote>"),
        ("<blockquote><p>The second quote is written twice here.</p></blockquote>",
         "<blockquote><p>第二段引文这里写了两遍。</p></blockquote>"),
        ("<p>The second quote is <em>written</em> twice here.</p>", "<p>第二段引文这里<em>写</em>了两遍。</p>"),
    )
    folded = _fold_copy_pairs(pairs)
    assert len(folded) == 2
    assert folded[1].en == "<blockquote><p>The second quote is <em>written</em> twice here.</p></blockquote>"


def test_a_quote_inside_the_copies_goes_back_into_the_item():
    """带格式的几段里夹着引文（原文的列表项里套着引文，导出只写一遍）：按整块的字接，一起套回列表项。"""
    nested = _pairs(
        ("<ul><li>He said this. The quoted words follow.</li></ul>", "<ul><li>他这么说。下面是引的话。</li></ul>"),
        ("<p>He said <em>this</em>.</p>", "<p>他这么<em>说</em>。</p>"),
        ("<blockquote><p>The quoted words follow.</p></blockquote>", "<blockquote><p>下面是引的话。</p></blockquote>"),
    )
    folded = _fold_copy_pairs(nested)
    assert [pair.en for pair in folded] == [
        "<ul><li><p>He said <em>this</em>.</p></li></ul>",
        '<ul><li class="tx-list-continuation"><blockquote><p>The quoted words follow.</p></blockquote></li></ul>',
    ]
    assert folded[1].zh == (
        '<ul><li class="tx-list-continuation"><blockquote><p>下面是引的话。</p></blockquote></li></ul>'
    )


def test_a_folded_quote_does_not_take_the_next_paragraph_in_a_later_round():
    """收好的引文下一轮只当别的外层里的一块：正文自己接着写的同一段不会被收进去。"""
    pairs = _pairs(
        ("<blockquote><p>Quoted words worth repeating here.</p></blockquote>",
         "<blockquote><p>值得再说一遍的引文。</p></blockquote>"),
        ("<p>Quoted words worth <em>repeating</em> here.</p>", "<p>值得再<em>说</em>一遍的引文。</p>"),
        ("<p>Quoted words worth repeating here.</p>", "<p>值得再说一遍的引文。</p>"),
    )
    folded = _fold_copy_pairs(pairs)
    assert [pair.en for pair in folded] == [
        "<blockquote><p>Quoted words worth <em>repeating</em> here.</p></blockquote>",
        "<p>Quoted words worth repeating here.</p>",
    ]


# 引文里还套着引文、列表（导出只写一遍）；引文里套着的引文自己也写了两遍（中间还有一段只有「[…]」）。
_MD_NESTED = (
    "Opening words of the article body here.\n\n"
    "> From the encyclopedia definition:\n"
    "> Web 2.0 refers to websites that emphasize user-generated content.\n"
    "> Seventeen years on there is more user-generated content than ever.\n\n"
    "From the [encyclopedia](https://example.invalid/wiki) definition:\n\n"
    "> Web 2.0 refers to websites that emphasize user-generated content.\n\n"
    "Seventeen years on there is more user-generated content than ever.\n\n"
    "> The release listed two changes:\n> Faster sync for large libraries\n> Offline mode for the mobile app\n\n"
    "The release listed [two changes](https://example.invalid/notes):\n\n"
    "- Faster sync for large libraries\n- Offline mode for the mobile app\n\n"
    "> This is why the copy works for the company and its users.\n"
    "> The CEO explained what they are trying to do:\n"
    "> You cannot just recreate another product.\n> […]\n> You have a completely different audience.\n"
    "> That right there is the tell.\n\n"
    "This is why the copy [works](https://example.invalid/works) for the company and its users.\n\n"
    "The CEO explained what they are trying to do:\n\n"
    "> You cannot just recreate another product.\n> […]\n> You have a completely different audience.\n\n"
    "You cannot just recreate another product.\n\n"
    "[…]\n\n"
    "You have a completely different audience.\n\n"
    "That right there is the tell.\n\n"
    "Closing words of the article body.\n"
)
_ZH_NESTED = {
    0: "<p>正文开头的几句话。</p>",
    1: "<p>百科的定义：Web 2.0 指强调用户生成内容的网站。</p>",
    2: "<p>十七年过去，用户生成的内容比以往都多。</p>",
    3: '<p>百科的<a href="https://example.invalid/wiki">定义</a>：</p>',
    4: "<p>Web 2.0 指强调用户生成内容的网站。</p>",
    5: "<p>十七年过去，用户生成的内容比以往都多。</p>",
    6: "<p>这次更新列了两处改动：大型资料库同步更快 手机应用可以离线用</p>",
    7: '<p>这次更新列了<a href="https://example.invalid/notes">两处改动</a>：</p>',
    8: "<li>大型资料库同步更快</li>",
    9: "<li>手机应用可以离线用</li>",
    10: "<p>这就是照搬对公司和用户都管用的原因。</p>",
    11: "<p>CEO 讲了他们想做什么：你不能只是复制另一个产品。</p>",
    12: "<p>[…] 你面对的是完全不同的受众。</p>",
    13: "<p>这就是关键所在。</p>",
    14: '<p>这就是照搬对公司和用户都<a href="https://example.invalid/works">管用</a>的原因。</p>',
    15: "<p>CEO 讲了他们想做什么：</p>",
    16: "<p>你不能只是复制另一个产品。</p>",
    17: "<p>[…] 你面对的是完全不同的受众。</p>",
    18: "<p>你不能只是复制另一个产品。</p>",
    19: "<p>你面对的是完全不同的受众。</p>",
    20: "<p>这就是关键所在。</p>",
    21: "<p>正文最后几句。</p>",
}
_NESTED_TEXTS = ("百科的", "指强调用户生成内容", "十七年过去", "这次更新列了", "大型资料库", "手机应用", "这就是照搬",
                 "CEO 讲了", "你不能只是", "完全不同的受众", "这就是关键所在")


def test_quotes_and_lists_inside_the_copies_show_once_on_the_bilingual_page():
    source, _zh, view = _render(_MD_NESTED, _ZH_NESTED)
    assert view.correspondence == "mapped" and not view.notice
    en = "".join(pair.en for pair in view.pairs)
    zh = "".join(pair.zh or "" for pair in view.pairs)
    assert all(zh.count(text) == 1 for text in _NESTED_TEXTS)
    assert en.count("user-generated content.") == 1 and en.count("Faster sync") == 1
    assert en.count("You cannot just") == 1 and en.count("the tell") == 1
    # 套着的引文、列表跟着放回外层的引文里。
    assert "<blockquote><blockquote>\n<p>Web 2.0 指强调" in zh
    assert "<blockquote><ul><li>大型资料库同步更快</li></ul></blockquote>" in zh
    inner = next(pair for pair in view.pairs if "You cannot just" in pair.en)
    assert inner.en.startswith("<blockquote>") and inner.en.count("<blockquote>") == 2
    assert content_gaps(source, en) == 0
    assert _ids(source) <= _ids(en + zh) | {pair.anchor for pair in view.pairs}


def test_quotes_and_lists_inside_the_copies_show_once_on_the_chinese_page():
    source, zh, _view = _render(_MD_NESTED, _ZH_NESTED)
    folded = fold_translated_copies(source, zh)
    assert all(folded.count(text) == 1 for text in _NESTED_TEXTS)
    first = folded.split('<blockquote id="p-2">', 1)[1].split("十七年过去", 1)[0]
    assert '<a href="https://example.invalid/wiki">定义</a>' in first and "<blockquote" in first
    release = folded.split('<blockquote id="p-6">', 1)[1].split("</blockquote>", 1)[0]
    assert "两处改动</a>" in release and "<ul" in release
    # 里面那条引文先收好，外层下一轮接上：两层引文，「这就是关键所在」还在外层里。
    tell = folded.split('<blockquote id="p-9">', 1)[1].split("正文最后几句", 1)[0]
    inner = tell.split("<blockquote", 1)[1].split("</blockquote>", 1)[0]
    assert "你不能只是" in inner and "[…]" in inner and "完全不同的受众" in inner
    assert tell.count("</blockquote>") == 2 and tell.index("这就是关键所在") < tell.rindex("</blockquote>")
    assert _ids(folded) == _ids(zh)


def test_a_quote_written_twice_at_the_end_of_another_quote_keeps_its_formatted_copy():
    pairs = _pairs(
        ("<blockquote><p>The release explains the change and ends with a quote from the team. "
         "We built this for people who read a lot.</p></blockquote>",
         "<blockquote><p>说明讲了改动，最后引了团队的话。我们是为爱读书的人做的。</p></blockquote>"),
        ('<p>The release explains the change and ends with a <a href="https://example.invalid/q">quote</a> '
         "from the team.</p>",
         '<p>说明讲了改动，最后<a href="https://example.invalid/q">引了</a>团队的话。</p>'),
        ("<blockquote><p>We built this for people who read a lot.</p></blockquote>",
         "<blockquote><p>我们是为爱读书的人做的。</p></blockquote>"),
        ("<p>We built this for people who <em>read</em> a lot.</p>", "<p>我们是为爱<em>读书</em>的人做的。</p>"),
    )
    folded = _fold_copy_pairs(pairs)
    assert len(folded) == 2
    assert folded[0].en.endswith(
        '<blockquote><p>The release explains the change and ends with a <a href="https://example.invalid/q">quote</a> '
        "from the team.</p></blockquote>"
    )
    assert folded[1].en == "<blockquote><blockquote><p>We built this for people who <em>read</em> a lot.</p>" \
        "</blockquote></blockquote>"
    assert folded[1].zh == "<blockquote><blockquote><p>我们是为爱<em>读书</em>的人做的。</p></blockquote></blockquote>"


# 外层的引文一开头就套着引文：纯文字那份和那条套着的引文紧挨着，Python-Markdown 把两条并成一条引文。
_MD_MERGED = (
    "Opening words of the article body here.\n\n"
    "> The most surprising announcement of all were the prices.\n"
    "> What this means is that in real terms the products actually got cheaper.\n\n"
    "> The most surprising announcement of all were the prices.\n\n"
    "What this means is that in real terms the products actually got *cheaper*.\n\n"
    "Closing words of the article body.\n"
)
_ZH_MERGED = {
    0: "<p>正文开头的几句话。</p>",
    1: "<p>最让人意外的是价格。<br> 这意味着按实际购买力算，这些产品其实更便宜了。</p>",
    2: "<p>最让人意外的是价格。</p>",
    3: "<p>这意味着按实际购买力算，这些产品其实更<em>便宜</em>了。</p>",
    4: "<p>正文最后几句。</p>",
}


def test_a_quote_merged_with_its_nested_copy_shows_once_on_every_page():
    source, zh, view = _render(_MD_MERGED, _ZH_MERGED)
    assert '<blockquote id="p-2">' in source and source.count("<blockquote") == 1
    en = "".join(pair.en for pair in view.pairs)
    shown = "".join(pair.zh or "" for pair in view.pairs)
    assert en.count("most surprising") == 1 and en.count("What this means") == 1
    assert shown.count("最让人意外") == 1 and shown.count("这意味着") == 1
    folded = fold_translated_copies(source, zh)
    assert folded.count("最让人意外") == 1 and folded.count("这意味着") == 1
    # 拆回两条：套着的那条放回外层里，带格式那段也在外层里。
    quote = folded.split('<blockquote id="p-2">', 1)[1].rsplit("</blockquote>", 1)[0]
    assert re.search(r"<blockquote><p>最让人意外的是价格。</p>\s*</blockquote>", quote) and "<em>便宜</em>" in quote
    assert _ids(folded) == _ids(zh)
