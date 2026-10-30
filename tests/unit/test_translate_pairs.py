import re

from researchlib.translate.language import is_chinese
from researchlib.translate.pair import bilingual_pairs, stamp_paragraph_ids
from researchlib.translate.segments import (
    chunk_segments,
    coarse_segment_reason,
    iter_segments,
    leaf_count_problem,
    segments_of,
    shield,
    splice_segments,
    stable_leaf,
    top_level_blocks,
    unshield,
)
from researchlib.translate.service import _assemble_zh_html, _dedupe_consecutive_blocks


def test_is_chinese_detects_han_majority():
    assert is_chinese("这是一篇中文商业分析，讲述公司战略。")
    assert not is_chinese("This is an English essay about aggregation theory and platforms.")


def test_top_level_and_pairs_match_aihot_order():
    en = "<p>Hello world.</p><p>Second paragraph.</p>"
    zh = "<p>你好世界。</p><p>第二段。</p>"
    en_s = stamp_paragraph_ids(en)
    zh_s = stamp_paragraph_ids(zh)
    pairs = bilingual_pairs(en_s, zh_s)
    assert pairs is not None
    assert len(pairs) == 2
    assert "Hello world" in pairs[0].en
    assert pairs[0].zh and "你好" in pairs[0].zh
    assert pairs[0].anchor == "p-1"


def test_pairs_drop_duplicate_media_on_zh():
    en = '<p>See <img src="/a.png" alt="x"> chart.</p>'
    zh = '<p>见图 <img src="/a.png" alt="x">。</p>'
    pairs = bilingual_pairs(en, zh)
    assert pairs is not None
    assert "<img" in pairs[0].en
    assert pairs[0].zh is not None
    assert "<img" not in pairs[0].zh


def test_shield_roundtrip():
    raw = '<p>Read <a href="https://example.com">this</a> and <img src="/x.png" alt="">.</p>'
    s = shield(raw)
    assert "⟦0⟧" in s.html
    assert 'id="L0"' in s.html
    back = unshield('<p>读 <a id="L0">这个</a> 与 ⟦0⟧。</p>', s)
    assert back is not None
    assert 'href="https://example.com"' in back
    assert "<img" in back


def test_chunk_segments_respects_limit():
    parts = ["a" * 100, "b" * 100, "c" * 100]
    chunks = chunk_segments(parts, limit=150)
    assert len(chunks) == 3


def test_top_level_blocks_basic():
    html = "<p>One</p><h2>Title</h2><p>Two</p>"
    assert top_level_blocks(html) == ["<p>One</p>", "<h2>Title</h2>", "<p>Two</p>"]


def test_blocks_keep_attribute_quotes_escaped():
    """属性里转义过的引号写回去还要是转义的，不然编段落号时会多出一个可执行的属性。"""
    html = '<p title="&quot; onmouseover=alert(1) x=&quot;">hi</p>'
    assert top_level_blocks(html) == [html]
    assert stamp_paragraph_ids(html) == '<p id="p-1" title="&quot; onmouseover=alert(1) x=&quot;">hi</p>'
    assert top_level_blocks('<p><a href="/s?a=1&amp;b=2">x</a></p>') == ['<p><a href="/s?a=1&amp;b=2">x</a></p>']


def test_dedupe_consecutive_identical_paragraphs():
    html = "<p>第一段内容比较长，用来触发去重。</p><p>第一段内容比较长，用来触发去重。</p><p>第二段不同。</p>"
    out = _dedupe_consecutive_blocks(html)
    assert out.count("第一段内容比较长") == 1
    assert "第二段不同" in out


def test_dedupe_keeps_nonconsecutive_repeats():
    html = "<p>A段落文字足够长了。</p><p>B段落文字足够长了。</p><p>A段落文字足够长了。</p>"
    out = _dedupe_consecutive_blocks(html)
    assert out.count("A段落文字足够长了") == 2


def test_dedupe_consecutive_paragraphs_inside_one_block():
    html = (
        "<div>"
        "<p>第一段内容比较长，用来触发去重。</p>"
        "<p>中间另一段，足够长的文字。</p>"
        "<p>第一段内容比较长，用来触发去重。</p>"
        "<p>第一段内容比较长，用来触发去重。</p>"
        "</div>"
    )
    out = _dedupe_consecutive_blocks(html)
    assert out.count("第一段内容比较长") == 2  # 非连续保留，连续去掉一段
    assert "中间另一段" in out


def test_dedupe_identical_paragraph_after_blockquote():
    html = (
        "<blockquote><p>Siri 可以在你已经在使用的应用中采取行动，包括像 Stuff 这样的第三方应用。</p></blockquote>"
        "<p>Siri 可以在你已经在使用的应用中采取行动，包括像 Stuff 这样的第三方应用。</p>"
        "<p>下一段不同内容，足够长以便保留。</p>"
    )
    out = _dedupe_consecutive_blocks(html)
    assert out.count("Siri 可以在你已经在使用的应用中采取行动") == 1
    assert "下一段不同内容" in out


def test_assemble_zh_keeps_quote_and_the_following_paragraph():
    """写入不跨段删：引用和紧跟的相同正文都留着，顶层块数和原文槽位一致。"""
    quote = "Siri 可以在你已经在使用的应用中采取行动，包括像 Stuff 这样的第三方应用。"
    slots = [
        f"<blockquote><p>{quote}</p></blockquote>",
        f"<p>{quote}</p>",
        "<p>下一段不同内容，足够长以便保留。</p>",
    ]
    html = _assemble_zh_html(slots)
    assert len(top_level_blocks(html)) == 3
    assert html.count(quote) == 2
    assert "下一段不同内容" in html


def test_assemble_zh_collapses_stutter_inside_one_slot():
    """一个译段里连吐两遍仍收成一份，旁边的段不动。"""
    sentence = "模型把同一段连吐两遍，这一段足够长。"
    slots = [
        "<p>开头这一段要留下来。</p>",
        f"<p>{sentence}</p><p>{sentence}</p>",
        "<p>结尾不同，要留下来。</p>",
    ]
    html = _assemble_zh_html(slots)
    assert html.count(sentence) == 1
    assert len(top_level_blocks(html)) == 3
    assert "开头这一段" in html and "结尾不同" in html


