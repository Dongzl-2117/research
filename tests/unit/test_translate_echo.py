"""交回来的还是英文的段不算译好：多半是模型把这一段的中文一起写进了旁边一段（11-11 全库抽查）。

合成样本，不调真模型、不连库。模型替身按送来的段回译文（见 test_translate_number_review.run）。
"""

from __future__ import annotations

from unittest.mock import patch

from test_translate_number_review import run

from researchlib.translate import provenance as pr
from researchlib.translate import service
from researchlib.translate.display import article_pairs

S1 = "<p>The company rebuilt its app first because loyalty starts with the booking flow that every customer sees.</p>"
S2 = "<p>It then fixed the operating data so that every delay could be traced back to a single cause in the plan.</p>"
S3 = "<p>Only after that did it change how the managers talked about blame in their weekly meetings with the crews.</p>"
S4 = "<p>The result was a culture that cared about fixing problems rather than finding someone to take the fall.</p>"
S5 = "<p>That is why the customer experience improved so quickly once the new habits were in place at every base.</p>"
Z1 = "<p>这家公司先重做了 App，因为忠诚度从每位顾客都会看到的订票流程开始。</p>"
Z2 = "<p>接着它理顺了运营数据，每次延误都能追到计划里的一个原因。</p>"
Z3 = "<p>在那之后，它才改了经理们每周和机组开会时谈责任的方式。</p>"
Z4 = "<p>结果形成了一种重在解决问题、不找人背锅的文化。</p>"
Z5 = "<p>所以新习惯在各个基地落地以后，客户体验改善得很快。</p>"
# 模型把第二段的中文一起写进第一段，第二段原样交回英文。
Z12 = Z1[:-4] + Z2[3:]
ZH = {S1: Z1, S2: Z2, S3: Z3, S4: Z4, S5: Z5}


def _merging(segs, _repair):
    """第一段第二段一起送来时串段：中文全写进第一段，第二段交回英文。分开送就各译各的。"""
    if S1 in segs and S2 in segs:
        return [Z12 if seg == S1 else S2 if seg == S2 else ZH[seg] for seg in segs]
    return [ZH[seg] for seg in segs]


def test_only_whole_english_sentences_handed_back_count_as_untranslated():
    assert pr.untranslated_english(S2, S2)
    # 只把说话人换成中文、后面整段照抄英文，也是没译。
    assert pr.untranslated_english(
        "<p>MA: " + S2[3:], "<p>马克：" + S2[3:],
    )
    assert not pr.untranslated_english(S2, Z2)
    # 人名列表、书名、短标题本来就可以照原样留着。
    names = "<p>Rescale CEO Jane Doe | Databricks CEO John Roe | Canva CEO Mary Major | Scale CEO Richard Miles</p>"
    assert not pr.untranslated_english(names, names)
    assert not pr.untranslated_english("<h4>The Art of the Take</h4>", "<h4>The Art of the Take</h4>")
    title = "<p>A Front Row Seat to the Collision of Technology and Democracy</p>"
    assert not pr.untranslated_english(title, title)


def test_footnotes_that_are_lists_of_source_urls_may_stay_as_they_are():
    # 脚注里一串出处网址：网址里的 the、of、and 不算词，模型照原样留着（或只译了前面几个字）都不算没译。
    graph = (
        "<li>Graph compiled from http://www.example.com/2004/10/12/the_share_of_the_market/, "
        "http://news.example.org/archive/2005/how-the-player-took-the-lead-in-the-us.htm, "
        "http://www.example.net/apps/news?pid=archive&amp;refer=us&amp;sid=a1, "
        "http://example.com/new_players_and_an _upgrade_for_the_phone_users.htm "
        '<a href="#rf16-40">↩</a></li>'
    )
    assert not pr.untranslated_english(graph, graph)
    assert not pr.untranslated_english(graph, graph.replace("Graph compiled from", "图表数据来自"))
    cite = (
        "<li>Doe, Jane, A Brief History of the Player, Example Weekly, "
        "http://www.example.com/index.php/articles/comments/instant-expert-a-brief-history-of-the-player/ "
        '<a href="#rf24-40">↩</a></li>'
    )
    assert not pr.untranslated_english(cite, cite)
    sources = (
        "<li>Sources include: http://www.example.com/reviews_the_phone_vs_the_other_one.html "
        "http://blog.example.org/2007/07/25/the-phone-v-the-other-side-by-side-two-week-comparison/ "
        "https://www.example.net/blogs-op-ed/the-wire/five-reasons-to-wait-on-the-bold.htm</li>"
    )
    assert not pr.untranslated_english(sources, sources)
    # 成句的英文里夹着网址，照原样交回仍是没译。
    with_url = S2[:-5] + " (see http://www.example.com/the-report-of-the-year).</p>"
    assert pr.untranslated_english(with_url, with_url)
    assert not pr.untranslated_english(with_url, Z2)


