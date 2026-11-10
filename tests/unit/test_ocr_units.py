"""识图交回的结构：校验、排版、连着截的几张怎么拼。不连库、不调模型。"""

from __future__ import annotations

import copy
import json

import pytest

from researchlib.ocr_units import (
    UnitsError,
    format_blocks,
    format_units,
    has_dialogue,
    join_tiles,
    normalize_units,
    paragraphs,
    parse_reply,
    stitch,
)


def _msg(
    text: str,
    speaker: str = "",
    *,
    me: bool = False,
    cut: str = "",
    quote: dict | None = None,
    illegible: bool = False,
) -> dict:
    block = {"type": "message", "speaker": speaker, "self": me, "text": text}
    if cut:
        block["cut"] = cut
    if quote:
        block["quote"] = quote
    if illegible:
        block["illegible"] = True
    return block


def _units(*blocks: dict, kind: str = "chat") -> dict:
    return normalize_units({"kind": kind, "blocks": list(blocks)})


LONG_NAME = "这显然是一整句话而不是一个显示名，模型把别人说的话整句写进了说话人的位置上"


def test_plain_text_reply_is_not_structure():
    """模型没按格式交（自由文字）：返回 None，调用方当旧格式的字存，这次的钱不白花。"""
    assert parse_reply("方伟：底线\n网友：非共识") is None
    assert parse_reply("") is None


def test_reply_in_a_fence_or_after_a_preface_is_read():
    payload = {"kind": "chat", "blocks": [_msg("先看现金流", me=True)]}
    fenced = "```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```"
    prefaced = "结果如下：\n" + json.dumps(payload, ensure_ascii=False)
    assert parse_reply(fenced)["blocks"] == [{"type": "message", "speaker": "", "self": True, "text": "先看现金流"}]
    assert parse_reply(prefaced) == parse_reply(fenced)


def test_raw_newline_inside_a_string_is_accepted():
    raw = (
        '{"kind": "chat", "blocks": [{"type": "message", "speaker": "王小明", "self": false,'
        ' "text": "第一行\n第二行"}]}'
    )
    assert parse_reply(raw)["blocks"][0]["text"] == "第一行\n第二行"


@pytest.mark.parametrize(
    "raw",
    [
        '{"kind": "chat", "blocks": [{"type": "message", "text": "说到一半',
        '{"kind": "chat", "blocks": "不是列表"}',
        '["kind", "blocks"]',
    ],
)
def test_broken_structure_raises_instead_of_storing_half_json(raw):
    with pytest.raises(UnitsError):
        parse_reply(raw)


def test_normalize_cleans_names_flags_and_drops_empty_blocks():
    units = normalize_units(
        {
            "kind": "Chat",
            "blocks": [
                {"type": "message", "speaker": "@王小明：", "self": "true", "text": "  毛利率：35%  "},
                {"type": "message", "speaker": "李四", "self": False, "text": "   "},
                {"type": "message", "speaker": LONG_NAME, "text": "好"},
                {"type": "message", "speaker": "李四", "text": "", "quote": {"speaker": "王五", "text": "原话"}},
                {"type": "table", "rows": [["公司", " 市盈率 "], ["", ""], "不是一行", [16, "倍"]]},
                {"type": "caption", "text": "图注也留着"},
                {"type": "text", "text": "截了一半", "cut": "下"},
                "不是块",
            ],
        }
    )
    assert units["kind"] == "chat"
    assert units["blocks"] == [
        {"type": "message", "speaker": "王小明", "self": True, "text": "毛利率：35%"},
        {"type": "message", "speaker": "", "self": False, "text": "好"},
        {
            "type": "message",
            "speaker": "李四",
            "self": False,
            "text": "",
            "quote": {"speaker": "王五", "self": False, "text": "原话"},
        },
        {"type": "table", "rows": [["公司", "市盈率"], ["16", "倍"]]},
        {"type": "text", "text": "图注也留着"},
        {"type": "text", "text": "截了一半", "cut": "bottom"},
    ]
    assert normalize_units({"kind": "海报", "blocks": []})["kind"] == "other"


def test_a_message_stays_one_paragraph():
    """一条消息里的空行收成换行：后面按空行分段时，一条不会被拆成两个人说的。"""
    units = _units(_msg("第一段\n\n\n第二段", "王小明"))
    assert units["blocks"][0]["text"] == "第一段\n第二段"
    assert format_units(units) == "王小明：第一段\n第二段"


