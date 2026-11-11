"""语义分融合、长文封顶、交给模型的上下文和目录。不连数据库。"""

from __future__ import annotations

import threading

import pytest

from researchlib import embeddings
from researchlib.ask import _batches, _material, _size, outline_note
from researchlib.retrieval import (
    LEXICAL_WEIGHT,
    SEMANTIC_BAND,
    FocusMatcher,
    Passage,
    Term,
    _ord_range,
    _title_mentions,
    article_caps,
    fuse_semantic,
    semantic_scores,
    thin_text,
)


def _p(article_id: int, ord_: int, score: float, *, coverage: float = 1.0, semantic: float = 0.0) -> Passage:
    return Passage(article_id, ord_, "", "正文", "标题", "src", None, score=score, coverage=coverage, semantic=semantic)


def test_semantic_scores_follow_similarity_and_drop_far_matches():
    sims = {(1, 0): 0.82, (2, 0): 0.80, (3, 0): 0.70, (4, 0): 0.55}
    scores = semantic_scores(sims)
    # 以第二近的那块为满分线，比它还近的都是 1 分。
    assert scores[(1, 0)] == 1.0
    assert scores[(2, 0)] == pytest.approx(1.0)
    assert scores[(2, 0)] > scores[(3, 0)]
    assert scores[(3, 0)] == pytest.approx((0.70 - (0.80 - SEMANTIC_BAND)) / SEMANTIC_BAND)
    # 比这条线差出 SEMANTIC_BAND 以上的不算语义相关：记 0 分（算过、不相关），不是没有。
    assert scores[(4, 0)] == 0.0


def test_semantic_scores_do_not_stop_at_a_rank_limit():
    # 以前按名次折算，第 60 名以后一律 0 分：库大了以后关键词召回到的段落几乎都没有语义分。
    sims = {(i, 0): 0.9 - i * 0.0001 for i in range(300)}
    scores = semantic_scores(sims)
    assert len(scores) == 300
    assert scores[(299, 0)] > 0.8


def test_one_near_copy_of_the_question_does_not_flatten_the_rest():
    # 以前只和最近的那一块比，差出 0.15 就是 0 分：一块几乎照抄问题的段落让别的段落全没了语义分。
    sims = {(0, 0): 0.99, (1, 0): 0.72, (2, 0): 0.70, (3, 0): 0.69}
    scores = semantic_scores(sims)
    assert scores[(3, 0)] == pytest.approx(0.5)


def test_when_only_one_block_is_close_the_far_ones_get_nothing():
    # 范围很窄（一位作者、一篇文章）时第二近的那块可能离得很远，不能拿它当满分线，让远的都沾光。
    scores = semantic_scores({(0, 0): 0.98, (1, 0): 0.04, (2, 0): 0.03})
    assert scores[(0, 0)] == 1.0
    assert scores[(1, 0)] == scores[(2, 0)] == 0.0


def test_blocks_with_only_links_and_file_names_count_as_empty():
    # 这样的块算出来的向量就是标题的向量，不能拿它算语义分。
    assert thin_text({"heading": "", "body": "Z6ZJbq.webp BO6fbH.webp v2-981fc.jpg"})
    assert thin_text({"heading": "", "body": "[](https://example.com/wp-content/uploads/2013/03/a.png)"})
    assert thin_text({"heading": "", "body": "On to the update:"})
    shots = "Screenshot 2024-06-17 at 16.18.55.png Screenshot 2024-06-17 at 16.50.58.png 0 image.png"
    assert thin_text({"heading": "", "body": shots})
    assert thin_text({"heading": "", "body": "截屏2025-06-08 19.36.10.png Screen Shot 2017-06-01 at 7.40.43 PM"})
    # 章节标题是同一节各块共用的，正文只有文件名照样算没字。
    assert thin_text({"heading": "规模效应被称为商业世界里的万有引力，" * 3, "body": "image.png"})
    assert not thin_text({"heading": "", "body": "会员费是利润的大头"})
    assert not thin_text({"heading": "", "body": "Costco makes its money from membership fees."})