def test_a_segment_handed_back_in_english_makes_the_batch_be_asked_again_in_halves(tmp_path):
    conn, result, calls = run(S1 + S2 + S3, _merging, tmp_path)
    stored = conn.writes[-1]
    assert result.status == "translated" and result.untranslated == 0
    # 第一次三段一起问，第二段交回英文：整批不信，拆成 [第一段] 和 [第二段、第三段] 重问。
    assert [segs for segs, _repair in calls] == [[S1, S2, S3], [S1], [S2, S3]]
    assert stored[3] == Z1 + Z2 + Z3
    view = article_pairs(S1 + S2 + S3, stored[3], provenance=stored[7].obj)
    assert view.correspondence == "mapped" and all(pair.zh for pair in view.pairs)
    assert pr.echoed_slots(stored[7].obj) == []


def test_a_segment_still_in_english_after_the_retries_is_left_as_a_gap(tmp_path):
    def answer(segs, _repair):
        return [seg if seg == S2 else ZH[seg] for seg in segs]

    conn, result, calls = run(S1 + S2, answer, tmp_path)
    # 拆开以后第二段单独问一次，再带着提示问两次，还是英文：这段先放英文，记进 untranslated，这篇没译完。
    assert [segs for segs, _repair in calls] == [[S1, S2], [S1], [S2]] + [[S2]] * service._SLOT_RETRIES
    assert calls[-1][1] == service._SLOT_NOTES["english"]
    assert result.status == "partial" and result.untranslated == 1
    stored = conn.writes[-1]
    assert stored[5] is False and stored[7].obj["untranslated"] == [1]
    assert stored[3] == Z1 + S2


def test_number_repair_does_not_swap_in_the_english_source(tmp_path):
    source = "<p>The airline flew 300 planes during the summer and it plans to add more of them in the next year.</p>"
    wrong = "<p>这家航空公司夏天飞了200架飞机，明年还打算再加一些。</p>"

    def answer(segs, repair):
        # 第一次数字写错；补译数字时原样交回英文（英文的数字当然对得上）。
        return list(segs) if repair else [wrong]

    conn, result, calls = run(source, answer, tmp_path)
    assert [repair is not None for _segs, repair in calls] == [False, True]
    assert result.status == "translated" and conn.writes[-1][3] == wrong
    assert conn.writes[-1][7].obj["number_review"] == [0]


def test_english_segments_in_a_receipt_and_their_neighbours_are_not_reused():
    manifest = pr.source_manifest(S1 + S2 + S3 + S4 + S5)
    proof = pr.receipt(manifest, Z12 + S2 + Z3 + Z4 + Z5)
    assert pr.echoed_slots(proof) == [1]
    known = pr.reusable_targets(proof)
    assert all(pr.reuse_key(seg) not in known for seg in (S1, S2, S3))
    assert known[pr.reuse_key(S4)] == [Z4] and known[pr.reuse_key(S5)] == [Z5]
    # 先放英文、记在 untranslated 里的段本来就不复用，不算交回英文。
    gap = pr.receipt(manifest, Z1 + S2 + Z3 + Z4 + Z5, untranslated=[1])
    assert pr.echoed_slots(gap) == []


def test_rerunning_retranslates_only_the_english_segment_and_its_neighbours(tmp_path):
    body = S1 + S2 + S3 + S4 + S5
    # 以前的核对没拦交回英文的段：这样存下来的译文，第二段只有英文，中文写在第一段里。
    with patch.object(service, "untranslated_english", return_value=False):
        conn, first, _calls = run(body, _merging, tmp_path)
    old = conn.writes[-1]
    assert old[3] == Z12 + S2 + Z3 + Z4 + Z5 and old[5] is True
    assert pr.echoed_slots(old[7].obj) == [1]

    stored = conn.writes[-1]
    conn.previous = {"content_hash": stored[1], "title_zh": stored[2], "body_html": stored[3],
                     "complete": stored[5], "provenance": stored[7].obj}
    calls: list = []
    conn, again, _calls = run(body, lambda segs, _repair: [ZH[seg] for seg in segs], tmp_path, conn=conn, calls=calls)
    # 只重译交回英文的第二段和前后两段，第四、五段照旧复用。
    assert [segs for segs, _repair in calls] == [[S1, S2, S3]]
    assert again.reused == 2 and conn.writes[-1][3] == Z1 + Z2 + Z3 + Z4 + Z5
    assert pr.echoed_slots(conn.writes[-1][7].obj) == []
