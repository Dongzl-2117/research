"""一段的中文又写进了相近一段、英文却对不上。

整体错开一格：英文不同的两段中文一字不差（11-11 21938 引的 Andreessen 那几句），多半是同一次回答里
一段的中文没了，后面几段各拿了下一段的中文，到某一段又对上、和前一段写成一样。多带了一段：一段的中文
前面或后面多写了相邻一段的整段中文，相邻那段自己的中文也在（11-14 线上对照页里 22537、22999 等十来篇）。
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
# 相邻的段说的是不同的事：英文只差一个词的两段会被当成同一段话（provenance.SAME_WORDS）。
TOPICS = [
    "the company raised prices last year and lost almost none of its customers",
    "hiring slowed in the spring because the new office was not ready in time",
    "support tickets piled up after the redesign, mostly from people who could not find settings",
    "shipping costs fell once the warehouse moved closer to the main port",
    "most of the marketing budget went to podcasts rather than search ads",
    "the design team rebuilt the onboarding flow from scratch over the summer",
    "a security audit found old servers that nobody remembered owning",
    "finance moved to quicker closes so the board could see trends sooner",
    "lawyers spent weeks on a contract dispute that ended in a quiet settlement",
    "researchers published a paper on caching that competitors quickly copied",
    "sales reps were told to stop discounting at the end of every quarter",
    "operations automated the nightly reports that used to take a whole team",
    "a partnership with a large retailer brought in a wave of new users",
    "the education program trained teachers in many countries",
    "community moderators asked for better tools to handle spam during launches",
    "hardware sales were small but kept the brand visible in physical stores",
]
LONG = [f"<p>Paragraph {word}: {topic}.</p>" for word, topic in zip(WORDS, TOPICS, strict=True)]
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
    # 口语里同一句又说一遍、只换了一个词（love / like）：英文九成的词对得上，中文一样是对的。
    disciplined = "<p>我们喜欢这个产品，但我们会保持克制。</p>"
    assert pr.copied_targets(["<p>We love the product. But we are going to be disciplined about it.</p>",
                              "<p>We like the product, but we are going to be disciplined about it.</p>"],
                             [disciplined, disciplined]) == []
    # 句式一样、换了要紧的词的两句（Andreessen 那几句）不算同一句：中文一样还是错开了。
    market = "<p>当优秀的团队遇上糟糕的市场，市场赢。</p>"
    parallel = ["<p>When a great team meets a lousy market, market wins.</p>",
                "<p>When a lousy team meets a great market, market wins.</p>"]
    assert pr.copied_targets(parallel, [market, market]) == [0, 1]
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


def test_a_neighbours_whole_chinese_written_into_a_segment_counts_too():
    # 21253 那样：一段的中文后面多写了下一段的整段中文，下一段自己的中文也在。
    assert pr.copied_targets([T1, T2, T3], [Y1[:-4] + Y2[3:], Y2, Y3]) == [0, 1]
    # 写在前面也一样（22537：后一段的中文开头先写了一遍前一段）。
    assert pr.copied_targets([T1, T2, T3], [Y1, Y2, Y2[:-4] + Y3[3:]]) == [1, 2]
    # 作者自己把前面那句又引了一遍：长的那段英文也包含短的那段，中文包含就对了。
    quoted = "<p>We built the platform because nobody else would sell us the software we needed.</p>"
    later = ("<p>As I said: we built the platform because nobody else would sell us the software we needed."
             " It paid off.</p>")
    said = "<p>我们自己做平台，是因为没人肯卖给我们需要的软件。</p>"
    later_zh = "<p>就像我说的：我们自己做平台，是因为没人肯卖给我们需要的软件。这招见效了。</p>"
    assert pr.copied_targets([quoted, later], [said, later_zh]) == []
    # 又引一遍时换了一个词（Such / The）：短的那段英文九成以上的词按顺序在长的那段里，也算引了两遍。
    filing = ("<p>In April the fund bought a small stake. Such minority holder is not a party to the agreements"
              " that are currently in effect among the operating companies and their owners.</p>")
    again = ("<p>The minority holder is not a party to the agreements that are currently in effect among the"
             " operating companies and their owners.</p>")
    holder = "该小股东不是各运营公司与其股东之间现行协议的当事方。"
    stake = f"<p>四月，这家基金买了一小笔股份。{holder}</p>"
    assert pr.copied_targets([filing, again], [stake, f"<p>{holder}</p>"]) == []
    # 被包含的那段不到 16 个汉字的不算：小标题、术语、插话、对话里重说一遍的短语本来就常出现在别的段里。
    heading = "<h3>The Chicken-and-Egg Problem</h3>"
    heading_zh = "<h3>先有鸡还是先有蛋</h3>"
    body = "<p>The reality is that platforms are not chicken-and-egg problems: users come first.</p>"
    body_zh = "<p>现实是，平台并不是先有鸡还是先有蛋的问题：用户先来。</p>"
    assert pr.copied_targets([heading, body], [heading_zh, body_zh]) == []
    aside = "<p>In the way Apple does.</p>"
    reply = "<p>Yeah, in the way that Apple does, I think we will hear a lot about this next week.</p>"
    assert pr.copied_targets([aside, reply], ["<p>以苹果一贯的方式。</p>",
                                              "<p>对，以苹果一贯的方式。我觉得下周会听到很多这方面的消息。</p>"]) == []
    question = "<p>Fast delivery is now a basic requirement.</p>"
    answer = "<p>Exactly, it is now a basic requirement for every store, and payments are next.</p>"
    assert pr.copied_targets([question, answer], ["<p>快速送达已经成了基本要求。</p>",
                                                  "<p>没错，快速送达已经成了基本要求。下一个是支付。</p>"]) == []
    # 导语有一整句（16 个汉字以上），下一段开头又写了一遍：算（22999、22530 那样）。
    lead = "<p>下面我尽量用三个要点把这套新系统讲清楚：</p>"
    body_zh = "<p>每笔订单都走同一个队列，流量暴涨时也不会丢。</p>"
    assert pr.copied_targets(
        ["<p>Here is my best attempt to explain the new system in three bullet points:</p>",
         "<p>Every order goes through the same queue, so nothing gets lost when traffic spikes.</p>"],
        [lead, lead[:-4] + body_zh[3:]]) == [0, 1]


def test_a_batch_where_one_segment_swallowed_its_neighbour_is_asked_again_in_halves(tmp_path):
    def answer(segs, _repair):
        # 四段一起送来时第一段把第二段的中文也写了；分开送就各译各的。
        if segs == [T1, T2, T3, T4]:
            return [Y1[:-4] + Y2[3:], Y2, Y3, Y4]
        return [TEAM[seg] for seg in segs]

    conn, result, calls = run(T1 + T2 + T3 + T4, answer, tmp_path)
    assert [segs for segs, _repair in calls] == [[T1, T2, T3, T4], [T1, T2], [T3, T4]]
    stored = conn.writes[-1]
    assert result.status == "translated" and stored[3] == Y1 + Y2 + Y3 + Y4
    assert pr.copied_slots(stored[7].obj) == []
    # 以前这样存下来的回执，盘点照样数得出来。
    swallowed = pr.receipt(pr.source_manifest(T1 + T2 + T3 + T4), Y1[:-4] + Y2[3:] + Y2 + Y3 + Y4)
    assert pr.copied_slots(swallowed) == [0, 1]


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