@pytest.mark.parametrize(
    "body",
    [
        "会员费是利润的大头，低价换来续费率。",
        "见 https://example.com/a.png 和 www.example.com/b",
        "HTTPS://EXAMPLE.COM/A 大写的链接",
        "截屏2025-06-08 19.36.10.png 后面的字",
        "屏幕截图 2024-01-02 10.11.12 后面的字",
        "Screen Shot 2017-06-01 at 7.40.43 PM (2).PNG",
        "SCREENSHOT 2024-06-17 AT 16.18.55.JPEG",
        "图在这里 image.Png、v2-981fc.JPG、附件.PDF、录屏.Mp4",
        # 忽略大小写时和 ASCII 字母算同一个、转小写后却不是 ASCII 的字母：长 s 写的截图名、开尔文符号。
        "ſcreenshot 2024-06-17 at 16.18.55.png",
        "ſcreen Shot 2017-06-01 at 7.40.43 PM",
        "无点的 ı 和带点的 İ，开尔文符号 K",
        "",
    ],
)
def test_skipping_the_link_pattern_never_changes_the_text(body):
    from researchlib.retrieval import _NOT_TEXT, _drop_not_text

    # 没有那几种字样时不跑正则，得到的字必须和跑了一模一样，否则段落的分数会变。
    assert _drop_not_text(body) == _NOT_TEXT.sub(" ", body)


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


# —— 查询向量：本机 Ollama 闲置后会卸下模型，重新加载要几秒 ——


@pytest.fixture
def query_embedders():
    """冷、热两种超时各一个假的算向量函数，记下这次用的是哪个。结束时 use_embedder(None) 把状态清干净。"""
    used: list[str] = []
    state = {"fail": False}

    def make(name):
        def embed(texts):
            used.append(name)
            if state["fail"]:
                raise embeddings.EmbeddingError("连不上")
            return [[1.0] + [0.0] * (embeddings.DIM - 1) for _ in texts]

        return embed

    embeddings.use_embedder(make("full"))
    embeddings._state.query_embedder = make("warm")
    embeddings._state.cold_embedder = make("cold")
    yield used, state
    embeddings.use_embedder(None)


def test_first_query_after_idle_waits_for_the_model_to_load(query_embedders):
    used, _state = query_embedders
    assert embeddings.query_vector("costco 商业模式") is not None
    # 刚算过，模型还在：用 4 秒的超时。
    assert embeddings.query_vector("会员费") is not None
    assert used == ["cold", "warm"]
    # 闲置超过 WARM_FOR_S，模型多半被卸下了：这次放宽超时。
    embeddings._state.last_ok -= embeddings.WARM_FOR_S + 1
    assert embeddings.query_vector("低毛利") is not None
    assert used[-1] == "cold"


def test_after_a_failure_queries_pause_and_do_not_wait_long_again(query_embedders):
    used, state = query_embedders
    state["fail"] = True
    assert embeddings.query_vector("costco 商业模式") is None
    assert embeddings.paused()
    # 暂停期间不再去连。
    assert embeddings.query_vector("会员费") is None
    assert used == ["cold"]
    # 暂停过后再试：上次失败了，不再放宽超时，服务卡住时每次搜索最多多等 4 秒。
    embeddings._state.down_until = 0.0
    assert embeddings.query_vector("会员费") is None
    assert used == ["cold", "warm"]
    state["fail"] = False
    embeddings._state.down_until = 0.0
    assert embeddings.query_vector("会员费") is not None
    assert not embeddings.paused()


