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
    translatable_blocks,
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


def test_br_line_write_back_stays_one_leaf_per_sentence():
    """一句一段写回成并列的 <p>。译文没有句号也不会被并回一段，段落号留在第一句。"""
    html = '<p id="p-1">Alpha line.<br>Beta line.<br>Gamma line.</p>'
    assert len(segments_of(html)) == 3
    written = splice_segments(html, {0: "<p>甲</p>", 1: "乙", 2: "<p>丙</p>"})
    assert written == '<p id="p-1">甲</p><p>乙</p><p>丙</p>'
    assert len(segments_of(written)) == 3
    assert leaf_count_problem(html, written) is None


def test_br_line_write_back_keeps_the_list_item_shell():
    html = "<ul><li>First row.<br>Second row.<br>Third row.</li></ul>"
    assert len(segments_of(html)) == 3
    written = splice_segments(html, {0: "第一行。", 1: "第二行。", 2: "第三行。"})
    assert written == "<ul><li><p>第一行。</p><p>第二行。</p><p>第三行。</p></li></ul>"
    assert len(segments_of(written)) == 3
    assert leaf_count_problem(html, written) is None


def test_two_line_paragraph_stays_one_leaf():
    """两行的地址、诗句、署名不拆。"""
    html = "<p>First line of the address.<br>Second line of the address.</p>"
    assert segments_of(html) == [html]


def test_punctuation_only_br_lines_join_the_neighboring_sentence():
    """只留标点的行不删，并进相邻的句子。两边同一位置都是「—」时，段数不会岔开。"""
    html = "<p>One.<br>—<br>Two.<br>|<br>Three.</p>"
    segments = segments_of(html)
    assert [re.sub(r"<[^>]+>", "", segment) for segment in segments] == ["One.", "— Two.", "| Three."]
    written = splice_segments(html, {0: "一。", 1: "破折号，二。", 2: "竖线，三。"})
    assert leaf_count_problem(html, written) is None
    assert len(segments_of(written)) == 3
    assert "One." not in written and "一。" in written


