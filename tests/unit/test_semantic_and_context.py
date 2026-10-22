"""语义分融合、长文封顶、交给模型的上下文和目录。不连数据库。"""

from __future__ import annotations

from researchlib.ask import _batches, _material, _size, outline_note
from researchlib.retrieval import (
    LEXICAL_WEIGHT,
    SEMANTIC_RANKS,
    Passage,
    _ord_range,
    article_caps,
    fuse_semantic,
    semantic_scores,
)


def _p(article_id: int, ord_: int, score: float, *, coverage: float = 1.0, semantic: float = 0.0) -> Passage:
    return Passage(article_id, ord_, "", "正文", "标题", "src", None, score=score, coverage=coverage, semantic=semantic)


def test_semantic_scores_follow_rank_and_drop_far_matches():
    sims = {(1, 0): 0.82, (2, 0): 0.80, (3, 0): 0.70, (4, 0): 0.55}
    scores = semantic_scores(sims)
    assert scores[(1, 0)] == 1.0
    assert scores[(1, 0)] > scores[(2, 0)] > scores[(3, 0)]
    # 比最相近的那块差出 0.15 以上的不算语义相关。
    assert (4, 0) not in scores


def test_semantic_scores_stop_after_rank_limit():
    sims = {(i, 0): 0.9 - i * 0.0001 for i in range(SEMANTIC_RANKS + 10)}
    assert len(semantic_scores(sims)) == SEMANTIC_RANKS


def test_fuse_without_semantic_only_applies_time_weight():
    passages = [_p(1, 0, 2.0), _p(2, 0, 1.0)]
    fused = fuse_semantic(passages, [0.5, 1.0])
    assert [p.score for p in fused] == [1.0, 1.0]
    assert not any(p.semantic_hit for p in fused)


def test_fuse_lifts_passage_that_means_the_same_thing():
    # 1 号段写全了搜索词；2 号段一个词都没写，但语义上最接近。
    exact = _p(1, 0, 4.0, coverage=1.0, semantic=0.2)
    meaning = _p(2, 0, 0.0, coverage=0.0, semantic=1.0)
    noise = _p(3, 0, 1.0, coverage=0.3, semantic=0.0)
    fuse_semantic([exact, meaning, noise], [1.0, 1.0, 1.0])
    assert exact.score > meaning.score > noise.score
    assert meaning.score == 4.0 * (1 - LEXICAL_WEIGHT)
    assert meaning.semantic_hit and meaning.coverage == 1.0
    assert not exact.semantic_hit


def test_long_article_with_many_strong_passages_gets_more_room():
    ranked = [_p(1, i, 10.0 - i * 0.1) for i in range(8)] + [_p(2, 0, 9.0), _p(2, 1, 1.0)]
    caps = article_caps(ranked, 2)
    assert caps[1] == 6  # 2 × 3
    assert 2 not in caps  # 短文照旧每篇 2 段


def test_neighbours_stay_within_the_same_kind_of_chunk():
    assert _ord_range(0) == (0, 10000)
    assert _ord_range(10003) == (10000, 20000)
    assert _ord_range(20001)[0] == 20000


def _item(n: int, text: str, before: str = "", after: str = "") -> dict:
    return {
        "n": n, "title": "拼多多投资研究", "author": "林文丰", "source": "林文丰飞书", "date": "2025-01-01",
        "heading": "零售是什么生意", "text": text, "before": before, "after": after,
    }


def test_material_shows_context_around_the_hit_under_one_number():
    text = _material([_item(1, "命中这段", before="前一段", after="后一段")])
    assert text.index("（上文）前一段") < text.index("（命中段）命中这段") < text.index("（下文）后一段")
    assert text.count("[1]") == 1


def test_context_counts_toward_batch_size():
    small = _item(1, "甲" * 100)
    wide = _item(2, "乙" * 100, before="丙" * 1000, after="丁" * 1000)
    assert _size(wide) - _size(small) == 2000
    assert len(_batches([wide, wide], 2500)) == 2


def test_outline_note_lists_sections_and_trims_long_headings():
    note = outline_note(["零售是什么生意", "长" * 100], "这篇的章节")
    assert "这篇的章节如下" in note
    assert "- 零售是什么生意" in note
    assert "长" * 60 + "…" in note
    assert outline_note([], "这篇的章节") == ""