def test_dedupe_ignores_stamped_paragraph_ids():
    """展示层盖上 id="p-N" 之后，引用段和紧跟的重复段仍视为同一段。"""
    text = "第一段内容比较长，用来触发去重。"
    html = (
        f'<blockquote id="p-1"><p>{text}</p></blockquote>'
        f'<p id="p-2">{text}</p>'
        '<p id="p-3">第二段不同，要留下来。</p>'
    )
    out = _dedupe_consecutive_blocks(html)
    assert out.count(text) == 1
    assert "第二段不同" in out


def _quote_repeat_fixture() -> tuple[str, str]:
    """21182 的形状：引用块后面又跟了一段相同正文，重复多次，差块数大于 1。

    译文侧这些重复段已按入库去重去掉，并盖上对照页的 p-id。原文侧仍在，也盖了 id。
    """
    en_parts: list[str] = []
    zh_parts: list[str] = []
    for index in range(8):
        quote = f"Quoted passage {index} is long enough that consecutive copies count as one block."
        note = f"Commentary {index} follows the quote and must stay with its own translation."
        en_parts.append(f'<blockquote id="p-{index * 3 + 1}"><p>{quote}</p></blockquote>')
        en_parts.append(f'<p id="p-{index * 3 + 2}">{quote}</p>')
        en_parts.append(f'<p id="p-{index * 3 + 3}">{note}</p>')
        zh_parts.append(f'<blockquote id="p-{index * 2 + 1}"><p>引用{index}足够长，连续重复只留一份。</p></blockquote>')
        zh_parts.append(f'<p id="p-{index * 2 + 2}">解说{index}要留在自己的译文上。</p>')
    return "".join(en_parts), "".join(zh_parts)


def test_pairs_realign_when_translation_dropped_repeated_quotes():
    """21182 的形状：英文 24 块、中文 16 块，差大于 1。对照仍在，引用对着引用，解说对着解说。"""
    en, zh = _quote_repeat_fixture()
    assert len(top_level_blocks(en)) == 24
    assert len(top_level_blocks(zh)) == 16
    assert abs(len(top_level_blocks(en)) - len(top_level_blocks(zh))) > 1
    pairs = bilingual_pairs(en, zh)
    assert pairs is not None
    assert len(pairs) == 16
    for index, pair in enumerate(pairs):
        assert pair.anchor == f"p-{index + 1}"
        assert pair.zh
        if index % 2 == 0:
            assert f"引用{index // 2}" in pair.zh
            assert f"Quoted passage {index // 2}" in pair.en
        else:
            assert f"解说{index // 2}" in pair.zh
            assert f"Commentary {index // 2}" in pair.en
            assert f"Quoted passage {index // 2}" not in pair.en


def test_one_dropped_repeat_realigns_following_paragraph():
    """只少一段重复引用时，解说仍对着自己的译文，不退回纯中文。"""
    quote = "Microsoft cloud revenue grew at the low end of the forecast this quarter."
    note = "That pivot is the part that matters for the rest of the year."
    en = (
        f'<blockquote id="p-1"><p>{quote}</p></blockquote>'
        f'<p id="p-2">{quote}</p>'
        f'<p id="p-3">{note}</p>'
    )
    zh = (
        '<blockquote id="p-1"><p>微软云收入落在预测低端。</p></blockquote>'
        '<p id="p-2">真正要紧的是后面这次转向。</p>'
    )
    pairs = bilingual_pairs(en, zh)
    assert pairs is not None
    assert len(pairs) == 2
    assert pairs[0].zh and "微软云" in pairs[0].zh
    assert "Microsoft cloud" in pairs[0].en
    assert pairs[0].anchor == "p-1"
    assert pairs[1].zh and "真正要紧" in pairs[1].zh
    assert "pivot" in pairs[1].en
    assert pairs[1].anchor == "p-2"


def test_leftover_english_hangs_after_aligned_prefix():
    """去重把重复引用对齐之后，后面多出来的英文挂在末尾，没有译文。"""
    quote = "Quoted passage is long enough that consecutive copies count as one block."
    note = "Commentary follows the quote and must stay with its own translation."
    extra = "A later paragraph that the translation never reached."
    en = (
        f'<blockquote id="p-1"><p>{quote}</p></blockquote>'
        f'<p id="p-2">{quote}</p>'
        f'<p id="p-3">{note}</p>'
        f'<p id="p-4">{extra}</p>'
    )
    zh = (
        '<blockquote id="p-1"><p>引用足够长，连续重复只留一份。</p></blockquote>'
        '<p id="p-2">解说要留在自己的译文上。</p>'
    )
    assert abs(len(top_level_blocks(en)) - len(top_level_blocks(zh))) > 1
    pairs = bilingual_pairs(en, zh)
    assert pairs is not None
    assert len(pairs) == 3
    assert pairs[0].zh and "引用足够长" in pairs[0].zh
    assert "Quoted passage" in pairs[0].en
    assert pairs[1].zh and "解说要留" in pairs[1].zh
    assert "Commentary follows" in pairs[1].en
    assert "later paragraph" in pairs[2].en
    assert pairs[2].zh is None


def test_soft_dedupe_collapses_equal_length_quote_then_body():
    """叶子数已经一样时，引用后紧跟的同一段正文两边都收成一对。

    21118 在叶子数对齐之后仍有这种连续相同的 <p>。段落号不同也要收掉。
    后面单独的一段还在自己的位置上。
    """
    quote = "Microsoft cloud revenue grew at the low end of the forecast this quarter."
    note = "That pivot is the part that matters for the rest of the year."
    zh_quote = "微软云收入落在这个季度预测区间的低端，连续相同只留一份。"
    zh_note = "真正要紧的是后面这次转向，要留在自己的译文上。"
    en = f'<blockquote id="p-1"><p>{quote}</p></blockquote><p id="p-2">{quote}</p>'
    zh = f'<blockquote id="p-1"><p>{zh_quote}</p></blockquote><p id="p-2">{zh_quote}</p>'
    assert len(segments_of(en)) == len(segments_of(zh)) == 2
    pairs = bilingual_pairs(en, zh)
    assert pairs is not None
    assert len(pairs) == 1
    assert quote in pairs[0].en
    assert pairs[0].zh and zh_quote in pairs[0].zh
    assert pairs[0].anchor == "p-1"

    en_note = en + f'<p id="p-3">{note}</p>'
    zh_note_html = zh + f'<p id="p-3">{zh_note}</p>'
    assert len(segments_of(en_note)) == len(segments_of(zh_note_html)) == 3
    followed = bilingual_pairs(en_note, zh_note_html)
    assert followed is not None and len(followed) == 2
    assert quote in followed[0].en and note not in followed[0].en
    assert followed[0].zh and zh_quote in followed[0].zh
    assert note in followed[1].en and quote not in followed[1].en
    assert followed[1].zh and "真正要紧" in followed[1].zh
    assert followed[1].anchor == "p-3"


