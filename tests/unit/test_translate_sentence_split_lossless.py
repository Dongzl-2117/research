"""长段按句切开、按 ``<br>`` 行切开时不许丢掉或多出字和链接：会丢的那一块整块留着，整篇照常能译、能签回执。

Stratechery 2026 网页导出的访谈在每节末尾留一个空锚点 ``[]()``（渲染成 ``<a href=""></a>``），
紧贴在最后一句的句号后面；链接文字里有缩写名时句号落在链接里。以前这两种段一按句切，
空锚点哪一句都不带、一个链接变成两三个，materialize 判 ``materialize-content``，整篇一开始就拒收。
合成样本，不调真模型、不连库。
"""

from __future__ import annotations

import pytest
from test_translate_number_review import run

from researchlib.translate import provenance as pr
from researchlib.translate import segments as seg
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


def _letters(value: str) -> str:
    import html
    import re

    return re.sub(r"\s+", "", html.unescape(re.sub(r"<[^>]+>", "", value)))


def test_br_line_split_that_would_lose_a_line_keeps_the_block_whole(monkeypatch):
    """按 <br> 行切也过同一道检查：哪种切法少了一行，这一块就整块一段，materialize 照样过。"""
    block = "<p>" + "<br>".join(SENTENCES[:4]) + "</p>"
    assert len(iter_segments(block)) == 4
    real = seg._pack_br_groups
    monkeypatch.setattr(seg, "_pack_br_groups", lambda lines: real(lines)[:-1])
    segments = iter_segments(block)
    assert len(segments) == 1
    assert _letters("".join(item.html for item in segments)) == _letters(block)
    assert pr.materialize(block)


def test_definition_line_split_that_would_lose_a_line_keeps_the_entry_whole(monkeypatch):
    # dd 里够长（过 _DEF_BR_PLAIN）的 <br> 长文才走按行打包那条路。
    entry = "<dl><dt>Notes</dt><dd>" + "<br>".join(SENTENCES * 3) + "</dd></dl>"
    assert len(iter_segments(entry)) > 2
    real = seg._pack_fragments
    packed: list[int] = []

    def lossy(items, size):
        packed.append(len(items))
        return real(items, size)[:-1]

    monkeypatch.setattr(seg, "_pack_fragments", lossy)
    segments = iter_segments(entry)
    assert packed
    assert _letters("".join(item.html for item in segments)) == _letters(entry)
    assert pr.materialize(entry)
