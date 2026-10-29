from researchlib.translate.language import is_chinese
from researchlib.translate.pair import bilingual_pairs, stamp_paragraph_ids
from researchlib.translate.segments import chunk_segments, shield, top_level_blocks, unshield
from researchlib.translate.service import _dedupe_consecutive_blocks


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


def test_pairs_align_when_translation_dropped_repeated_quotes():
    """原文未去重、译文已去重，块数差大于 1 时对照仍逐段配上，不退回纯中文。"""
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


def test_one_dropped_repeat_does_not_shift_following_paragraph():
    """只少一段时，旧逻辑会按位置错配。去重后解说段仍对着自己的译文。"""
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
    assert pairs[1].zh and "真正要紧" in pairs[1].zh
    assert "pivot" in pairs[1].en
    assert pairs[1].anchor == "p-2"


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


def test_pairs_align_when_model_repeated_a_paragraph():
    """模型把同一段连吐三遍、入库去重后中文会短一截。对照仍按剩下的段配。"""
    sentence = "聚合理论解释平台为什么会赢，这一段足够长所以连续重复要去掉。"
    en = "<p>Aggregation theory explains why platforms win.</p><p>Costs come second in this note.</p>"
    zh = f"<p>{sentence}</p><p>{sentence}</p><p>{sentence}</p><p>成本是后面的第二点。</p>"
    assert abs(len(top_level_blocks(en)) - len(top_level_blocks(zh))) > 1
    pairs = bilingual_pairs(en, zh)
    assert pairs is not None
    assert len(pairs) == 2
    assert pairs[0].zh and "聚合理论" in pairs[0].zh
    assert pairs[1].zh and "成本" in pairs[1].zh
    assert "Costs come second" in pairs[1].en


def test_pairs_still_none_when_structure_diverges():
    en = "".join(f"<p>Paragraph {index} says something distinct.</p>" for index in range(6))
    zh = "<p>只有两段。</p><p>结构对不上，不该硬配。</p>"
    assert bilingual_pairs(en, zh) is None