def test_soft_dedupe_still_hides_zh_when_text_matches_english():
    """收成一对之后，译文纯文本仍和原文相同，中文照旧不显示。"""
    quote = "Microsoft cloud revenue grew at the low end of the forecast this quarter."
    html = f'<blockquote id="p-1"><p>{quote}</p></blockquote><p id="p-2">{quote}</p>'
    pairs = bilingual_pairs(html, html)
    assert pairs is not None and len(pairs) == 1
    assert pairs[0].zh is None
    assert quote in pairs[0].en


def test_soft_dedupe_keeps_equal_length_when_shrink_would_diverge():
    """两边都有连续重复，但去掉的段数不同。长度会拉开，仍按原来的等长序列配。"""
    alpha = "Quoted passage alpha is long enough that a consecutive copy counts as one."
    beta = "Quoted passage beta is long enough that a consecutive copy counts as one."
    later = "A later note that appears only once on the English side of the page."
    zh_alpha = "甲段译文足够长，连续相同要能被去掉，这里先留着。"
    zh_beta = "乙段译文足够长，连续相同要能被去掉，这里也留着。"
    en = (
        f"<blockquote><p>{alpha}</p></blockquote><p>{alpha}</p>"
        f"<p>{beta}</p><p>{later}</p>"
    )
    zh = (
        f"<blockquote><p>{zh_alpha}</p></blockquote><p>{zh_alpha}</p>"
        f"<blockquote><p>{zh_beta}</p></blockquote><p>{zh_beta}</p>"
    )
    assert len(segments_of(en)) == len(segments_of(zh)) == 4
    pairs = bilingual_pairs(en, zh)
    assert pairs is not None and len(pairs) == 4
    assert alpha in pairs[0].en and pairs[0].zh and zh_alpha in pairs[0].zh
    assert alpha in pairs[1].en and pairs[1].zh and zh_alpha in pairs[1].zh
    assert beta in pairs[2].en and pairs[2].zh and zh_beta in pairs[2].zh
    assert later in pairs[3].en and pairs[3].zh and zh_beta in pairs[3].zh


def test_equal_length_list_item_and_paragraph_stay_two_pairs():
    """同文的 li 和 p 标签不同。叶子数已经一样时也不收成一段。"""
    text = "iPhone revenue was flat and the installed base stayed loyal through the year."
    zh = "iPhone 收入持平，已有用户这一年仍然留着，列表项和正文各留一份。"
    en = f"<ul><li>{text}</li></ul><p>{text}</p>"
    zh_html = f"<ul><li>{zh}</li></ul><p>{zh}</p>"
    leaves = segments_of(en)
    assert len(leaves) == len(segments_of(zh_html)) == 2
    assert leaves[0].startswith("<li") and leaves[1].startswith("<p")
    pairs = bilingual_pairs(en, zh_html)
    assert pairs is not None and len(pairs) == 2
    assert all(text in pair.en for pair in pairs)
    assert all(pair.zh and zh in pair.zh for pair in pairs)


def test_pairs_keep_repeats_when_translation_kept_them():
    """两边块数已经一样时，不因为英文侧有重复段就把译文错开。"""
    quote = "Microsoft cloud revenue grew at the low end of the forecast this quarter."
    en = (
        f"<blockquote><p>{quote}</p></blockquote>"
        f"<p>{quote}</p>"
        "<p>A separate observation about the quarter and what follows.</p>"
    )
    zh = (
        "<blockquote><p>微软云收入落在预测低端。</p></blockquote>"
        "<p>另一条并不相同的译文，所以入库时没有被去重。</p>"
        "<p>关于这个季度的另一点观察，要留在第三段。</p>"
    )
    pairs = bilingual_pairs(en, zh)
    assert pairs is not None
    assert len(pairs) == 3
    assert pairs[1].zh and "另一条并不相同" in pairs[1].zh
    assert pairs[2].zh and "第三段" in pairs[2].zh


def test_pairs_when_model_repeated_a_paragraph():
    """中文连吐三遍时先收成一份再配，成本对着自己的英文，不整页退回。"""
    sentence = "聚合理论解释平台为什么会赢，这一段足够长所以连续重复要去掉。"
    en = "<p>Aggregation theory explains why platforms win.</p><p>Costs come second in this note.</p>"
    zh = f"<p>{sentence}</p><p>{sentence}</p><p>{sentence}</p><p>成本是后面的第二点。</p>"
    assert abs(len(top_level_blocks(en)) - len(top_level_blocks(zh))) > 1
    pairs = bilingual_pairs(en, zh)
    assert pairs is not None
    assert len(pairs) == 2
    assert pairs[0].zh and "聚合理论" in pairs[0].zh
    assert "Aggregation theory" in pairs[0].en
    assert pairs[1].zh and "成本" in pairs[1].zh
    assert "Costs come second" in pairs[1].en


def test_pairs_when_structure_diverges():
    """没有可去重的重复段时仍出对照：前面对齐，多出来的原文挂在末尾且没有译文。"""
    en = "".join(f"<p>Paragraph {index} says something distinct.</p>" for index in range(6))
    zh = "<p>只有两段。</p><p>结构对不上，照样对照。</p>"
    pairs = bilingual_pairs(en, zh)
    assert pairs is not None
    assert len(pairs) == 6
    assert pairs[0].zh and "只有两段" in pairs[0].zh
    assert "Paragraph 0" in pairs[0].en
    assert pairs[1].zh and "照样对照" in pairs[1].zh
    assert "Paragraph 1" in pairs[1].en
    assert all(pair.zh is None for pair in pairs[2:])
    assert "Paragraph 5" in pairs[5].en