def test_format_labels_the_owner_and_keeps_colons_inside_what_people_said():
    units = _units(
        _msg("毛利率：35%，比去年高", "王小明"),
        _msg("先看现金流", me=True),
        _msg("再看负债", me=True),
        _msg("同意", ""),
        _msg("我也同意", ""),
        _msg("注意：明天出财报", "大头🐟"),
    )
    assert format_units(units) == (
        "王小明：毛利率：35%，比去年高\n\n"
        "方伟：先看现金流\n再看负债\n\n"
        "网友：同意\n\n"
        "网友：我也同意\n\n"
        "大头🐟：注意：明天出财报"
    )


def test_author_accounts_are_the_author_only_when_the_source_is_known():
    """帖子、评论截图没有左右气泡：显示名是来源作者本人账号的（author、poster_names）写成方伟。

    别人帖子里标着作者、楼主的照图上的名字写；名字里带「方伟」的读者也不算。
    """
    units = _units(
        _msg("这季度先看现金流", "方伟Rey"),
        _msg("楼主说得对，毛利率还会降", "王小明"),
        _msg("我也这么看", "方伟铁粉"),
    )
    assert format_blocks(units["blocks"], owners=("方伟", "方伟Rey")) == (
        "方伟：这季度先看现金流\n\n王小明：楼主说得对，毛利率还会降\n\n方伟铁粉：我也这么看"
    )
    # 单张图存 image_texts.text 时不知道来源：照图上的名字写。
    assert format_units(units).startswith("方伟Rey：这季度先看现金流")
    # 别的来源按它自己的名字认，之后由 relabel_host 换成作者名。
    other = _units(_msg("先看现金流", "老船长"), _msg("同意", "王小明"))
    assert format_blocks(other["blocks"], owners=("乘风破浪", "老船长")) == "方伟：先看现金流\n\n王小明：同意"


def test_quote_comes_before_the_reply_and_is_written_once():
    quote = {"speaker": "李四", "self": False, "text": "李录投资也很好啊"}
    units = _units(
        _msg("同意，长期看现金流", me=True, quote=quote),
        _msg("我说的不是这个", "李四", quote={"speaker": "", "self": True, "text": "同意，长期看现金流"}),
    )
    assert format_units(units) == "李四：李录投资也很好啊\n\n方伟：同意，长期看现金流\n\n李四：我说的不是这个"


def test_tables_and_paragraphs_are_written_as_they_are():
    units = _units(
        {"type": "text", "text": "2025 年估值"},
        {"type": "table", "rows": [["公司", "市盈率"], ["甲", "16倍"]]},
        kind="table",
    )
    assert format_units(units) == "2025 年估值\n\n公司 | 市盈率\n甲 | 16倍"
    assert not has_dialogue(units)
    assert has_dialogue(_units(_msg("好", "王小明")))
    assert format_units(_units(kind="photo")) == ""


def test_overlapping_screenshots_keep_one_copy():
    first = _units(_msg("瑞幸同店还有压力", "张三"), _msg("古茗店效还行", "李四"), _msg("先看批价", me=True))
    second = _units(_msg("古茗店效还行", "李四"), _msg("先看批价", me=True), _msg("下周再聊", "张三"))
    before = copy.deepcopy([first, second])
    upper, lower = stitch([first, second])
    assert [block["text"] for block in upper] == ["瑞幸同店还有压力", "古茗店效还行", "先看批价"]
    assert [block["text"] for block in lower] == ["下周再聊"]
    assert [first, second] == before


def test_bottom_cut_message_is_replaced_by_the_complete_one():
    first = _units(_msg("茅台批价这一周没动", "张三"), _msg("渠道仍在观", "李四", cut="bottom"))
    second = _units(_msg("渠道仍在观望，短期不宜提价", "李四"), _msg("先看到这里", me=True))
    upper, lower = stitch([first, second])
    assert upper[-1]["text"] == "渠道仍在观望，短期不宜提价"
    assert "cut" not in upper[-1]
    assert [block["text"] for block in lower] == ["先看到这里"]


