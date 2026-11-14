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


# 同上，纯文字那份还分了好几段，后面跟着图（线上 21401）。拆在第一段后面时，后面几段连同套着的那条
# 像一条「结尾带署名」的外层，也能凑出一处；拆在纯文字那份的最后一段后面，带格式那几块才正好接上。
_MD_MERGED_PARAS = (
    "Opening words of the article body here.\n\n"
    "> The most surprising announcement of all were the prices, which stayed the same.\n>\n"
    "> What this means is that in real terms the products actually got cheaper this year.\n>\n"
    "> This does not fit the way the company has always been thought to set its prices.\n\n"
    "> The most surprising announcement of all were the prices, which stayed the "
    "[same](https://example.invalid/prices).\n\n"
    "[](https://example.invalid/chart)\n\n"
    f"![Prices over five years]({_CHART})\n\n"
    "What this means is that in real terms the products actually got *cheaper* this year.\n\n"
    "This does not fit the way the company has always been thought to set its "
    "[prices](https://example.invalid/pricing).\n\n"
    "Closing words of the article body.\n"
)
_ZH_MERGED_PARAS = {
    0: "<p>正文开头的几句话。</p>",
    1: "<p>最让人意外的是价格，价格一点没变。</p>",
    2: "<p>这意味着按实际购买力算，这些产品今年其实更便宜了。</p>",
    3: "<p>这和人们一直以为的这家公司的定价方式对不上。</p>",
    4: '<p>最让人意外的是价格，价格一点<a href="https://example.invalid/prices">没变</a>。</p>',
    5: "<p>这意味着按实际购买力算，这些产品今年其实更<em>便宜</em>了。</p>",
    6: '<p>这和人们一直以为的这家公司的<a href="https://example.invalid/pricing">定价方式</a>对不上。</p>',
    7: "<p>正文最后几句。</p>",
}


def test_a_quote_in_paragraphs_merged_with_its_nested_copy_shows_once_on_every_page():
    source, zh, view = _render(_MD_MERGED_PARAS, _ZH_MERGED_PARAS, _IMAGES)
    texts = ("最让人意外", "这意味着", "这和人们一直以为")
    shown = "".join(pair.zh or "" for pair in view.pairs)
    assert view.correspondence == "mapped" and all(shown.count(text) == 1 for text in texts)
    folded = fold_translated_copies(source, zh)
    assert all(folded.count(text) == 1 for text in texts)
    # 留下带格式的那几块，套着的那条、图、加粗和链接都放回外层的引文里。
    quote = folded.split('<blockquote id="p-2">', 1)[1].split("正文最后几句", 1)[0]
    assert re.search(r'<blockquote><p>最让人意外.*没变</a>。</p>\s*</blockquote>', quote)
    assert _CHART in quote and "<em>便宜</em>" in quote and "定价方式</a>" in quote
    assert _ids(folded) == _ids(zh)


# 写两遍的引文：纯文字那份按行切开译，每行都译对了；带格式那几段的译文挪了一格（漏了第一个列表项，
# 后面的各往前挪一格，最后一个列表项是下一段的译文）。线上 21938 是这样。
_MD_SLIPPED = (
    "Opening words of the article body here.\n\n"
    "> First ask what matters most: the team, the product, or the market?\n"
    "> When a great team meets a lousy market, market wins.\n"
    "> When a lousy team meets a great market, market wins.\n"
    "> When a great team meets a great market, something special happens.\n"
    "> You can still ruin a great market, but a great market usually means success.\n\n"
    "First ask what matters most: the team, the product, or the *market*?\n\n"
    "- When a great team meets a lousy market, market wins.\n\n"
    "- When a lousy team meets a great market, market wins.\n\n"
    "- When a great team meets a great market, something special happens.\n\n"
    "You can still ruin a great market, but a great market usually means [success](https://example.invalid/s).\n\n"
    "Closing words after the quote end here.\n"
)
_ZH_RIGHT = {
    0: "<p>正文开头的几句话。</p>",
    1: "<p>先问什么最重要：团队、产品，还是市场？</p>",
    2: "<p>优秀的团队遇上糟糕的市场，市场赢。</p>",
    3: "<p>平庸的团队遇上巨大的市场，市场赢。</p>",
    4: "<p>优秀的团队遇上巨大的市场，就会有特别的事情发生。</p>",
    5: "<p>巨大的市场你照样可能搞砸，但巨大的市场通常意味着成功。</p>",
    6: "<p>先问什么最重要：团队、产品，还是<em>市场</em>？</p>",
    7: "<p>优秀的团队遇上糟糕的市场，市场赢。</p>",
    8: "<p>平庸的团队遇上巨大的市场，市场赢。</p>",
    9: "<p>优秀的团队遇上巨大的市场，就会有特别的事情发生。</p>",
    10: '<p>巨大的市场你照样可能搞砸，但巨大的市场通常意味着<a href="https://example.invalid/s">成功</a>。</p>',
    11: "<p>引文后面收尾的几句话。</p>",
}
_ZH_SLIPPED = {**_ZH_RIGHT, 7: _ZH_RIGHT[8], 8: _ZH_RIGHT[9], 9: _ZH_RIGHT[5]}


