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
    normalize_units,
    paragraphs,
    parse_reply,
    stitch,
)


def _msg(text: str, speaker: str = "", *, me: bool = False, cut: str = "", quote: dict | None = None) -> dict:
    block = {"type": "message", "speaker": speaker, "self": me, "text": text}
    if cut:
        block["cut"] = cut
    if quote:
        block["quote"] = quote
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
    marked = _units(_msg("股价不理想，如何提振股价", "王小明"))
    marked["blocks"][0]["marks"] = ["股价不理想", "如何提振股价"]
    assert format_units(marked) == "王小明：股价不理想，如何提振股价\n（图上标出：股价不理想；如何提振股价）"
    assert format_units(units).endswith(
        "管理层回答：先做好业务\n（图上标出：先做好业务；3）\n\n"
        "公司 | 市盈率\n甲 | 16倍\n（图上标出：16倍）\n\n没有标注"
    )


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


def test_paragraphs_share_written_quotes_across_shots():
    written: set[str] = set()
    first = paragraphs(_units(_msg("李录投资也很好啊", "李四"))["blocks"], written)
    quote = {"speaker": "李四", "self": False, "text": "李录投资也很好啊"}
    second = paragraphs(_units(_msg("同意", me=True, quote=quote))["blocks"], written)
    assert first == [("李四", ["李录投资也很好啊"])]
    assert second == [("方伟", ["同意"])]
    assert format_blocks([], written) == ""