def test_cut_edge_tolerates_a_couple_of_misread_characters():
    first = _units(_msg("茅台批价这一周没动", "张三"), _msg("渠道仍在观望短期不宜捉什", "李四", cut="bottom"))
    second = _units(_msg("渠道仍在观望短期不宜提价，等中秋", "李四"))
    upper, lower = stitch([first, second])
    assert upper[-1]["text"] == "渠道仍在观望短期不宜提价，等中秋"
    assert lower == []


def test_top_cut_message_is_dropped_when_the_earlier_shot_has_it_whole():
    first = _units(_msg("茅台批价这一周没动，渠道仍在观望", "张三"))
    second = _units(_msg("渠道仍在观望", "张三", cut="top"), _msg("短期不宜提价", "李四"))
    upper, lower = stitch([first, second])
    assert upper[0]["text"] == "茅台批价这一周没动，渠道仍在观望"
    assert [block["text"] for block in lower] == ["短期不宜提价"]


def test_message_split_across_two_shots_is_spliced():
    first = _units(_msg("先说结论", me=True), _msg("这家公司的现金流一直很稳", "王小明", cut="bottom"))
    second = _units(_msg("现金流一直很稳，负债也低", "王小明", cut="top"), _msg("同意", me=True))
    upper, lower = stitch([first, second])
    assert upper[-1]["text"] == "这家公司的现金流一直很稳，负债也低"
    assert [block["text"] for block in lower] == ["同意"]


def test_garbled_top_cut_message_is_skipped_when_the_rest_lines_up():
    """后一张第一条被标题栏挡住半行、认乱了，和前一张里完整的那条对不上：跳过它，后面几条对得上就是接着截的。

    以前对不上就整张照留，重合的几条在旁路里多写一遍（11-08 对比里一篇群聊重复了 4 条）。
    """
    first = _units(
        _msg("毛利率35%，比去年高，主要靠提价", "王小明"),
        _msg("先看现金流", me=True),
        _msg("经营现金流连续三年为正", me=True),
    )
    second = _units(
        _msg("比云年髙，主要靠提介", cut="top"),
        _msg("先看现金流", me=True),
        _msg("经营现金流连续三年为正", me=True),
        _msg("那估值呢", "李四"),
    )
    upper, lower = stitch([first, second])
    assert [block["text"] for block in upper] == [
        "毛利率35%，比去年高，主要靠提价",
        "先看现金流",
        "经营现金流连续三年为正",
    ]
    assert [block["text"] for block in lower] == ["那估值呢"]


def test_garbled_bottom_cut_message_gives_way_to_the_complete_one_when_the_rest_lines_up():
    first = _units(
        _msg("今年收入增长两成，主要靠新店", "王小明"),
        _msg("利润呢", me=True),
        _msg("利润增速慢于收八，因为原料涨价", "李四", cut="bottom"),
    )
    second = _units(
        _msg("今年收入增长两成，主要靠新店", "王小明"),
        _msg("利润呢", me=True),
        _msg("利润增速慢于收入，因为原材料涨价，毛利率掉了三个点", "李四"),
        _msg("那就等明年", me=True),
    )
    upper, lower = stitch([first, second])
    assert [block["text"] for block in upper] == ["今年收入增长两成，主要靠新店", "利润呢"]
    assert [block["text"] for block in lower] == ["利润增速慢于收入，因为原材料涨价，毛利率掉了三个点", "那就等明年"]


def test_garbled_cut_message_from_someone_else_is_kept():
    """跳过的那条得和前一张里紧挨着的那条是同一个人说的；不是就照旧都留着，宁可重复，不丢字。"""
    first = _units(_msg("毛利率35%，比去年高，主要靠提价", "王小明"), _msg("先看现金流，再看负债", me=True))
    second = _units(_msg("比云年髙，主要靠提介", "李四", cut="top"), _msg("先看现金流，再看负债", me=True))
    _upper, lower = stitch([first, second])
    assert [block["text"] for block in lower] == ["比云年髙，主要靠提介", "先看现金流，再看负债"]


