"""译文整体错开一格：英文不同的两段中文一字不差（11-11 21938 引的 Andreessen 那几句）。

多半是同一次回答里一段的中文没了，后面几段各拿了下一段的中文，到某一段又对上、和前一段写成一样。
合成样本，不调真模型、不连库。模型替身按送来的段回译文（见 test_translate_number_review.run）。
"""

from __future__ import annotations

from unittest.mock import patch

from test_translate_number_review import run

from researchlib.translate import provenance as pr
from researchlib.translate import service

T1 = "<p>When a great team meets a lousy market, the market wins and the team goes home early.</p>"
T2 = "<p>When a lousy team meets a great market, the market still wins and carries the team along.</p>"
T3 = "<p>When a great team meets a great market, something special happens and everyone can see it.</p>"
T4 = "<p>You can obviously screw up a great market, but a good market will usually lift a decent team.</p>"
Y1 = "<p>当优秀的团队遇上糟糕的市场，市场赢，团队早早回家。</p>"
Y2 = "<p>当平庸的团队遇上巨大的市场，还是市场赢，团队被市场带着走。</p>"
Y3 = "<p>当优秀的团队遇上巨大的市场，就会有特别的事情发生，谁都看得见。</p>"
Y4 = "<p>巨大的市场当然也可能被搞砸，但好市场通常会把一支还行的团队托起来。</p>"
TEAM = {T1: Y1, T2: Y2, T3: Y3, T4: Y4}

WORDS = "one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen".split()
ZH_WORDS = "一 二 三 四 五 六 七 八 九 十 十一 十二 十三 十四 十五 十六".split()
LONG = [f"<p>Paragraph {word} explains why the team kept working on the product for another long year.</p>"
        for word in WORDS]
LONG_ZH = [f"<p>第{word}段讲团队为什么又在这个产品上坚持做了一整年。</p>" for word in ZH_WORDS]
# 英文文章里原文就是中文的一段：回执里有这一段，可它不用译，分批不算它。
CHINESE = "<p>这一段引文原文就是中文，不用译。</p>"


def test_only_different_english_with_the_same_chinese_counts_as_copied():
    # 21938 那样：第一段的中文没了，后面每段拿下一段的中文，最后两段写成一样。
    assert pr.copied_targets([T1, T2, T3, T4], [Y2, Y3, Y4, Y4]) == [2, 3]
    assert pr.copied_targets([T1, T2, T3, T4], [Y1, Y2, Y3, Y4]) == []
    # 整段比，去掉标签和空白：一份带强调、多一个空格也算一样。
    styled = "<p><em>当优秀的团队</em>遇上巨大的市场， 就会有特别的事情发生，谁都看得见。</p>"
    assert pr.copied_targets([T3, T4], [Y3, styled]) == [0, 1]
    # 中文太短的不算：「是的。」本来就常一样。
    assert pr.copied_targets(["<p>Yes.</p>", "<p>Yeah, yes.</p>"], ["<p>是的。</p>", "<p>是的。</p>"]) == []
    # 同一段话又引一遍、前面多了说话人：一段英文包含另一段，不算。
    quoted = "<p>We built the platform because nobody else would sell us the software we needed.</p>"
    again = "<p>Jane Doe: We built the platform because nobody else would sell us the software we needed.</p>"
    said = "<p>我们自己做平台，是因为没人肯卖给我们需要的软件。</p>"
    assert pr.copied_targets([quoted, again], [said, said]) == []
    # 英文一样（导出写了两遍）中文当然一样；目录里的小标题和正文里的同一个，只差大小写、连字符，也一样。
    assert pr.copied_targets([T1, T1], [Y1, Y1]) == []
    heading = "<p>单一城市内的共享出行</p>"
    assert pr.copied_targets(["<p>Ride-sharing in a single city</p>", "<h3>Ride Sharing in a Single City</h3>"],
                             [heading, f"<h3>{heading[3:-4]}</h3>"]) == []
    # 一次回答最多 COPY_SPAN 段：隔 11 段的还比，隔 12 段的不比。
    filler = [f"<p>Filler paragraph {word} is about something else entirely and has nothing to add.</p>"
              for word in WORDS[:11]]
    filler_zh = [f"<p>第{word}段说的是完全不相干的另一件事情。</p>" for word in ZH_WORDS[:11]]
    assert pr.copied_targets([T1, *filler[:10], T2], [Y1, *filler_zh[:10], Y1]) == [0, 11]
    assert pr.copied_targets([T1, *filler, T2], [Y1, *filler_zh, Y1]) == []