def test_copies_translated_one_slot_off_keep_the_plain_copy():
    """带格式那几段的中文挪了一格：英文不同的两段中文一字不差。纯文字那份的中文才是对的，收掉它
    「优秀的团队遇上糟糕的市场」这句就没了，这一处不收。"""
    source, zh, view = _render(_MD_SLIPPED, _ZH_SLIPPED)
    assert view.correspondence == "mapped" and not view.notice
    assert "优秀的团队遇上糟糕的市场" in "".join(pair.zh or "" for pair in view.pairs)
    assert "优秀的团队遇上糟糕的市场" in fold_translated_copies(source, zh)
    # 译对了的同一篇照常收成一份。
    source, zh, view = _render(_MD_SLIPPED, _ZH_RIGHT)
    assert "".join(pair.zh or "" for pair in view.pairs).count("优秀的团队遇上糟糕的市场") == 1
    folded = fold_translated_copies(source, zh)
    assert folded.count("优秀的团队遇上糟糕的市场") == 1 and folded.count("先问什么最重要") == 1


# 列表项先是一句导语、下面套着引文；推文的署名直接写在引文里。导出带格式的那份只写了引文、推文正文，
# 导语、署名只在纯文字那份里（线上 21735、21273 这样）。
_MD_LEAD = (
    "Opening words of the article body here.\n\n"
    "- Video is not a separate unit; it is a setting you bid on. The chief explains: "
    "Video does displace other posts in the feed. Video is just a format that is bid into the auction.\n\n"
    "> Video does displace other posts in the [feed](https://example.invalid/feed). "
    "Video is just a format that is bid into the auction.\n\n"
    "- The second point is short and has no copy at all.\n\n"
    "> This has been the story of every company that has gone there. Why does nobody learn it?\n"
    "> https://t.co/abc123\n"
    "> — 王小明 (@wangxm)\n"
    "> May 21, 2025\n\n"
    "This has been the story of every company that has gone there. Why does nobody learn it? "
    "[https://t.co/abc123](https://t.co/abc123)\n\n"
    "Closing words of the article body.\n"
)
_ZH_LEAD = {
    0: "<p>正文开头的几句话。</p>",
    1: "<li>视频不是一种单独的广告单元，只是出价时的一个设置。负责人解释说：视频确实会挤掉信息流里的其他帖子。"
       "视频只是一种参与竞价的格式。</li>",
    2: '<p>视频确实会挤掉<a href="https://example.invalid/feed">信息流</a>里的其他帖子。视频只是参与竞价的一种格式。</p>',
    3: "<li>第二点很短，没有副本。</li>",
    4: "<p>去那里的每一家公司都是这个故事。怎么就没人吸取教训？ https://t.co/abc123 — 王小明 (@wangxm) "
       "2025年5月21日</p>",
    5: '<p>去那里的每一家公司都是这个故事。怎么就没人吸取教训？ <a href="https://t.co/abc123">https://t.co/abc123</a></p>',
    6: "<p>正文最后几句。</p>",
}