def test_marks_are_kept_and_written_after_the_block():
    """截图后有人另外圈出、划线标出的字：一处一项存在 marks，排版时写在那一块后面一行。"""
    units = _units(
        _msg("股东会上问了股价不理想怎么办", "王小明"),
        {"type": "text", "text": "管理层回答：先做好业务", "marks": ["  先做好业务 ", "", "先做好业务", 3]},
        {"type": "table", "rows": [["公司", "市盈率"], ["甲", "16倍"]], "marks": "16倍"},
        {"type": "text", "text": "没有标注", "marks": {"text": "不是列表"}},
    )
    assert units["blocks"][1]["marks"] == ["先做好业务", "3"]
    assert units["blocks"][2]["marks"] == ["16倍"]
    assert "marks" not in units["blocks"][3]
    marked = _units(_msg("股东会上有人问：股价不理想，如何提振股价", "王小明"))
    marked["blocks"][0]["marks"] = ["股价不理想", "如何提振股价"]
    assert format_units(marked) == (
        "王小明：股东会上有人问：股价不理想，如何提振股价\n（图上标出：股价不理想；如何提振股价）"
    )
    assert format_units(units).endswith(
        "管理层回答：先做好业务\n（图上标出：先做好业务；3）\n\n"
        "公司 | 市盈率\n甲 | 16倍\n（图上标出：16倍）\n\n没有标注"
    )


def test_marks_covering_the_whole_block_are_not_copied_again():
    """整段、整条都被圈出来时不再把这段抄一遍，只说这段在图上标出了。几处标注连起来正好是整块也算。"""
    units = _units(
        {"type": "text", "text": "·过去几年，公司股价表现不理想", "marks": ["过去几年，公司股价表现不理想"]},
        _msg("你是没玩过这款相机，拼接要后期", "李四"),
        {
            "type": "text",
            "text": "预计明天 15:00 送达\n门店 | 闪购: 10.00",
            "marks": ["预计明天 15:00 送达", "门店 | 闪购: 10.00"],
        },
        {"type": "text", "text": "1.角色与场景\n2.充分的调研", "marks": ["2.充分的调研"]},
    )
    units["blocks"][1]["marks"] = ["你是没玩过这款相机，拼接要后期"]
    assert format_units(units) == (
        "·过去几年，公司股价表现不理想\n（图上标出这段）\n\n"
        "李四：你是没玩过这款相机，拼接要后期\n（图上标出这条）\n\n"
        "预计明天 15:00 送达\n门店 | 闪购: 10.00\n（图上标出这段）\n\n"
        "1.角色与场景\n2.充分的调研\n（图上标出：2.充分的调研）"
    )


def test_small_text_is_flagged_after_the_block():
    """字明显比正文小的块（聊天里转发的书页、论坛截图）后面写一行「（小字，可能认错）」，有标注时标注排在后面。"""
    units = _units(
        _msg("在招聘时，公司要求每个人都要参加培训", me=True),
        _msg("谁写的", "王小明"),
        {"type": "text", "text": "书页上的一段", "small": "true", "marks": ["书页上的一段"]},
        {"type": "table", "rows": [["年份", "增速"], ["2001", "-41.7"]], "small": 1},
        {"type": "text", "text": "正文", "small": False},
    )
    units["blocks"][0]["small"] = True
    assert units["blocks"][2]["small"] is True and units["blocks"][3]["small"] is True
    assert "small" not in units["blocks"][1] and "small" not in units["blocks"][4]
    assert format_units(units) == (
        "方伟：在招聘时，公司要求每个人都要参加培训\n（小字，可能认错）\n\n"
        "王小明：谁写的\n\n"
        "书页上的一段\n（小字，可能认错）\n（图上标出这段）\n\n"
        "年份 | 增速\n2001 | -41.7\n（小字，可能认错）\n\n"
        "正文"
    )


def test_quote_cut_short_with_an_ellipsis_is_not_written_again():
    """引用框常把原话截成前半句加省略号：前面已经有整句，就不再写这半句。原话不在图上、或者半句太短，照写。"""
    whole = "对，好东西便宜，在外面很难快速找到替代，别家的好东西不便宜，外面遍地都是替代"
    cut = {"speaker": "李四", "self": False, "text": whole[:31] + "..."}
    units = _units(_msg(whole, "李四"), _msg("合作是非常紧密的", me=True, quote=cut))
    assert format_units(units) == f"李四：{whole}\n\n方伟：合作是非常紧密的"
    alone = _units(_msg("合作是非常紧密的", me=True, quote=cut))
    assert format_units(alone) == f"李四：{cut['text']}\n\n方伟：合作是非常紧密的"
    short = {"speaker": "李四", "self": False, "text": "好的..."}
    units = _units(_msg("好的明天见", "李四"), _msg("收到", me=True, quote=short))
    assert format_units(units) == "李四：好的明天见\n好的...\n\n方伟：收到"