def test_leftover_chinese_hangs_when_nothing_to_dedupe():
    """没有重复段可对齐时，多出来的译文接在末尾，en 为空，对照不退回。"""
    en = "<p>Only one English paragraph here.</p>"
    zh = "<p>只有一段对得上。</p><p>后面多出来的译文。</p><p>再多一段，内容不同。</p>"
    pairs = bilingual_pairs(en, zh)
    assert pairs is not None
    assert len(pairs) == 3
    assert "Only one" in pairs[0].en
    assert pairs[0].zh and "对得上" in pairs[0].zh
    assert pairs[1].en == "" and pairs[1].zh and "多出来" in pairs[1].zh
    assert pairs[2].en == "" and pairs[2].zh and "再多一段" in pairs[2].zh


def test_pairs_skip_zh_when_same_as_english_or_empty():
    en = "<p>Same text.</p><p>Has translation.</p><p>Third.</p>"
    zh = "<p>Same text.</p><p>有译文。</p><p></p>"
    pairs = bilingual_pairs(en, zh)
    assert pairs is not None
    assert len(pairs) == 3
    assert pairs[0].zh is None and "Same text." in pairs[0].en
    assert pairs[1].zh and "有译文" in pairs[1].zh
    assert pairs[2].zh is None and "Third." in pairs[2].en


def test_pairs_none_only_when_both_sides_have_no_blocks():
    assert bilingual_pairs("", "") is None
    assert bilingual_pairs("   ", "") is None
    only_en = bilingual_pairs("<p>Hello.</p>", "")
    assert only_en is not None and len(only_en) == 1 and only_en[0].zh is None and "Hello." in only_en[0].en
    only_zh = bilingual_pairs("", "<p>你好。</p>")
    assert only_zh is not None and len(only_zh) == 1 and only_zh[0].en == ""
    assert only_zh[0].zh and "你好" in only_zh[0].zh


