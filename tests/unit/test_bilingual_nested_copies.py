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


def _render(md: str = _MD, zh: dict[int, str] = _ZH):
    """和文章页一样：两边都编好段落号，对照页拿没并句的译文，只看中文页拿并好句的。"""
    source = stamp_paragraph_ids(str(render_body(md, {})))
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
    # 带格式的几段里夹着引文：原文页也不收，这里一样。
    nested = _pairs(
        ("<ul><li>He said this. The quoted words follow.</li></ul>", "<ul><li>他这么说。下面是引的话。</li></ul>"),
        ("<p>He said this.</p>", "<p>他这么说。</p>"),
        ("<blockquote><p>The quoted words follow.</p></blockquote>", "<blockquote><p>下面是引的话。</p></blockquote>"),
    )
    assert _fold_copy_pairs(nested) == nested
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