def test_copy_span_is_one_request():
    assert pr.COPY_SPAN == service.TRANSLATE_BATCH_MAX_SEGMENTS


def test_a_batch_whose_chinese_is_shifted_is_asked_again_in_halves(tmp_path):
    def answer(segs, _repair):
        # 四段一起送来时错开一格；分开送就各译各的。
        if segs == [T1, T2, T3, T4]:
            return [Y2, Y3, Y4, Y4]
        return [TEAM[seg] for seg in segs]

    conn, result, calls = run(T1 + T2 + T3 + T4, answer, tmp_path)
    assert [segs for segs, _repair in calls] == [[T1, T2, T3, T4], [T1, T2], [T3, T4]]
    stored = conn.writes[-1]
    assert result.status == "translated" and stored[3] == Y1 + Y2 + Y3 + Y4
    assert pr.copied_slots(stored[7].obj) == []


def test_copied_segments_and_a_request_around_them_are_not_reused():
    body = "".join(LONG[:6]) + CHINESE + "".join(LONG[6:])
    # 第 14 段拿了第 15 段的中文，和第 15 段一样。第 6、7 段之间夹着一段原文就是中文的。
    shifted = [*LONG_ZH[:13], LONG_ZH[14], LONG_ZH[14], LONG_ZH[15]]
    proof = pr.receipt(pr.source_manifest(body), "".join(shifted[:6]) + CHINESE + "".join(shifted[6:]))
    assert pr.copied_slots(proof) == [14, 15]
    known = pr.reusable_targets(proof)
    # 从哪段开始错开看不出来：两段前后各 11 段要译的段都不复用（中文那段不算），只剩前两段。
    assert [pr.reuse_key(seg) in known for seg in LONG] == [True, True] + [False] * 14

    # 交回英文的段也按要译的段数前后各一段：中间夹着中文那段，前一段照样不复用。
    merged = LONG_ZH[0][:-4] + LONG_ZH[1][3:]
    echo = pr.receipt(pr.source_manifest(LONG[0] + CHINESE + LONG[1] + LONG[2] + LONG[3]),
                      merged + CHINESE + LONG[1] + LONG_ZH[2] + LONG_ZH[3])
    assert pr.echoed_slots(echo) == [2]
    known = pr.reusable_targets(echo)
    assert [pr.reuse_key(seg) in known for seg in LONG[:4]] == [False, False, False, True]


def test_rerunning_retranslates_only_the_stretch_around_copied_segments(tmp_path):
    body = "".join(LONG)
    zh = dict(zip(LONG, LONG_ZH, strict=True))

    def shifted(segs, _repair):
        # 第二批（第 13 到 16 段）里第 14 段拿了第 15 段的中文。
        return [zh[LONG[14]] if seg == LONG[13] else zh[seg] for seg in segs]

    # 以前的核对不拦这种：这样存下来的译文页面照样逐段对得上。
    with patch.object(service, "copied_targets", return_value=[]):
        conn, _first, _calls = run(body, shifted, tmp_path)
    stored = conn.writes[-1]
    assert pr.copied_slots(stored[7].obj) == [13, 14]

    conn.previous = {"content_hash": stored[1], "title_zh": stored[2], "body_html": stored[3],
                     "complete": stored[5], "provenance": stored[7].obj}
    calls: list = []
    conn, again, _calls = run(body, lambda segs, _repair: [zh[seg] for seg in segs], tmp_path, conn=conn, calls=calls)
    # 第 3 到 16 段重译（照常 12 段一批），前两段复用。
    assert [segs for segs, _repair in calls] == [LONG[2:14], LONG[14:]]
    assert again.reused == 2 and conn.writes[-1][3] == "".join(LONG_ZH)
    assert pr.copied_slots(conn.writes[-1][7].obj) == []