def _plain_len(html: str) -> int:
    return len(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", html or "")).strip())


def test_br_does_not_swallow_the_following_paragraphs():
    """21118 的测量：引用里一个 <br> 曾把后面所有兄弟节点吞进同一个顶层块。"""
    html = (
        "<p>Before the quote.</p>"
        "<blockquote><p>First line.<br>Second line of the same quote.</p></blockquote>"
        "<p>After the quote, still its own paragraph.</p>"
        "<p>Another paragraph that must not join the quote.</p>"
    )
    blocks = top_level_blocks(html)
    assert len(blocks) == 4
    assert blocks[0].startswith("<p>Before")
    assert blocks[-1].startswith("<p>Another")
    assert "After the quote" not in blocks[1]
    segments = segments_of(html)
    assert len(segments) == 4
    assert any("First line." in segment and "Second line" in segment for segment in segments)
    assert all("After the quote" not in segment or segment.startswith("<p>After") for segment in segments)


def test_short_blockquote_without_block_children_stays_one_leaf():
    """没有块级子节点的短引用仍是一整段 blockquote，不拆开。"""
    html = "<p>Intro sentence here.</p><blockquote>A short real quotation.</blockquote><p>Follow-up sentence here.</p>"
    assert segments_of(html) == [
        "<p>Intro sentence here.</p>",
        "<blockquote>A short real quotation.</blockquote>",
        "<p>Follow-up sentence here.</p>",
    ]
    # h1 不在 AIHOT 的 BLOCK 里，不单独成段。
    assert segments_of("<h1>Not a leaf heading.</h1><p>Body paragraph.</p>") == ["<p>Body paragraph.</p>"]


def test_short_definition_inside_blockquote_stays_one_leaf():
    """一条短定义：AIHOT 不拆 dl，研究站也不拆。没有块级子节点的短引用仍是一整段。"""
    html = "<blockquote><dl><dt>Term</dt><dd>A short definition of that term.</dd></dl></blockquote>"
    assert segments_of(html) == [html]
    assert coarse_segment_reason(html, recorded_segments=1) is None


def test_definition_list_entries_inside_blockquote_become_leaves():
    """多条 dt 不再收成一个 blockquote。外壳在写回时留着，叶子是每一条。"""
    html = (
        "<blockquote><dl>"
        "<dt>Line one of the pasted list.</dt>"
        "<dt>Line two stays readable on its own.</dt>"
        "</dl></blockquote>"
    )
    assert segments_of(html) == [
        "<dt>Line one of the pasted list.</dt>",
        "<dt>Line two stays readable on its own.</dt>",
    ]
    written = splice_segments(html, {0: "<dt>第一条</dt>", 1: "<dt>第二条</dt>"})
    assert written.count("<blockquote>") == 1
    assert written.count("<dl>") == 1
    assert "第一条" in written and "Line one" not in written
    assert segments_of(written) == ["<dt>第一条</dt>", "<dt>第二条</dt>"]
    assert splice_segments(html, {}) == html


def test_nested_definition_list_and_long_br_entry_split():
    """21118 的形状：引用里嵌着 dl，最里面的 dd 是用 <br> 粘起来的长文。条目和行都拆开。"""
    line = (
        "Services carried the quarter forward and the installed base kept growing through the year. "
        "Margins stayed high because the same customers kept paying."
    )
    lines = [f"{line} Point {index}." for index in range(16)]
    html = (
        "<p>Intro paragraph stays outside the quote.</p>"
        "<blockquote><dl>"
        "<dt>Outer heading of the pasted note.</dt>"
        "<dd><dl>"
        "<dt>Inner heading one is its own leaf.</dt>"
        "<dt>Inner heading two is its own leaf.</dt>"
        f"<dd>{'<br>'.join(lines)}</dd>"
        "</dl></dd>"
        "</dl></blockquote>"
        "<p>Closing paragraph after the quote.</p>"
    )
    segments = segments_of(html)
    lengths = [_plain_len(segment) for segment in segments]
    assert segments[0].startswith("<p>Intro")
    assert segments[-1].startswith("<p>Closing")
    assert any(segment.startswith("<dt>Outer heading") for segment in segments)
    assert any(segment.startswith("<dt>Inner heading one") for segment in segments)
    assert any(segment.startswith("<dt>Inner heading two") for segment in segments)
    assert len(segments) >= 8
    assert max(lengths) < 2_000
    assert not any(segment.lower().startswith("<blockquote") for segment in segments)
    assert not any("Point 0" in segment and "Point 15" in segment for segment in segments)
    assert coarse_segment_reason(html, recorded_segments=len(segments)) is None
    replacements = {index: f"<p>译{index}</p>" for index, segment in enumerate(segments)}
    written = splice_segments(html, replacements)
    assert written.count("<blockquote>") == 1
    assert "Point 7" not in written
    assert len(segments_of(written)) == len(segments)


def _daily_quote_html(*, chinese: bool = False) -> str:
    """21118 的形状：开头几段，一条短引用，然后一个包着几十段和若干标题的大 blockquote。"""
    intro = "早上好，这是开头。" if chinese else "Good morning, this is the introduction."
    section = "财报" if chinese else "Earnings"
    source = "摘自报道：" if chinese else "From the paper:"
    short = "一条没有内层块的短引用。" if chinese else "A short quotation with no inner blocks."
    restated = "作者接着用自己的话复述。" if chinese else "The author restates the point afterwards."
    closing = "长引用之后的收尾。" if chinese else "Closing note after the long quotation."
    head = "小节" if chinese else "Section"
    para = "段落" if chinese else "Paragraph"
    parts = [
        f"<p>{intro}</p>",
        f"<h3>{section}</h3>",
        f"<p>{source}</p>",
        f"<blockquote>{short}</blockquote>",
        f"<p>{restated}</p>",
    ]
    inner: list[str] = []
    for index in range(36):
        if index % 6 == 0:
            inner.append(f"<h3>{head} {index // 6}</h3>")
        inner.append(f"<p>{para} {index} stands alone in the bilingual view and is long enough.</p>")
    parts.append("<blockquote>" + "".join(inner) + "</blockquote>")
    parts.append(f"<p>{closing}</p>")
    return "".join(parts)


def _digest_lines(count: int) -> list[str]:
    return [f"Digest line {index} keeps its own row in the email." for index in range(count)]


def test_paragraph_with_many_br_splits_into_line_leaves():
    """22092 的形状：一个 <p> 里塞几十行 <br>，原来只有一段，对照页只能出一对。"""
    lines = _digest_lines(8)
    html = f'<p id="p-1">{"<br>".join(lines)}</p>'
    segments = iter_segments(html)
    assert len(segments) == 8
    assert segments[0].html == "<p>Digest line 0 keeps its own row in the email.</p>"
    assert segments[0].anchor == "p-1" and segments[1].anchor is None
    assert all(segment.html.count("<p") == 1 for segment in segments)
    # 不按行拆的那一份只有一个叶子，旧译文用得上。
    assert len(iter_segments(html, split_br=False)) == 1


def test_br_line_write_back_stays_in_the_same_paragraph():
    """一行一段写回原来的 <p>，行间还是 <br>；再切叶子数不变，id 也留着。"""
    html = '<p id="p-1">Alpha line.<br>Beta line.<br>Gamma line.</p>'
    assert len(segments_of(html)) == 3
    written = splice_segments(html, {0: "<p>甲</p>", 1: "乙", 2: "<p>丙</p>"})
    assert written == '<p id="p-1">甲<br>乙<br>丙</p>'
    assert len(segments_of(written)) == 3
    assert leaf_count_problem(html, written) is None


def test_br_line_write_back_keeps_the_list_item_shell():
    html = "<ul><li>First row.<br>Second row.<br>Third row.</li></ul>"
    assert len(segments_of(html)) == 3
    written = splice_segments(html, {0: "第一行。", 1: "第二行。", 2: "第三行。"})
    assert written == "<ul><li>第一行。<br>第二行。<br>第三行。</li></ul>"
    assert len(segments_of(written)) == 3
    assert leaf_count_problem(html, written) is None


def test_two_line_paragraph_stays_one_leaf():
    """两行的地址、诗句、署名不拆。"""
    html = "<p>First line of the address.<br>Second line of the address.</p>"
    assert segments_of(html) == [html]


def test_punctuation_only_br_lines_stay_leaves():
    """只留标点的行不删：译文同一个位置可能是「。」，删掉一边整段就错开。"""
    html = "<p>One.<br>—<br>Two.<br>|<br>Three.</p>"
    segments = segments_of(html)
    assert segments == ["<p>One.</p>", "<p>—</p>", "<p>Two.</p>", "<p>|</p>", "<p>Three.</p>"]
    written = splice_segments(html, {0: "一。", 2: "二。", 3: "｜", 4: "三。"})
    # 只有标点的译文不收（「｜」也不算字母），留着原文那一行，叶子数不变。
    assert written == "<p>一。<br>—<br>二。<br>|<br>三。</p>"
    assert len(segments_of(written)) == 5
    assert leaf_count_problem(html, written) is None


def test_br_lines_pair_one_to_one_when_both_sides_match():
    """两边的行数一样时按行对照：22092 修好之后就是这一种。"""
    en = f'<p id="p-1">{"<br>".join(_digest_lines(6))}</p>'
    zh = f'<p id="p-1">{"<br>".join(f"第{index}行。" for index in range(6))}</p>'
    pairs = bilingual_pairs(en, zh)
    assert pairs is not None and len(pairs) == 6
    for index, pair in enumerate(pairs):
        assert f"line {index}" in pair.en
        assert pair.zh and f"第{index}行" in pair.zh


def test_legacy_br_paragraph_with_other_line_count_stays_one_pair():
    """旧译文把整段几十行当一段译，行数和原文差一行时不拆，免得从那行起错位。"""
    en = "<p>" + "<br>".join(f"English row {index}." for index in range(5)) + "</p>"
    zh = "<p>" + "<br>".join(f"中文第{index}行。" for index in range(4)) + "</p>"
    pairs = bilingual_pairs(en, zh)
    assert pairs is not None and len(pairs) == 1
    assert "row 0" in pairs[0].en and pairs[0].zh and "第0行" in pairs[0].zh


def test_coarse_reason_ignores_br_lines_in_one_paragraph():
    """22092：库里记 2 段，按行拆出几十段，但块级叶子仍是 2，不该抽出来花模型钱重译。"""
    zh = f'<p id="p-1">{"<br>".join(f"第{index}行。" for index in range(30))}</p>'
    assert coarse_segment_reason(zh, recorded_segments=2) is None


def test_giant_blockquote_splits_into_paragraph_pairs():
    """大引用里的 p / h3 各成一段。没有任何一对占掉整篇的纯文本。"""
    english = _daily_quote_html()
    chinese = _daily_quote_html(chinese=True)
    segments = segments_of(english)
    assert len(segments) >= 40
    assert all(segment.lower().count("<p") <= 1 for segment in segments)
    assert any(segment.startswith("<blockquote>A short quotation") for segment in segments)
    giant = [segment for segment in segments if segment.lower().count("<p") > 1]
    assert giant == []
    # 顶层仍然是一个大引用；拆开的是叶子段，不是把外壳拆掉。
    top_giants = [
        block for block in top_level_blocks(english) if block.lower().count("<p") > 1
    ]
    assert len(top_giants) == 1 and top_giants[0].lower().startswith("<blockquote")

    pairs = bilingual_pairs(english, chinese)
    assert pairs is not None
    lengths = [_plain_len(pair.en) for pair in pairs]
    assert len(pairs) == len(segments)
    assert len(pairs) >= 40
    assert max(lengths) / sum(lengths) < 0.1
    assert max(lengths) < 200
    assert pairs[3].en.startswith("<blockquote>A short quotation")
    assert pairs[3].zh and "短引用" in pairs[3].zh
    assert any(pair.zh and "段落 35" in pair.zh and "Paragraph 35" in pair.en for pair in pairs)


def test_splice_writes_inner_leaves_and_keeps_blockquote():
    """译文写回叶子。短引用和大引用的 blockquote 外壳都还在。"""
    html = (
        "<p>Intro sentence here.</p>"
        "<blockquote><p>Short quoted sentence about the quarter.</p></blockquote>"
        "<blockquote><p>One pasted paragraph.</p><h3>Inside</h3><p>Two pasted paragraph.</p></blockquote>"
    )
    assert splice_segments(html, {}) == html
    segments = segments_of(html)
    assert [segment.startswith("<h3") for segment in segments] == [False, False, False, True, False]
    replacements = {
        index: ("<h3>标题</h3>" if segment.startswith("<h3") else f"<p>译{index}</p>")
        for index, segment in enumerate(segments)
    }
    written = splice_segments(html, replacements)
    assert written.count("<blockquote>") == 2
    assert written.count("</blockquote>") == 2
    assert "<p>译1</p>" in written
    assert "<h3>标题</h3>" in written
    assert "Short quoted" not in written
    assert written.index("<blockquote>") < written.index("<p>译1</p>") < written.index("</blockquote>")


def test_markdown_quote_lines_segment_inside_the_blockquote():
    """连续的 markdown > 行渲染成一个 blockquote，段是里面的 p，不是整段引用。"""
    import markdown

    source = "\n".join([
        "Intro paragraph stays outside.",
        "",
        "> First quoted paragraph of the daily.",
        ">",
        "> ### Pasted heading",
        ">",
        "> Second quoted paragraph of the daily.",
        "",
        "After the quotation.",
    ])
    html = markdown.markdown(source, extensions=["extra", "nl2br", "sane_lists"])
    assert html.lower().count("<blockquote") == 1
    segments = segments_of(html)
    assert len(segments) >= 4
    quoted = [segment for segment in segments if "First quoted paragraph" in segment]
    assert quoted and not quoted[0].lower().startswith("<blockquote")
    assert any("Pasted heading" in segment and segment.lower().startswith("<h3") for segment in segments)
    assert any("Second quoted paragraph" in segment for segment in segments)
    assert not any(
        segment.lower().startswith("<blockquote") and "Second quoted" in segment for segment in segments
    )


def test_markdown_definition_quote_splits_like_stratechery():
    """extra 把引用里的「: 」收成 dl。长粘贴拆开后没有几万字的叶子。"""
    import markdown

    lines = [
        "Intro stays outside.",
        "",
        "> Apple just delivered its",
        "> best quarter ever",
        "> , underpinned by modest growth.",
        "> : upgrade cycles are slow and the pasted definition keeps going.",
    ]
    row = "The installed base kept paying for services and the margin stayed high. "
    lines.extend(f"> {row * 2}Row {index}." for index in range(20))
    lines.extend(["", "After the quotation."])
    html = markdown.markdown("\n".join(lines), extensions=["extra", "nl2br", "sane_lists"])
    assert "<dl>" in html and "<dt>" in html
    segments = segments_of(html)
    lengths = [_plain_len(segment) for segment in segments]
    assert any("Intro stays" in segment for segment in segments)
    assert any("After the quotation" in segment for segment in segments)
    assert len(segments) >= 4
    assert max(lengths) < 4_000
    assert not any(segment.lower().startswith("<blockquote") and _plain_len(segment) > 2_000 for segment in segments)


def _pasted_definition_article() -> str:
    """#119 之后大约 180 段的正文，再加一段被收成一行一条 dt 的长粘贴。"""
    paras = "".join(
        f"<p>Paragraph {index} explains a distinct point about the market this year.</p>" for index in range(178)
    )
    fragments = "".join(
        f"<dt>Fragment {index} of the pasted note continues across the wrapped line.</dt>" for index in range(400)
    )
    return paras + f"<blockquote><dl>{fragments}</dl></blockquote>"


def test_glossary_pairs_stay_one_leaf_each():
    """术语表是 dt、dd 交替，不把术语收成一段。"""
    html = (
        "<blockquote><dl>"
        "<dt>iPhone</dt><dd>The handset business stayed flat.</dd>"
        "<dt>Services</dt><dd>The margin business kept growing.</dd>"
        "<dt>Wearables</dt><dd>The accessory business matured.</dd>"
        "</dl></blockquote>"
    )
    assert [segment.split(">", 1)[0] for segment in segments_of(html)] == [
        "<dt", "<dd", "<dt", "<dd", "<dt", "<dd",
    ]


def test_consecutive_definition_lines_pack_into_readable_chunks():
    """粘贴的连续短 dt 收成段，不保留几百个只有几个词的叶子。"""
    fragments = "".join(f"<dt>Fragment {index} of the wrapped paste.</dt>" for index in range(12))
    html = f"<blockquote><dl>{fragments}</dl></blockquote>"
    segments = segments_of(html)
    assert 1 < len(segments) < 12
    assert max(_plain_len(segment) for segment in segments) < 700
    assert all(segment.startswith("<p>") for segment in segments)
    written = splice_segments(html, {index: f"<p>译{index}</p>" for index in range(len(segments))})
    assert written.count("<blockquote>") == 1
    assert "Fragment 0" not in written
    assert len(segments_of(written)) == len(segments)
    assert splice_segments(html, {}) == html


def test_coarse_reason_flags_old_segment_count_and_definition_lists():
    """段数远少于现在的叶子就要重译。定义列表粘贴记下的段数对不上时抽中。"""
    english = _daily_quote_html()
    leaves = len(segments_of(english))
    assert leaves >= 40
    assert coarse_segment_reason(english, recorded_segments=7) == "segments-below-leaves"
    assert coarse_segment_reason(english, recorded_segments=leaves) is None
    # #119 之后 21118 记下的段数大约是 180。粘贴收成段之后会多出几十段，重检抽得到。
    quote = _pasted_definition_article()
    quote_leaves = segments_of(quote)
    assert len(quote_leaves) >= 178 + 40
    assert max(_plain_len(segment) for segment in quote_leaves) < 2_000
    assert coarse_segment_reason(quote, recorded_segments=180) == "segments-below-leaves"
    assert coarse_segment_reason(quote, recorded_segments=len(quote_leaves)) is None


class _RowsConn:
    """list_coarse_translations 用的假连接，按 SQL 分辨两件事：

    - 译文队列那条多表查询返回给定的 row；
    - 译制前的清洗（来源 kind）、模板句统计和图片哈希都按「没有」回答，不连库。
    """

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.sql = ""

    def execute(self, sql, params=None):
        self.sql = sql
        return self

    def fetchone(self):
        if "FROM sources" in self.sql:
            return {"kind": "rss"}
        if "count(*)" in self.sql:
            return {"n": 1, "t": ""}
        return None

    def fetchall(self):
        if "article_images" in self.sql or self.sql.strip().startswith("SELECT id, body_md FROM articles"):
            return []
        return self.rows


def test_list_coarse_picks_definition_list_when_recorded_count_is_stale():
    """21118 这种译文：#119 记下大约 180 段，定义列表拆开后多出几十段，重检要抽到。"""
    from researchlib.translate.service import list_coarse_translations

    quote = _pasted_definition_article()
    leaves = len(segments_of(quote))
    row = {
        "id": 21118,
        "source_id": "stratechery",
        "title": "A daily",
        "body_md": "unused because the translation html already splits",
        "body_text": "x" * 8000,
        "raw_meta": {},
        "segments": 180,
        "body_html": quote,
    }

    found = list_coarse_translations(_RowsConn([row]), limit=10, source_id="stratechery")
    assert found == [{"id": 21118, "source": "stratechery", "reason": "segments-below-leaves"}]
    row["segments"] = leaves
    assert list_coarse_translations(_RowsConn([row]), limit=10, source_id="stratechery") == []


def test_list_coarse_uses_source_when_translation_was_flattened():
    """译文被收成少数几段时，用原文叶子数判断，而不是只看译文 HTML。"""
    from researchlib.translate.service import list_coarse_translations

    body = "\n\n".join(
        f"Paragraph {index} explains a distinct point about the market this year." for index in range(24)
    )
    row = {
        "id": 9,
        "source_id": "stratechery",
        "title": "A daily",
        "body_md": body,
        "body_text": "x" * 8000,
        "raw_meta": {},
        "segments": 6,
        "body_html": "".join(f"<p>Short translated paragraph {index}.</p>" for index in range(6)),
    }

    found = list_coarse_translations(_RowsConn([row]), limit=10, source_id="stratechery")
    assert found == [{"id": 9, "source": "stratechery", "reason": "segments-below-source"}]

    row["segments"] = 24
    row["body_html"] = "".join(f"<p>Paragraph {index} in Chinese remains one leaf.</p>" for index in range(24))
    assert list_coarse_translations(_RowsConn([row]), limit=10, source_id="stratechery") == []


def _plain_pair(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", html or "")).strip()


def test_stable_leaf_closes_tags_and_keeps_inline_markup():
    """没闭合的 p 要补上；链接和强调留在这一段里面，不再拆出新叶子。"""
    closed = stable_leaf('<p>见 <a id="L0">链接</a> 和 <strong>重点</strong>', tag="p")
    assert closed.startswith("<p>") and closed.endswith("</p>")
    assert closed.count("<p") == 1
    assert 'id="L0"' in closed and "链接" in closed and "重点" in closed
    assert "<br>" in stable_leaf("<p>第一行<br>第二行</p>", tag="p", keep_br=True)
    flattened = stable_leaf("<p>第一行<br>第二行</p>", tag="dt", keep_br=False)
    assert flattened == "<dt>第一行 第二行</dt>"
    merged = stable_leaf("<p>上一段</p><p>下一段</p>", tag="h3")
    assert merged == "<h3>上一段 下一段</h3>"


def _long_dt(label: str) -> str:
    filler = "The installed base kept paying for services and the margin stayed high. "
    body = (f"{label} {filler}") * 6
    assert len(body) >= 500
    return f"<dt>{body}</dt>"


def test_short_dt_writeback_stays_one_to_one_and_does_not_repeat_openings():
    """21118：连续长 dt 译短后不能重新打包。同一英文开头的两份对照，中文也得是同一段。"""
    labels = [
        "Regulatory and legal risks:",
        "On the macro-policy front Apple stayed cautious about fines:",
        "Services growth could attract unwanted attention today:",
        "Wearables, Home and Accessories matured this cycle:",
        "The Mac Apple Silicon advantage showed up in the quarter:",
        "Personal computing stayed differentiated in the holiday quarter:",
    ]
    iphone = "iPhone – premium focus amid plateau: revenue was flat and the installed base stayed loyal."
    mac = "Mac – the Apple Silicon advantage: revenue surged and the chip transition paid off this year."
    html = (
        "<p>Intro paragraph before the paste.</p>"
        "<blockquote><dl>" + "".join(_long_dt(label) for label in labels) + "</dl></blockquote>"
        f"<ul><li>{iphone}</li></ul><p><strong>{iphone}</strong></p>"
        f"<ul><li>{mac}</li></ul><p><strong>{mac}</strong></p>"
        "<h3>The Unbundling of Substantiation</h3>"
        "<p><em>[外部图片已省略]</em></p>"
        "<p>Closing note after the picture stays its own paragraph.</p>"
    )
    segments = segments_of(html)
    assert len(segments) >= 12
    assert sum(segment.startswith("<dt") for segment in segments) == 6

    def marker(segment: str, index: int) -> str:
        plain = _plain_pair(segment)
        for token in (
            "Unbundling",
            "Closing",
            "Intro",
            "iPhone",
            "Mac –",
            "Regulatory",
            "macro-policy",
            "Services growth",
            "Wearables",
            "Apple Silicon",
            "Personal computing",
        ):
            if token in plain:
                return token
        return f"段{index}"

    replacements = {}
    for index, segment in enumerate(segments):
        if "外部图片已省略" in segment:
            continue
        # 模型有时不闭合标签，有时把 dt 改成 p。两种都要仍是一段。
        replacements[index] = f"<p>{marker(segment, index)}的译文"

    written = splice_segments(html, replacements)
    assert leaf_count_problem(html, written) is None
    assert len(segments_of(written)) == len(segments)
    pairs = bilingual_pairs(html, written)
    assert pairs is not None and len(pairs) == len(segments)
    assert sum(pair.zh is None for pair in pairs) == 1

    def opening(value: str | None) -> str:
        return _plain_pair(value or "")[:60]

    for left, right in zip(pairs, pairs[1:], strict=False):
        if left.en and opening(left.en) == opening(right.en):
            assert opening(left.zh)[:12] == opening(right.zh)[:12]

    iphone_pairs = [pair for pair in pairs if opening(pair.en).startswith("iPhone – premium")]
    assert len(iphone_pairs) == 2
    assert all(pair.zh and "iPhone的译文" in pair.zh for pair in iphone_pairs)
    assert all(pair.zh and "Mac的译文" not in pair.zh for pair in iphone_pairs)

    wearables = next(pair for pair in pairs if "Wearables, Home and Accessories" in pair.en)
    assert wearables.zh and "Wearables的译文" in wearables.zh
    assert "Services growth" not in wearables.zh

    heading = next(pair for pair in pairs if "Unbundling of Substantiation" in pair.en)
    assert heading.zh and "Unbundling的译文" in heading.zh
    assert "外部图片已省略" not in heading.zh
    image = next(pair for pair in pairs if "外部图片已省略" in pair.en)
    assert image.zh is None
    closing = pairs[-1]
    assert "Closing note" in closing.en and closing.zh and "Closing的译文" in closing.zh


def test_unclosed_translation_does_not_pair_heading_with_image_stub():
    """没闭合的译文不能把后面的标题吞掉，标题也不能配上「外部图片已省略」。"""
    line = (
        "Services carried the quarter forward and the installed base kept growing through the year. "
        "Margins stayed high because the same customers kept paying."
    )
    lines = [f"{line} Point {index}." for index in range(16)]
    html = (
        "<p>Intro paragraph stays outside the quote.</p>"
        "<blockquote><dl>"
        "<dt>First heading of the pasted note.</dt>"
        "<dt>Second heading of the pasted note.</dt>"
        f"<dd>{'<br>'.join(lines)}</dd>"
        "</dl></blockquote>"
        "<h3>The Unbundling of Substantiation</h3>"
        "<p><em>[外部图片已省略]</em></p>"
        "<p>Closing note after the picture stays its own paragraph.</p>"
    )
    segments = segments_of(html)
    assert any(segment.startswith("<h3") and "Unbundling" in segment for segment in segments)
    assert any("外部图片已省略" in segment for segment in segments)
    assert len(segments) >= 6
    replacements = {
        index: "<h3>论证支撑的解绑" if segment.startswith("<h3") else f"<p>译文{index}"
        for index, segment in enumerate(segments)
        if "外部图片已省略" not in segment
    }
    written = splice_segments(html, replacements)
    assert leaf_count_problem(html, written) is None
    pairs = bilingual_pairs(html, written)
    assert pairs is not None and len(pairs) == len(segments)
    heading = next(pair for pair in pairs if "Unbundling of Substantiation" in pair.en)
    assert heading.zh and "论证支撑的解绑" in heading.zh
    assert "外部图片已省略" not in heading.zh
    image = next(pair for pair in pairs if "外部图片已省略" in pair.en)
    assert image.zh is None
    assert pairs[-1].zh and "译文" in pairs[-1].zh
    assert "Closing note" in pairs[-1].en


def test_soft_dedupe_leaves_a_missing_leaf_paired_with_the_image_stub():
    """译文已经少了一叶时，同一套去重拉不齐。标题会配上图片占位，文末没有中文。"""
    english = (
        "<h3>The Unbundling of Substantiation</h3>"
        "<p><em>[外部图片已省略]</em></p>"
        "<p>Closing note after the picture stays visible.</p>"
    )
    chinese = "<p><em>[外部图片已省略]</em></p><p>收尾译文。</p>"
    assert leaf_count_problem(english, chinese) == "leaves 3 vs 2"
    pairs = bilingual_pairs(english, chinese)
    assert pairs is not None and len(pairs) == 3
    assert "Unbundling" in pairs[0].en
    assert pairs[0].zh and "外部图片已省略" in pairs[0].zh
    assert "Closing note" in pairs[2].en and pairs[2].zh is None
