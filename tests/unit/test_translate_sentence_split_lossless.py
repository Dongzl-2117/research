"""长段按句切开时不许丢掉或多出链接：会丢的那一段整段留着，整篇照常能译、能签回执。

Stratechery 2026 网页导出的访谈在每节末尾留一个空锚点 ``[]()``（渲染成 ``<a href=""></a>``），
紧贴在最后一句的句号后面；链接文字里有缩写名时句号落在链接里。以前这两种段一按句切，
空锚点哪一句都不带、一个链接变成两三个，materialize 判 ``materialize-content``，整篇一开始就拒收。
合成样本，不调真模型、不连库。
"""

from __future__ import annotations

import pytest
from test_translate_number_review import run

from researchlib.translate import provenance as pr
from researchlib.translate.segments import iter_segments, split_sentence_paragraphs

SENTENCES = [
    "The platform first won by making distribution nearly free for anyone who wanted to publish something.",
    "Publishers followed the audience, and the audience followed whatever the feed decided to show them next.",
    "That arrangement worked well until the feed itself became the product that advertisers were paying for.",
    "Once that happened, every incentive inside the company pointed toward keeping people scrolling longer.",
    "Creators noticed the shift quickly, because their reach began to depend on formats rather than quality.",
    "Some of them moved to subscriptions, which turned out to be steadier than chasing each new algorithm.",
    "Others stayed and adapted, producing shorter pieces that fit the feed instead of the original audience.",
    "Neither path is obviously right, and the answer probably depends on how loyal the existing readers are.",
    "What is clear is that owning the relationship with readers matters more than renting attention again.",
]
PLAIN = " ".join(SENTENCES)
TRAILING_ANCHOR = f'<p>{PLAIN}<a href=""></a></p>'
NO_ANCHOR = f"<p>{PLAIN}</p>"
INITIALS = (
    "<p>" + " ".join(SENTENCES[:4])
    + ' The idea comes from <a href="https://example.invalid/public">[J. Q.] Public</a>, who wrote it down long ago. '
    + " ".join(SENTENCES[4:]) + "</p>"
)
CLOSING = "<p>A short closing note keeps the article from ending on the long paragraph.</p>"

ZH_TRAILING = "<p>平台最先靠让分发几乎免费取胜，此后读者、发布者和广告主一步步跟着信息流走。</p>"
ZH_INITIALS = '<p>平台靠免费分发起家；这个想法来自<a id="L0">[J. Q.] Public</a>，他很早就写下来了。</p>'
ZH_CLOSING = "<p>结尾的一句短话，免得文章停在那段长文上。</p>"


def test_samples_are_long_enough_to_be_split_by_sentence():
    assert len(PLAIN) > 900
    assert len(iter_segments(NO_ANCHOR)) == len(SENTENCES)


@pytest.mark.parametrize("html", [TRAILING_ANCHOR, INITIALS], ids=["trailing-empty-anchor", "initials-in-link"])
def test_split_that_would_lose_or_duplicate_a_link_keeps_the_paragraph_whole(html):
    segments = iter_segments(html)
    assert len(segments) == 1
    assert segments[0].html.count("<a ") == html.count("<a ")
    assert pr.materialize(html)
    assert len(pr.source_manifest(html)["segments"]) == 1


def test_paragraphs_that_split_cleanly_still_split_the_same_way():
    # 链接在句子中间、不跨句号时照旧按句切，已入库回执的切法不变。
    linked = NO_ANCHOR.replace("feed decided", '<a href="https://example.invalid/feed">feed</a> decided')
    segments = iter_segments(linked)
    assert len(segments) == len(SENTENCES)
    assert sum(segment.html.count("<a ") for segment in segments) == 1


def test_legacy_reflow_candidates_are_not_changed():
    # 旧稿候选重排（anchored_legacy_reflow 用）不走这道检查，照旧按句切。
    assert split_sentence_paragraphs(TRAILING_ANCHOR).count("<p>") == len(SENTENCES)


def test_article_with_such_paragraphs_translates_and_gets_a_receipt(tmp_path):
    def answer(segs, _repair):
        out = []
        for segment in segs:
            if 'id="L0"' in segment:
                out.append(ZH_INITIALS)
            elif "distribution" in segment:
                out.append(ZH_TRAILING)
            else:
                out.append(ZH_CLOSING)
        return out

    body = TRAILING_ANCHOR + INITIALS + CLOSING
    conn, result, calls = run(body, answer, tmp_path)
    assert result.status == "translated"
    assert [len(segs) for segs, _repair in calls] == [3]
    stored = conn.writes[0]
    assert "https://example.invalid/public" in stored[3]
    assert pr.verify(body, stored[3], stored[7].obj).status == "mapped"