def _plain_segment(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", html)).strip()


def _write_translatable(html: str) -> str:
    """按译制的做法写回：没有拉丁字母的叶子不送去翻译，原样留在这一格。"""
    replacements = {index: f"<p>译文{index}。</p>" for index, _block in translatable_blocks(html)}
    return splice_segments(html, replacements)


def test_closing_paren_line_after_a_sentence_joins_that_sentence():
    """句末已经收过，下一行只剩「).」时并进前一句。单独成叶的话写回后再切会少一片。"""
    html = "<p>First sentence is done.<br>).<br>Second sentence follows.</p>"
    plains = [_plain_segment(segment) for segment in segments_of(html)]
    assert plains == ["First sentence is done.).", "Second sentence follows."]
    written = _write_translatable(html)
    assert leaf_count_problem(html, written) is None
    assert len(segments_of(written)) == len(plains)


def test_soft_wrapped_closing_paren_is_not_split_off_past_seven_hundred_chars():
    """21130 的形状：一句已经结束，后面的半句累计超过 700 字，句末「).」单独一行。

    按定义列表的 700 字上限切开时，「).」自己成叶。它没有字母，不进 ``t``。
    同一次写回把其余行换成 ``<p>`` 之后，再切段会丢掉这片，出现 leaves N vs N-1。
    """
    clause = "clause about scale and copyright keeps going"
    lines = ["Opening sentence stays on its own."]
    pending = 0
    while pending <= 700:
        lines.append(clause)
        pending += len(clause) + 1
    lines.append(").")
    assert len(lines) >= 3
    html = "<p>" + "<br>".join(lines) + "</p>"
    plains = [_plain_segment(segment) for segment in segments_of(html)]
    assert plains[0] == "Opening sentence stays on its own."
    assert len(plains) == 2
    assert plains[1].count(clause) == lines.count(clause)
    assert plains[1].endswith("going).")
    written = _write_translatable(html)
    assert leaf_count_problem(html, written) is None
    assert len(segments_of(written)) == 2


def test_markdown_blockquote_closing_paren_stays_in_the_sentence():
    """引用里的软换行（nl2br）累计超过 700 字、句末「).」单独一行时，仍和前一句是一片叶子。"""
    import markdown

    clause = "clause about scale and copyright keeps going"
    lines = ["Opening sentence stays on its own.", ""]
    pending = 0
    while pending <= 700:
        lines.append(f"> {clause}")
        pending += len(clause) + 1
    lines.append("> ).")
    html = markdown.markdown("\n".join(lines), extensions=["extra", "nl2br", "sane_lists"])
    assert "<br" in html.lower()
    plains = [_plain_segment(segment) for segment in segments_of(html)]
    assert plains[0] == "Opening sentence stays on its own."
    assert len(plains) == 2
    assert plains[1].count(clause) == lines.count(f"> {clause}")
    assert plains[1].endswith("going).")
    assert not any(plain == ")." for plain in plains)
    written = _write_translatable(html)
    assert leaf_count_problem(html, written) is None
    assert len(segments_of(written)) == 2


def _interview_soft_breaks() -> list[str]:
    """22209 的形状：链接原来占一行，软换行把人名和句号拆开。"""
    return [
        "This week’s interview is a conversation with Hollywood super-agent",
        "Ari Emanuel",
        ". Emanuel is the Executive Chairman of",
        "WME Group",
        ", a live events company.",
        "Yeah, for sure.",
        "Topics:",
        "The State of Social",
        "|",
        "Roll the Calls",
        "Ari Emanuel:",
        "Can I ask you one question?",
    ]


def _interview_soft_breaks_zh() -> list[str]:
    return [
        "本周这场访谈是和好莱坞超级经纪人",
        "阿里·伊曼纽尔",
        "。伊曼纽尔是",
        "WME 集团",
        "的董事长。",
        "对，没错。",
        "主题：",
        "社交现状",
        "|",
        "Roll the Calls",
        "阿里·伊曼纽尔：",
        "我能问一个问题吗？",
    ]


def test_soft_break_interview_lines_pack_into_sentences():
    """半句、人名、链到句号上的续写收成一句，不再各成一对。"""
    lines = _interview_soft_breaks()
    html = "<p>" + "<br>".join(lines) + "</p>"
    segments = segments_of(html)
    plains = [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", segment)).strip() for segment in segments]
    assert len(lines) == 12
    assert len(segments) == 3
    assert plains[0].startswith("This week’s interview")
    assert "super-agent Ari Emanuel. Emanuel is" in plains[0]
    assert "WME Group, a live events company." in plains[0]
    assert plains[1] == "Yeah, for sure."
    assert plains[2].endswith("Can I ask you one question?")
    assert "Ari Emanuel:" in plains[2]
    assert all(plain != "Ari Emanuel" for plain in plains)
    assert all(len(plain) >= 15 for plain in plains)


def test_soft_break_interview_writeback_keeps_leaf_count():
    """按句写回成若干 <p> 之后再切，叶子数不变，人名不会重新掉成一片。"""
    html = '<p id="p-1">' + "<br>".join(_interview_soft_breaks()) + "</p>"
    leaves = segments_of(html)
    assert len(leaves) == 3
    assert iter_segments(html)[0].anchor == "p-1"
    written = splice_segments(html, {index: f"<p>译文{index}。</p>" for index in range(len(leaves))})
    assert written.count("<br>") == 0
    assert leaf_count_problem(html, written) is None
    assert len(segments_of(written)) == len(leaves)
    assert "Ari Emanuel" not in written


def test_parallel_soft_breaks_pack_to_the_same_leaf_count():
    """句末对齐的中英软换行收成同样多的段，对照按句并排，不退成整段一对。"""
    en = "<p>" + "<br>".join(_interview_soft_breaks()) + "</p>"
    zh = "<p>" + "<br>".join(_interview_soft_breaks_zh()) + "</p>"
    en_leaves = segments_of(en)
    zh_leaves = segments_of(zh)
    assert len(en_leaves) == len(zh_leaves) == 3
    assert len(en_leaves) < len(_interview_soft_breaks())
    pairs = bilingual_pairs(en, zh)
    assert pairs is not None and len(pairs) == 3
    assert "Ari Emanuel" in pairs[0].en and pairs[0].zh and "阿里·伊曼纽尔" in pairs[0].zh
    assert "Yeah, for sure." in pairs[1].en and pairs[1].zh and "对，没错。" in pairs[1].zh
    assert not any(re.sub(r"<[^>]+>", "", pair.en).strip() == "Ari Emanuel" for pair in pairs)


def test_soft_break_link_stays_inside_the_sentence():
    html = (
        "<p>a conversation with Hollywood super-agent<br>"
        '<a href="https://example.com/ari">Ari Emanuel</a><br>'
        ". Emanuel is the chairman of the group.</p>"
    )
    segments = segments_of(html)
    assert len(segments) == 1
    assert 'href="https://example.com/ari"' in segments[0]
    assert "Ari Emanuel. Emanuel is" in re.sub(r"<[^>]+>", "", segments[0])


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


def _plateau_sentences(count: int) -> list[str]:
    return [
        (
            f"Point {index} carried the hardware plateau and the installed base kept paying "
            "through another quarter of record margins."
        )
        for index in range(count)
    ]


def test_long_paragraph_splits_into_sentences_and_keeps_the_link():
    """21118 / 22195 的形状：一段超过 900 字、里面有多句，按句切开，链接留在所在的那一句。"""
    sentences = _plateau_sentences(10)
    sentences[1] = (
        'Point 1 cites <a href="https://example.com/filing">example.com</a> inside the sentence '
        "about margins and the installed base."
    )
    html = '<p id="p-4">' + " ".join(sentences) + "</p>"
    assert _plain_len(html) > 900
    leaves = segments_of(html)
    plains = [_plain_segment(leaf) for leaf in leaves]
    assert len(leaves) == 10
    assert all(_plain_len(leaf) <= 900 for leaf in leaves)
    assert plains[0].startswith("Point 0")
    assert "example.com/filing" in leaves[1]
    assert "Point 1 cites" in plains[1]
    assert "Point 2" not in plains[1]
    assert not any(plain.startswith("com ") or plain.startswith("9 ") for plain in plains)
    written = splice_segments(html, {index: f"<p>译文{index}。</p>" for index in range(len(leaves))})
    assert written.startswith('<p id="p-4">')
    assert leaf_count_problem(html, written) is None
    assert len(segments_of(written)) == 10


def test_one_long_sentence_is_not_cut_in_the_middle():
    """没有句号的长句保持一整段。按字数切开会把 21130 那种「).」切成没有字母的叶子。"""
    clause = "clause about scale and copyright keeps going "
    html = "<p>" + clause * 30 + "at the end).</p>"
    assert _plain_len(html) > 900
    leaves = segments_of(html)
    assert len(leaves) == 1
    assert "at the end)." in _plain_segment(leaves[0])


def test_decimals_and_domains_are_not_sentence_breaks():
    """46.9 和 example.com 里的句号不切句。"""
    sentences = [
        f"Item {index} rose 46.9 percent after example.com published the filing for the holiday quarter."
        for index in range(12)
    ]
    html = "<p>" + " ".join(sentences) + "</p>"
    assert _plain_len(html) > 900
    plains = [_plain_segment(leaf) for leaf in segments_of(html)]
    assert len(plains) == 12
    assert all("46.9" in plain and "example.com" in plain for plain in plains)


def test_doubled_long_list_item_collapses_when_both_sides_repeat():
    """21118：长列表项和紧跟着的同文段落，两边都有时收成一份，不再是两千字的两对。"""
    body = " ".join(_plateau_sentences(10))
    en = f"<ul><li>{body}</li></ul><p><strong>Point</strong> {body[len('Point'):]}</p>"
    bits = [
        (
            f"第{index}句译文说明硬件平台和服务收入，已有用户这个季度继续付费，利润率维持在纪录高位。"
            "服务收入托住了出货，已有用户继续留下来，这一句要单独成对，不能并到旁边。请单独保留。"
        )
        for index in range(10)
    ]
    assert all(len(bit) >= 80 for bit in bits)
    zh = "<ul><li>" + "".join(f"<p>{bit}</p>" for bit in bits) + "</li></ul>"
    zh += "".join(f"<p>{bit}</p>" for bit in bits)
    assert len(segments_of(en)) == 20
    pairs = bilingual_pairs(en, zh)
    assert pairs is not None and len(pairs) == 10
    assert all(_plain_len(pair.en) <= 900 for pair in pairs)
    assert pairs[0].en.startswith("<p>Point 0") or "Point 0" in pairs[0].en
    assert pairs[0].zh and "第0句" in pairs[0].zh
    assert pairs[9].zh and "第9句" in pairs[9].zh
    assert sum("Point 0" in pair.en for pair in pairs) == 1


def test_repeated_section_collapses_on_both_sides():
    """21130：连续几段在后文再贴一遍。纯文本相同的后一份两边都收掉，不硬配。"""
    paragraphs = [
        (
            f"<p>Paragraph {index} explains a distinct point about the fair use record "
            "and is long enough that a repeated block counts as the same leaf.</p>"
        )
        for index in range(5)
    ]
    en = "".join(paragraphs + paragraphs)
    zh_bits = [
        (
            f"<p>第{index}段译文足够长，用来覆盖引用后再贴一遍的规则，不要收成别的段。"
            "这段还要把后文写完整，已有用户继续付费，利润率维持在高位，避免因为太短而被重复规则放过。请单独保留。</p>"
        )
        for index in range(5)
    ]
    assert all(len(re.sub(r"<[^>]+>", "", bit)) >= 80 for bit in zh_bits)
    zh = "".join(zh_bits + zh_bits)
    assert len(segments_of(en)) == 10
    pairs = bilingual_pairs(en, zh)
    assert pairs is not None and len(pairs) == 5
    assert "Paragraph 0" in pairs[0].en and pairs[0].zh and "第0段" in pairs[0].zh
    assert "Paragraph 4" in pairs[4].en and pairs[4].zh and "第4段" in pairs[4].zh


def test_shared_opening_with_a_different_ending_stays_two_pairs():
    """只共享开头、后文不同的两段都留着，不把第二份提示收成第一份。"""
    first = (
        "I am the author of this note. This is important because the report should follow "
        "the usual voice and stop after the latest quarter."
    )
    second = (
        "I am the author of this note. This is important because the report should follow "
        "the holiday quarter and the China angle in particular."
    )
    en = f"<p>{first}</p><p>{second}</p>"
    zh = (
        "<p>第一份提示到最新季度就停，后文和下一份不一样，两段都要留。</p>"
        "<p>第二份提示改看假日季和中国，不能因为开头相同就被收掉。</p>"
    )
    pairs = bilingual_pairs(en, zh)
    assert pairs is not None and len(pairs) == 2
    assert "latest quarter" in pairs[0].en and "holiday quarter" in pairs[1].en
    assert pairs[0].zh and "最新季度" in pairs[0].zh
    assert pairs[1].zh and "假日季" in pairs[1].zh


def test_blockquote_with_one_br_drops_the_repeated_opening():
    """22195：引用里一个 <br> 粘了两段，后面的段落只重复第一段。拆开后第一段只出现一次。"""
    first = " ".join(
        f"Opening point {index} about the foldable phone and the lock screen layout stays one sentence."
        for index in range(6)
    )
    second = " ".join(
        f"Following point {index} about developer interfaces and the outer display stays one sentence."
        for index in range(6)
    )
    assert len(first) < 900 < len(first) + len(second)
    en = f"<blockquote><p>{first}<br>{second}</p></blockquote><p>{first}</p>"
    line = "这句话专门写长，用来把引用的两段撑过九百字的门槛，这样才会按换行拆开而不是整段一对。"
    zh_first = " ".join(f"开头{index} {line}" for index in range(16))
    zh_second = " ".join(f"后文{index} {line}" for index in range(16))
    assert len(zh_first) < 900 < len(zh_first) + len(zh_second)
    zh = f"<blockquote><p>{zh_first}<br>{zh_second}</p></blockquote><p>{zh_first}</p>"
    pairs = bilingual_pairs(en, zh)
    assert pairs is not None
    assert len(pairs) == 2
    assert "Opening point 0" in pairs[0].en and "Following point" not in pairs[0].en
    assert "Following point 0" in pairs[1].en and "Opening point 0" not in pairs[1].en
    assert pairs[0].zh and "开头0" in pairs[0].zh
    assert pairs[1].zh and "后文0" in pairs[1].zh
    assert sum("Opening point 0" in pair.en for pair in pairs) == 1
    assert all(_plain_len(pair.en) < 2_000 for pair in pairs)


def test_one_sided_long_repeat_is_not_hard_zipped():
    """只在英文侧重复的长段不单边丢掉，后记仍对着自己的译文。"""
    body = " ".join(_plateau_sentences(10))
    note = "A later distinct note about the quarter that must stay with its own translation and not slide over."
    en = f"<ul><li>{body}</li></ul><p>{body}</p><p>{note}</p>"
    zh = (
        "<ul><li><p>列表项的译文，和后面那段不是同一句。</p></li></ul>"
        "<p>这不是同一句译文，所以不能为了对齐把英文重复段丢掉，后记还在自己的位置。</p>"
        "<p>后记要留在第三组，不能配到重复段上。</p>"
    )
    pairs = bilingual_pairs(en, zh)
    assert pairs is not None and len(pairs) == 3
    assert "Point 0" in pairs[0].en and pairs[0].zh and "列表项" in pairs[0].zh
    assert "Point 0" in pairs[1].en and pairs[1].zh and "不是同一句" in pairs[1].zh
    assert note in pairs[2].en and pairs[2].zh and "第三组" in pairs[2].zh


def test_definition_url_fragment_stays_inside_the_sentence():
    """域名单独一条 dt 时不切成一片叶子，链接留在「( example.com ).」那一句里。"""
    tail = "The quotation then finishes the sentence about revenue and the installed base. " * 8
    html = (
        "<blockquote><dl>"
        "<dt>See the filing (</dt>"
        '<dt><a href="https://example.com/10k">example.com</a></dt>'
        f"<dt>). {tail}</dt>"
        "<dt>Another short line of the pasted note.</dt>"
        "<dt>A third line keeps the run long enough to pack.</dt>"
        "<dt>A fourth line closes the sample paste.</dt>"
        "</dl></blockquote>"
    )
    leaves = segments_of(html)
    plains = [_plain_segment(leaf) for leaf in leaves]
    assert plains
    assert not any(plain == "example.com" for plain in plains)
    joined = next(leaf for leaf in leaves if "example.com/10k" in leaf)
    joined_plain = _plain_segment(joined)
    assert "See the filing" in joined_plain
    assert "finishes the sentence" in joined_plain
    assert "example.com)." in joined_plain or "example.com )." in joined_plain


def test_prefix_restatement_collapses_when_both_sides_match():
    """21130 的 Meta 开头：后一段是前一段的开头，两边都是这样时收掉后一段。"""
    clause = "the company escaped the copyright claim because the record did not show market harm "
    long = clause * 16 + "and then the opinion turns to the fourth factor."
    short = (clause * 8).strip()
    assert short
    assert _plain_len(short) >= 80
    assert _plain_len(long) > 900
    en = f"<p>{long}</p><p>{short}</p>"
    zh_long = "甲" * 240 + "这段译文先把开头和后文放在一起。"
    zh_short = "甲" * 240
    zh = f"<p>{zh_long}</p><p>{zh_short}</p>"
    pairs = bilingual_pairs(en, zh)
    assert pairs is not None and len(pairs) == 1
    assert "fourth factor" in pairs[0].en
    assert pairs[0].zh and "后文" in pairs[0].zh


def test_near_identical_paragraph_collapses_when_both_sides_match():
    """差一个词的近重复段，两边都这样时收成一份。后文不同的不收。"""
    clause = "revenue rose after the filing and the installed base kept paying through the holiday quarter "
    text = (clause * 6).strip() + "."
    near = text.replace("holiday", "holidays", 1)
    assert text != near
    en = f"<p>{text}</p><p>{near}</p>"
    zh_a = "甲" * 120 + "。"
    zh_b = "甲" * 120 + "啊。"
    zh = f"<p>{zh_a}</p><p>{zh_b}</p>"
    pairs = bilingual_pairs(en, zh)
    assert pairs is not None and len(pairs) == 1
    assert "holiday quarter" in pairs[0].en


def test_reuse_duplicate_sources_copies_the_first_translation():
    """写回仍是一段对一段：重复段用第一次的译文，不另起一条不同的中文。"""
    from researchlib.translate.service import reuse_duplicate_sources

    sentence = (
        "Point zero carried the hardware plateau and the installed base kept paying through another quarter."
    )
    other = "A different sentence about the following quarter is also long enough to be its own leaf here."
    got = reuse_duplicate_sources([sentence, other, sentence], ["甲", "乙", "丙"])
    assert got == ["甲", "乙", "甲"]
    assert len(got) == 3