def test_prefetched_query_vector_is_computed_once_and_waited_for(query_embedders):
    used, _state = query_embedders
    started, release = threading.Event(), threading.Event()

    def slow(texts):
        started.set()
        release.wait(5)
        used.append("slow")
        return [[1.0] + [0.0] * (embeddings.DIM - 1) for _ in texts]

    embeddings._state.query_embedder = embeddings._state.cold_embedder = slow
    # 检索一开始就在后台算问题的向量；同一个问题再预取一次不另算。
    embeddings.prefetch_query_vector("costco 商业模式")
    embeddings.prefetch_query_vector("costco 商业模式")
    assert started.wait(5)
    got: dict = {}
    waiter = threading.Thread(target=lambda: got.setdefault("vector", embeddings.query_vector("costco 商业模式")))
    waiter.start()
    waiter.join(0.2)
    # 用到时后台那一次还没算完：等它，不再另算一次。
    assert waiter.is_alive()
    release.set()
    waiter.join(5)
    assert got["vector"] is not None
    assert used == ["slow"]
    # 算完记进缓存，再问直接用。
    assert embeddings.query_vector("costco 商业模式") == got["vector"]
    assert used == ["slow"]


def test_prefetch_failure_pauses_like_a_direct_call(query_embedders):
    used, state = query_embedders
    state["fail"] = True
    embeddings.prefetch_query_vector("costco 商业模式")
    assert embeddings.query_vector("costco 商业模式") is None
    assert embeddings.paused()
    assert used == ["cold"]
    # 暂停期间预取也不去连。
    embeddings.prefetch_query_vector("会员费")
    assert embeddings.query_vector("会员费") is None
    assert used == ["cold"]


def test_keep_warm_only_pings_a_local_service_that_has_been_idle(query_embedders):
    used, state = query_embedders
    # 云端的服务不保温：模型不会被卸下，每次调用还要花钱。
    assert not embeddings.keep_warm()
    assert used == []
    embeddings._state.local = True
    # 进程刚起来还没算过：先算一次，上线后的第一次搜索不用等模型加载。
    assert embeddings.keep_warm()
    assert used == ["cold"]
    # 刚算过就不算；保温算过，模型在，搜索用短超时。
    assert not embeddings.keep_warm()
    assert embeddings.query_vector("costco 商业模式") is not None
    assert used == ["cold", "warm"]
    embeddings._state.last_ok -= embeddings.KEEP_WARM_S
    assert embeddings.keep_warm()
    assert used[-1] == "cold"
    # 保温没成不暂停检索的语义这一路，下次搜索照常试。
    embeddings._state.last_ok -= embeddings.KEEP_WARM_S
    state["fail"] = True
    assert not embeddings.keep_warm()
    assert not embeddings.paused()


@pytest.mark.parametrize(
    ("base", "local"),
    [
        ("http://host.docker.internal:11434/v1", True),
        ("http://127.0.0.1:11434/v1", True),
        ("http://localhost:8080/v1", True),
        ("http://mac-mini.local:9000/v1", True),
        ("https://api.example.com/v1", False),
        ("https://embeddings.example.org:443/v1", False),
    ],
)
def test_only_services_on_this_machine_count_as_local(base, local):
    assert embeddings._is_local(base) is local


# —— 检索页：标题算不算写到了问的公司和词 ——

COSTCO = {"id": 7, "name_zh": "开市客", "name_en": "Costco", "ticker": "COST", "aliases": ["好市多"]}
DISNEY = {"id": 8, "name_zh": "迪士尼", "name_en": "Disney", "ticker": "DIS", "aliases": []}


def test_title_mentions_uses_alias_rules_not_substrings():
    matcher = FocusMatcher([COSTCO, DISNEY])
    costco = Term("开市客", "company", 0, forms=["开市客", "Costco", "COST", "好市多"], company_id=7)
    disney = Term("迪士尼", "company", 1, forms=["迪士尼", "Disney", "DIS"], company_id=8)
    assert _title_mentions(costco, "Costco 的会员费", matcher)
    assert _title_mentions(costco, "好市多开业", matcher)
    # 以前按子串找，「COST」算进了「Cost」，「DIS」算进了「Disruption」。
    assert not _title_mentions(costco, "The Cost of AI", matcher)
    assert not _title_mentions(disney, "Disruption Theory", matcher)
    word = Term("ai", "word", 2, forms=["ai"])
    assert _title_mentions(word, "AI and the Cloud", None)
    assert not _title_mentions(word, "Retail Margins", None)
    assert _title_mentions(Term("商业", "word", 3, forms=["商业"]), "开市客的商业模式", None)