def test_short_coincidence_is_not_an_overlap():
    """只重合一句「好的」不算接着截：两张图里各自的「好的」都留着。"""
    first = _units(_msg("看完了", "王小明"), _msg("好的", me=True))
    second = _units(_msg("好的", me=True), _msg("明天见", "王小明"))
    _upper, lower = stitch([first, second])
    assert [block["text"] for block in lower] == ["好的", "明天见"]


def test_different_speakers_are_not_merged_even_with_the_same_words():
    first = _units(_msg("这季度毛利率有改善", "张三"))
    second = _units(_msg("这季度毛利率有改善", "李四"))
    _upper, lower = stitch([first, second])
    assert [block["speaker"] for block in lower] == ["李四"]


def test_only_neighbouring_structured_shots_are_stitched():
    chat = _units(_msg("瑞幸同店还有压力，先看到这里", "张三"))
    again = _units(_msg("瑞幸同店还有压力，先看到这里", "张三"))
    assert stitch([chat, None, again])[2] == again["blocks"]
    assert stitch([chat, _units(kind="photo"), again])[2] == again["blocks"]


def test_a_shot_that_only_repeats_the_last_one_lets_the_next_compare_with_it():
    first = _units(_msg("瑞幸同店还有压力", "张三"), _msg("古茗店效还行，折扣不能再深了", "李四"))
    repeat = _units(_msg("瑞幸同店还有压力", "张三"), _msg("古茗店效还行，折扣不能再深了", "李四"))
    third = _units(_msg("古茗店效还行，折扣不能再深了", "李四"), _msg("下周再聊", "张三"))
    _first, middle, last = stitch([first, repeat, third])
    assert middle == []
    assert [block["text"] for block in last] == ["下周再聊"]


# 88898c：气泡延伸到图外，看得见的字是完整的。第 3 版和这次都留全文，不因为 cut 丢掉日期。
NETFLIX_UNITS = {
    "v": 1,
    "kind": "chat",
    "blocks": [
        {
            "type": "message",
            "speaker": "豆包",
            "self": False,
            "text": (
                "1 | Netflix 美国十年价格线 (对标【Standard无广告 | Premium】)\n"
                "• 2016: Standard $9.99; Premium $11.99 (4K)\n"
                "• 2017-10: Standard $10.99; Premium $13.99\n"
                "• 2019-01: Standard $12.99; Premium $15.99\n"
                "• 2020-10: Standard $13.99; Premium $17.99\n"
                "• 2023-10: Standard $15.49; Premium $19.99 (新增广告档$6.99)\n"
                "• 2025-01: Standard $17.99; Premium $24.99; 广告$7.99\n"
                "• 2026-03: Standard $19.99; Premium $26.99; 广告$8.99\n"
                "十年涨幅 (2016 Standard $9.99 → 2026 $19.99): +100%\n"
                "Premium: $11.99 → $26.99, +125%, 大幅跑赢通胀 (40%)\n"
                "2 | HBO (2016=HBO Now; 2020后=HBO Max / Max)\n"
                "• 2016 (HBO Now, 唯一一档): $14.99/月 (无分层、无广告版)"
            ),
            "cut": "bottom",
        }
    ],
}


def test_legible_cut_block_keeps_every_visible_line_and_date():
    """cut 只表示图外还有下文。看得清的价格和日期整段留下，不换成「无法辨认」。"""
    text = format_units(NETFLIX_UNITS)
    assert "2016" in text and "2017-10" in text and "2019-01" in text and "2026-03" in text
    assert "$9.99" in text and "$19.99" in text and "$14.99" in text
    assert "豆包：" in text
    assert "无法辨认" not in text
    # 没有 illegible 字段的旧第 3 版行，排版和改之前一样。
    assert format_units(normalize_units(NETFLIX_UNITS)) == text


