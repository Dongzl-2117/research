import re

from researchlib.translate.language import is_chinese
from researchlib.translate.pair import bilingual_pairs, stamp_paragraph_ids
from researchlib.translate.segments import (
    chunk_segments,
    coarse_segment_reason,
    segments_of,
    shield,
    splice_segments,
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


def test_definition_list_inside_blockquote_stays_one_leaf():
    """dl 不在 CONTAINER 里。包着定义列表的引用按 AIHOT 仍是一整段。"""
    html = (
        "<blockquote><dl>"
        "<dt>Line one of the pasted list.</dt>"
        "<dt>Line two stays inside the same quote.</dt>"
        "</dl></blockquote>"
    )
    assert segments_of(html) == [html]


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


def test_coarse_reason_flags_old_segment_count_not_a_definition_list():
    """段数远少于现在的叶子就要重译。定义列表引用按规则仍是一段，不反复抽中。"""
    english = _daily_quote_html()
    leaves = len(segments_of(english))
    assert leaves >= 40
    assert coarse_segment_reason(english, recorded_segments=7) == "segments-below-leaves"
    assert coarse_segment_reason(english, recorded_segments=leaves) is None
    quote = (
        "<blockquote><dl>"
        + "".join(f"<dt>Line {index} of a pasted list stays one quote.</dt>" for index in range(30))
        + "</dl></blockquote>"
    )
    assert segments_of(quote) == [quote]
    assert coarse_segment_reason(quote, recorded_segments=1) is None


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

    class Conn:
        def execute(self, sql, params):
            assert "stratechery" in params
            return self

        def fetchall(self):
            return [row]

    found = list_coarse_translations(Conn(), limit=10, source_id="stratechery")
    assert found == [{"id": 9, "source": "stratechery", "reason": "segments-below-source"}]

    row["segments"] = 24
    row["body_html"] = "".join(f"<p>Paragraph {index} in Chinese remains one leaf.</p>" for index in range(24))
    assert list_coarse_translations(Conn(), limit=10, source_id="stratechery") == []