def test_lead_in_and_tweet_signature_stay_on_the_bilingual_page():
    source, _zh, view = _render(_MD_LEAD, _ZH_LEAD)
    assert view.correspondence == "mapped" and not view.notice
    en = "".join(pair.en for pair in view.pairs)
    zh = "".join(pair.zh or "" for pair in view.pairs)
    for text in ("Video does displace", "The chief explains", "story of every company", "王小明 (@wangxm)"):
        assert en.count(text) == 1, text
    for text in ("视频确实会挤掉", "负责人解释说", "每一家公司", "2025年5月21日"):
        assert zh.count(text) == 1, text
    item, _second, tweet = view.pairs[1:4]
    # 导语留在列表项里，带格式的引文套回这一项下面；推文的署名留在引文末尾。
    assert "<ul><li><p>Video is not a separate unit; it is a setting you bid on. The chief explains:</p><blockquote>" \
        in item.en
    lead = "<p>视频不是一种单独的广告单元，只是出价时的一个设置。负责人解释说：</p>"
    assert item.zh.startswith(f"<ul><li>{lead}<blockquote>")
    assert '<a href="https://example.invalid/feed">信息流</a>' in item.zh
    assert tweet.en.endswith("<p>— 王小明 (@wangxm) May 21, 2025</p></blockquote>")
    assert tweet.zh.endswith("<p>— 王小明 (@wangxm) 2025年5月21日</p></blockquote>")
    assert content_gaps(source, en) == 0
    assert _ids(source) <= _ids(en + zh) | {pair.anchor for pair in view.pairs}


def test_lead_in_and_tweet_signature_stay_on_the_chinese_page():
    source, zh, _view = _render(_MD_LEAD, _ZH_LEAD)
    folded = fold_translated_copies(source, zh)
    for text in ("视频确实会挤掉", "负责人解释说", "每一家公司", "2025年5月21日"):
        assert folded.count(text) == 1, text
    assert '<li><p>视频不是一种单独的广告单元，只是出价时的一个设置。负责人解释说：</p><blockquote id="p-3">' in folded
    signature = "<p>— 王小明 (@wangxm) 2025年5月21日</p>"
    assert re.search(r'<blockquote id="p-5">\s*<p id="p-6">去那里的每一家公司.*?</p>' + re.escape(signature), folded)
    assert _ids(folded) == _ids(zh)


def test_lead_in_translated_into_the_first_quoted_sentence_is_kept():
    """译文把导语末尾（「他解释说」）和引文第一句译成了一句：这一句整句留在导语里，多显示一遍，不收掉导语的字。"""
    zh = dict(_ZH_LEAD)
    zh[1] = "<li>视频不是一种单独的广告单元，只是出价时的一个设置。他解释说视频确实会挤掉信息流里的其他帖子。" \
        "视频只是一种参与竞价的格式。</li>"
    source, joined, view = _render(_MD_LEAD, zh)
    folded = fold_translated_copies(source, joined)
    kept = "视频不是一种单独的广告单元，只是出价时的一个设置。他解释说视频确实会挤掉信息流里的其他帖子。"
    assert f"<li><p>{kept}</p>" in folded
    assert folded.count("视频只是") == 1
    assert "他解释说" in "".join(pair.zh or "" for pair in view.pairs)


# 原文的列表项下面挂着嵌套列表：纯文字那份所在的项、带格式的一段，带格式的嵌套项顶格写，
# 和后面正文自己的项被 Python-Markdown 并在一个列表里（线上 21068）。
_MD_TIERS = (
    "Opening words of the article body here.\n\n"
    "- The deal has three parts worth knowing about.\n\n"
    "- There will be three tiers of games on the service: All games go to subscribers of the new package. "
    "A smaller number of games go to the cheaper plan. An even smaller number of games will be free.\n\n"
    "There will be three tiers of games on the service:\n\n"
    "- All games go to subscribers of the new package.\n\n"
    "- A smaller number of games go to the cheaper plan.\n\n"
    "- An even smaller number of games will be free.\n\n"
    "- The league keeps the right to sell local rights to others.\n\n"
    "Closing words of the article body.\n"
)
_ZH_TIERS = {
    0: "<p>正文开头的几句话。</p>",
    1: "<p>这笔交易有三点值得知道。</p>",
    2: "<p>服务上的比赛分三档：所有比赛都给新套餐的订阅用户。少一些的比赛给便宜的那档。更少的比赛免费。</p>",
    3: "<p>服务上的比赛分三档：</p>",
    4: "<p>所有比赛都给新套餐的订阅用户。</p>",
    5: "<p>少一些的比赛给便宜的那档。</p>",
    6: "<p>更少的比赛免费。</p>",
    7: "<p>联赛保留把地方转播权卖给别人的权利。</p>",
    8: "<p>正文最后几句。</p>",
}