def test_clipped_glyphs_are_not_reconstructed_into_a_sentence():
    """字只露出上半截、模型补成整句：必须另标 illegible，排版才丢掉那句。单靠 cut 不丢。"""
    kept = format_units(_units(_msg("去年年费2万块钱就是亏大了", cut="bottom")))
    assert "去年年费2万块钱就是亏大了" in kept
    assert "无法辨认" not in kept
    dropped = format_units(_units(_msg("去年年费2万块钱就是亏大了", cut="bottom", illegible=True)))
    assert "去年年费" not in dropped
    assert "亏大了" not in dropped
    assert "网友" not in dropped
    assert "方伟" not in dropped
    assert dropped == "（末尾一条被遮挡，无法辨认）"
    parsed = normalize_units({
        "kind": "chat",
        "blocks": [{
            "type": "message",
            "text": "补出来的一句",
            "cut": "bottom",
            "partial_glyphs": True,
        }],
    })
    assert parsed["blocks"][0]["illegible"] is True
    assert "补出来的一句" not in format_units(parsed)


def test_a_visible_quote_on_an_illegible_message_is_kept():
    """看不全的是这一条自己的字；引用框里看得到的原话仍写出来。"""
    quote = {"speaker": "王小明", "self": False, "text": "毛利率35%，比去年高"}
    text = format_units(_units(_msg("去年年费2万块钱就是亏大了", cut="bottom", illegible=True, quote=quote)))
    assert text == "王小明：毛利率35%，比去年高\n\n（末尾一条被遮挡，无法辨认）"
    assert "亏大了" not in text


def test_a_cut_line_completed_by_the_next_shot_keeps_that_sentence():
    """邻图补全以后 cut 会拿掉，完整的那句留下，不再换成「无法辨认」。"""
    first = _units(_msg("茅台批价这一周没动", "张三"), _msg("渠道仍在观", "李四", cut="bottom"))
    second = _units(_msg("渠道仍在观望，短期不宜提价", "李四"), _msg("先看到这里", me=True))
    upper, lower = stitch([first, second])
    text = format_blocks(upper + lower)
    assert "渠道仍在观望，短期不宜提价" in text
    assert "无法辨认" not in text


def test_paragraphs_share_written_quotes_across_shots():
    written: set[str] = set()
    first = paragraphs(_units(_msg("李录投资也很好啊", "李四"))["blocks"], written)
    quote = {"speaker": "李四", "self": False, "text": "李录投资也很好啊"}
    second = paragraphs(_units(_msg("同意", me=True, quote=quote))["blocks"], written)
    assert first == [("李四", ["李录投资也很好啊"])]
    assert second == [("方伟", ["同意"])]
    assert format_blocks([], written) == ""


def _para(text: str, *, cut: str = "") -> dict:
    block = {"type": "text", "text": text}
    if cut:
        block["cut"] = cut
    return block


def test_tiles_of_one_long_shot_keep_one_copy_of_the_overlap():
    """长截图切成的几段：重叠那一截里完整的几条两段都有，只留一份。"""
    first = _units(_msg("瑞幸同店还有压力", "张三"), _msg("古茗店效还行", "李四"), _msg("先看批价", me=True))
    second = _units(_msg("古茗店效还行", "李四"), _msg("先看批价", me=True), _msg("下周再聊", "张三"))
    third = _units(_msg("下周再聊", "张三"), _msg("好的，周一见", me=True))
    before = copy.deepcopy([first, second, third])
    joined = join_tiles([first, second, third])
    assert [block["text"] for block in joined["blocks"]] == [
        "瑞幸同店还有压力",
        "古茗店效还行",
        "先看批价",
        "下周再聊",
        "好的，周一见",
    ]
    assert joined["tiles"] == 3 and joined["kind"] == "chat"
    assert [first, second, third] == before


def test_paragraph_running_across_a_seam_is_joined_even_without_cut_flags():
    """切口落在行与行之间，模型看到的是完整的几行，没标 cut；接缝两边重合的几行照样接成一段。"""
    first = _units(_para("一、收入"), _para("今年收入增长两成，主要靠新店。毛利率三十五，比去年高"), kind="text")
    second = _units(_para("毛利率三十五，比去年高，主要靠提价。"), _para("二、现金流"), kind="text")
    joined = join_tiles([first, second])
    assert [block["text"] for block in joined["blocks"]] == [
        "一、收入",
        "今年收入增长两成，主要靠新店。毛利率三十五，比去年高，主要靠提价。",
        "二、现金流",
    ]
    assert all("cut" not in block for block in joined["blocks"])


