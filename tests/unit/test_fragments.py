"""检索按片段列（C）：相邻相关的块拼成片段、隔一块也接、章节变了就断、一篇一种语言、每篇封顶。"""

from datetime import UTC, datetime

from researchlib.retrieval import Passage, _gap_keys, _one_language, build_fragments, cap_per_article, seeded_only

WHEN = datetime(2026, 1, 1, tzinfo=UTC)


def _p(article: int, ord_: int, score: float, heading: str = "第一章", body: str | None = None) -> Passage:
    return Passage(article, ord_, heading, body or f"第{article}篇第{ord_}块的正文，讲渠道费用。", "资料",
                   "src", WHEN, score=score)


def test_adjacent_blocks_join_and_heading_change_breaks():
    passages = [_p(1, 10, 1.0), _p(1, 11, 0.8), _p(1, 12, 0.6), _p(1, 13, 0.7, heading="第二章"), _p(1, 9, 0.3)]
    fragments = build_fragments(passages)
    first = fragments[0]
    assert [b.ord for b in first.blocks] == [10, 11, 12]  # 9 太弱，13 换了章节
    assert first.url == "/articles/1?chunk=10&to=12"
    assert sorted(b.ord for f in fragments for b in f.blocks) == [9, 10, 11, 12, 13]


def test_one_weak_block_in_between_is_bridged():
    passages = [_p(1, 20, 1.0), _p(1, 22, 0.9)]
    gaps = {(1, 21): {"article_id": 1, "ord": 21, "heading": "第一章", "body": "中间那块。", "title": "资料",
                      "source_id": "src", "published_at": WHEN, "lang": "", "anchor": ""}}
    assert _gap_keys(passages) == [(1, 21)]
    fragment = build_fragments(passages, gaps)[0]
    assert [b.ord for b in fragment.blocks] == [20, 21, 22] and "中间那块" in fragment.text
    # 没有中间那块的正文就不接。
    assert len(build_fragments(passages)) == 2


def test_six_blocks_at_most_and_ocr_never_joins():
    passages = [_p(1, ord_, 1.0 - ord_ / 100) for ord_ in range(10)]
    fragments = build_fragments(passages)
    assert len(fragments[0].blocks) == 6
    images = [_p(2, 20000, 1.0), _p(2, 20001, 0.9)]
    assert [len(f.blocks) for f in build_fragments(images)] == [1, 1]


def test_one_language_per_article():
    passages = [_p(1, 3, 0.5), _p(1, 10003, 0.9), _p(1, 20000, 0.4), _p(2, 4, 0.7)]
    kept = {(p.article_id, p.ord) for p in _one_language(passages)}
    assert kept == {(1, 10003), (1, 20000), (2, 4)}


def test_cap_per_article_counts_the_rest():
    passages = [_p(1, ord_ * 10, 1.0 - ord_ / 10) for ord_ in range(5)] + [_p(2, 0, 0.75)]
    listed = cap_per_article(build_fragments(passages))
    ones = [f for f in listed if f.article_id == 1]
    assert len(ones) == 3 and ones[-1].more == 2 and ones[0].more == 0
    assert any(f.article_id == 2 for f in listed)


def test_neighbours_outside_the_seeds_never_stand_alone():
    """种子旁边补进来的块只能接在种子上；没接上（换了章节）的不单独列。"""
    seed = _p(1, 10, 9.0)
    joined = _p(1, 11, 6.0)
    stray = _p(1, 9, 8.0, heading="上一章")
    fragments = build_fragments([seed, joined, stray])
    assert len(fragments) == 2
    kept = seeded_only(fragments, {(1, 10)})
    assert [(f.first, f.last) for f in kept] == [(10, 11)]