def test_nested_items_merged_into_the_next_list_show_once_on_every_page():
    source, zh, view = _render(_MD_TIERS, _ZH_TIERS)
    assert view.correspondence == "mapped" and not view.notice
    shown = "".join(pair.zh or "" for pair in view.pairs)
    folded = fold_translated_copies(source, zh)
    for text in ("所有比赛", "少一些的比赛", "更少的比赛", "联赛保留"):
        assert shown.count(text) == 1 and folded.count(text) == 1, text
    # 嵌套的三项放回那一项下面，正文自己的那一项还在外面。
    item = folded.split("<p id=\"p-3\">服务上的比赛分三档：</p>", 1)[1].split("</li>\n</ul>", 1)[0]
    assert '<ul id="p-4">' in item and "更少的比赛免费" in item and "联赛保留" not in item
    assert "联赛保留" in folded.split("</li>\n</ul>", 1)[1]
    assert _ids(folded) == _ids(zh)


# 引文纯文字那份里有一行以「: 」开头，Markdown 把它认成了定义列表（线上 22726）。
_MD_DL = (
    "Opening words of the article body here.\n\n"
    "> It is important to define what a subscription means before anything else.\n"
    "> Each of those words matters:\n"
    "> Paying\n"
    "> : A subscription is an ongoing commitment to the work. Regular delivery: readers get it directly.\n\n"
    "It is important to define what a subscription means before anything else.\n\n"
    "Each of those words matters:\n\n"
    "- Paying: A subscription is an ongoing commitment to the [work](https://example.invalid/w).\n\n"
    "- Regular delivery: readers get it directly.\n\n"
    "Closing words of the article body.\n"
)
_ZH_DL = {
    0: "<p>正文开头的几句话。</p>",
    1: "<dt>在说别的之前，先要界定订阅是什么意思。</dt>",
    2: "<dt>每个词都有分量：</dt>",
    3: "<dt>付费</dt>",
    4: "<dd>订阅是对这份工作的持续承诺。定期送达：读者直接收到。</dd>",
    5: "<p>在说别的之前，先要界定订阅是什么意思。</p>",
    6: "<p>每个词都有分量：</p>",
    7: '<p>付费：订阅是对这份<a href="https://example.invalid/w">工作</a>的持续承诺。</p>',
    8: "<p>定期送达：读者直接收到。</p>",
    9: "<p>正文最后几句。</p>",
}


def test_plain_quote_read_as_a_definition_list_still_folds():
    source, zh, view = _render(_MD_DL, _ZH_DL)
    assert '<blockquote id="p-2">\n<dl>' in source
    assert view.correspondence == "mapped" and not view.notice
    en = "".join(pair.en for pair in view.pairs)
    shown = "".join(pair.zh or "" for pair in view.pairs)
    folded = fold_translated_copies(source, zh)
    for text in ("先要界定订阅", "每个词都有分量", "持续承诺", "读者直接收到"):
        assert shown.count(text) == 1 and folded.count(text) == 1, text
    assert "<dl>" not in folded and "<dl>" not in en
    assert re.search(r'<blockquote id="p-2"><p id="p-3">在说别的之前.*<ul id="p-5">.*</ul></blockquote>', folded, re.S)
    # 少的只有认错的定义列表外壳，字和链接都在。
    assert content_gaps(re.sub(r"</?(?:dl|dt|dd)>", "", source), en) == 0
    assert _ids(folded) == _ids(zh)