def test_paragraph_cut_short_at_the_seam_gives_way_to_the_whole_one():
    """前一段最后一条只露到接缝（没标 cut），后一段里是完整的：留完整的那条。"""
    first = _units(_msg("茅台批价这一周没动", "张三"), _msg("渠道仍在观望，短期", "李四"))
    second = _units(
        _msg("茅台批价这一周没动", "张三"), _msg("渠道仍在观望，短期不宜提价", "李四"), _msg("先看到这里", me=True)
    )
    joined = join_tiles([first, second])
    assert [block["text"] for block in joined["blocks"]] == [
        "茅台批价这一周没动",
        "渠道仍在观望，短期不宜提价",
        "先看到这里",
    ]


def test_only_cuts_at_the_real_edges_of_the_shot_are_kept():
    """接缝处的 cut 是我们切出来的，接完去掉；整张图顶上、底下真截到的留着，和下一张图拼接时要用。"""
    first = _units(_msg("渠道仍在观望", "张三", cut="top"), _msg("先看批价", me=True, cut="bottom"))
    second = _units(_msg("下周再聊", "张三", cut="top"), _msg("好的，周一", me=True, cut="bottom"))
    blocks = join_tiles([first, second])["blocks"]
    assert [(block["text"], block.get("cut", "")) for block in blocks] == [
        ("渠道仍在观望", "top"),
        ("先看批价", ""),
        ("下周再聊", ""),
        ("好的，周一", "bottom"),
    ]


def test_slivers_cut_by_a_seam_are_dropped_and_only_the_real_edge_says_unreadable():
    """切口压着字时，接缝两边各有一条只露出半截笔画的（illegible）：另一段里那行整行都在，去掉它，
    不写「被遮挡，无法辨认」。整张图顶上真截到的那条照旧写成被遮挡。"""
    first = _units(
        _msg("这周批价", "张三", cut="top", illegible=True),
        _msg("瑞幸同店还有压力，门店太密", "张三"),
        _msg("古茗店效还行，加盟商也在扩", "李四"),
        _msg("先看批", "李四", cut="bottom", illegible=True),
    )
    second = _units(
        _msg("瑞幸同店", "张三", cut="top", illegible=True),
        _msg("古茗店效还行，加盟商也在扩", "李四"),
        _msg("先看批价，下周再聊", "李四"),
    )
    joined = join_tiles([first, second])
    assert [(block["text"], block.get("cut", ""), bool(block.get("illegible"))) for block in joined["blocks"]] == [
        ("这周批价", "top", True),
        ("瑞幸同店还有压力，门店太密", "", False),
        ("古茗店效还行，加盟商也在扩", "", False),
        ("先看批价，下周再聊", "", False),
    ]
    text = format_units(joined)
    assert text.count("无法辨认") == 1 and "开头一条被遮挡" in text
    assert "先看批价，下周再聊" in text and "瑞幸同店还有压力，门店太密" in text


def test_a_table_split_by_a_seam_becomes_one_table():
    head = {"type": "table", "rows": [["年份", "收入"], ["2023", "100亿"], ["2024", "120亿"]]}
    tail = {"type": "table", "rows": [["2024", "120亿"], ["2025", "150亿"]], "marks": ["150亿"]}
    joined = join_tiles([_units(head, kind="table"), _units(tail, kind="table")])
    assert joined["blocks"] == [
        {
            "type": "table",
            "rows": [["年份", "收入"], ["2023", "100亿"], ["2024", "120亿"], ["2025", "150亿"]],
            "marks": ["150亿"],
        }
    ]


def test_a_tile_without_text_breaks_the_join_and_nothing_is_lost():
    """中间一段只有图（照片、图表没字），前后两段不挨着，各自的字都留着。"""
    first = _units(_msg("先看这张图", "张三"))
    photo = _units(kind="photo")
    third = _units(_msg("先看这张图", "张三"), _msg("看完了", me=True))
    joined = join_tiles([first, photo, third])
    assert [block["text"] for block in joined["blocks"]] == ["先看这张图", "先看这张图", "看完了"]
    assert joined["kind"] == "chat"


def test_different_words_on_both_sides_of_a_seam_are_not_merged():
    first = _units(_para("利润增速慢于收入，因为原材料涨价"), kind="text")
    second = _units(_para("经营现金流连续三年为正"), kind="text")
    joined = join_tiles([first, second])
    assert [block["text"] for block in joined["blocks"]] == [
        "利润增速慢于收入，因为原材料涨价",
        "经营现金流连续三年为正",
    ]
