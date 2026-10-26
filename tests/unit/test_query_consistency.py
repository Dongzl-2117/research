"""Public query grain and hybrid retrieval boundaries; no live DB or models."""
from __future__ import annotations

from datetime import date

import pytest

from researchlib import ask, retrieval
from researchlib.duplicates import _ranker
from researchlib.retrieval import Query, Term
from researchlib.web import queries
from researchlib.web.queries import Scope


class Rows:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class Connection:
    def __init__(self, rows=()):
        self.statements = []
        self.rows = rows

    def execute(self, sql, params=None):
        self.statements.append((sql, params))
        return Rows(self.rows)


def query(terms=(), **kwargs):
    values = dict(raw="", terms=list(terms), companies=[], authors=[], excludes=[], pairs=[],
                  years=[], recent=False, overview=False, timeline=False, author_intent=False)
    values.update(kwargs)
    return Query(**values)


def test_full_match_groups_share_body_ocr_translation_corpus_and_scope():
    q = query([Term("低价", "word", 0, ["低价"]), Term("规模", "word", 1, ["规模"])], excludes=["广告"])
    where, params = retrieval._full_match_where(q, Scope(
        source_ids=["fangwei-mp"], date_from=date(2026, 1, 1), company_id=7,
    ))
    # Each group has its own indexed article/chunk/translation union. Requiring
    # both groups in a single chunk would miss cross-paragraph matches.
    assert where.count("FROM article_chunks cq") == 3  # two required groups + exclude
    assert where.count("FROM article_translations tq") == 3
    assert "NOT (a.id IN" in where
    assert "a.duplicate_of IS NULL" in where and params["scope_entity"] == [7]
    assert "projection_md" in where and "suppressed" in where
    assert params["scope_from"] == date(2026, 1, 1)


def test_pure_recruitment_cannot_become_evidence_even_with_semantic_support(monkeypatch):
    monkeypatch.setattr(retrieval, "_corpus_stats", lambda conn: (2, 100))
    promo = (
        "— 扫码下方微信加入会员群—\n"
        "适合研究行业的朋友加入，我们讨论公司的经营方式，也可以私信我问题。（备注：群会员）"
    )
    rows = [
        {"article_id": 1, "ord": 0, "heading": "主营业务", "body": promo, "title": "经营方式", "source_id": "s"},
        {"article_id": 2, "ord": 0, "heading": "", "body": "集中采购降低经营成本。",
         "title": "经营方式", "source_id": "s"},
    ]
    q = query([Term("经营", "word", 0, ["经营"], df=2, idf=1)])
    found = retrieval.score_rows(Connection(), q, rows, scope=Scope(), semantic={(1, 0): 1.0, (2, 0): 0.1})
    assert [passage.article_id for passage in found] == [2]
    assert rows[0]["body"] == promo

    # A labelled quotation/case study is evidence, even if its text is an ad.
    rows[0]["heading"] = "引用的推广文案"
    quoted = retrieval.score_rows(Connection(), q, rows[:1], scope=Scope(), semantic={(1, 0): 1.0})
    assert [passage.article_id for passage in quoted] == [1]


def test_group_or_and_company_constraints_are_not_flattened_to_all_words():
    q = query([Term("腾讯", "company", 0, ["腾讯"], company_id=1),
               Term("阿里", "company", 0, ["阿里"], company_id=2),
               Term("增长", "word", 1, ["增长"])])
    where, params = retrieval._full_match_where(q, Scope(source_ids=["s"]))
    assert params["match_0_0"] == [1] and params["match_0_1"] == [2]
    assert "_entity_alias" in where
    assert "FROM article_chunks cq" in where


def test_company_index_detail_and_author_stats_share_canonical_policy():
    calls = [
        lambda c: queries.list_companies(c, ["s"]),
        lambda c: queries.company_depth_counts(c, 1, ["s"]),
        lambda c: queries.company_author_counts(c, 1, ["s"]),
        lambda c: queries.company_months(c, 1, ["s"]),
        lambda c: queries.company_source_stats(c, 1, ["s"]),
        lambda c: queries.source_stats(c, ["s"]),
    ]
    for call in calls:
        conn = Connection()
        call(conn)
        sql = conn.statements[-1][0]
        assert "duplicate_of IS NULL" in sql
        assert "owner_account_id" in sql
        if "article_mentions" in sql:
            assert "projection_md" in sql
        assert "poster_role', '') <> 'guest'" not in sql


def test_get_article_projects_legacy_owner_and_never_returns_legacy_comments():
    conn = Connection([dict(id=7, source_id="fangwei-zsxq", title="观点", body_text="过期缓存",
                           body_md="星主原文。\n\n## 评论（1）\n\n网友未经验证的观点。",
                           raw_meta={"poster_id": "1825524112", "poster_role": "guest"})])
    article = queries.get_article(conn, 7, ["fangwei-zsxq"])
    assert "星主原文" in article["body_text"]
    assert "未经验证" not in article["body_text"]
    assert article["raw_meta"]["poster_role"] == "owner"


def test_get_article_rejects_nickname_without_account_proof():
    conn = Connection([dict(id=8, source_id="fangwei-zsxq", body_md="私有网友长文",
                           raw_meta={"poster_name": "方伟Rey", "poster_role": "owner"})])
    assert queries.get_article(conn, 8, ["fangwei-zsxq"]) is None


def test_duplicate_rank_never_prefers_guest_length_over_verified_owner():
    rows = [
        dict(id=1, source_id="fangwei-zsxq", raw_meta={"poster_id": "not-owner"},
             body_md="网友长文" * 500, body_text="网友长文" * 500, images=20, published_at=None),
        dict(id=2, source_id="fangwei-zsxq", raw_meta={"poster_id": "1825524112"},
             body_md="星主的实际判断。", body_text="星主的实际判断。", images=0, published_at=None),
    ]
    assert min(rows, key=_ranker(rows))["id"] == 2


def test_vector_lookup_is_entity_constrained_and_checks_current_model_text(monkeypatch):
    conn = Connection()
    monkeypatch.setattr(retrieval.embeddings, "tune_search", lambda c, *args: None)
    monkeypatch.setattr(retrieval.embeddings, "model_name", lambda: "existing-model")
    retrieval._vector_keys(conn, "halfvec", "[1,0]", Scope(source_ids=["s"]), 5, company_ids=[7])
    sql, params = conn.statements[-1]
    assert "_entity_m.company_id = ANY" in sql and "_entity_m.depth <> 'passing'" in sql
    assert "_entity_alias" in sql and "projection_md" in sql
    assert "JOIN article_chunks c" in sql and "e.text_hash = md5" in sql
    assert params["vec_model"] == "existing-model"


def test_named_entity_vector_lookup_never_opens_a_second_other_company_channel(monkeypatch):
    calls = []
    monkeypatch.setattr(retrieval, "_vector_keys", lambda *a, **kw: calls.append(kw) or {(1, 0): .8})
    result = retrieval._vector_lookup(Connection(), "halfvec", "[1,0]", Scope(), [], 8, [7])
    assert result == {(1, 0): .8}
    assert calls == [{"company_ids": [7]}]


def test_semantic_ranking_favors_mechanism_over_incidental_exact_topic(monkeypatch):
    company = dict(id=1, name_zh="目标企业", name_en="TargetCo", ticker="TGTX", aliases=[])
    q = query([Term("目标企业", "company", 0, ["目标企业"], company_id=1, idf=1),
               Term("商业模式", "word", 1, ["商业模式"], idf=5)])
    rows = [
        dict(article_id=1, ord=0, title="目标企业", heading="", body="目标企业集中订单、降低单位履约成本。",
             source_id="s", published_at=None),
        dict(article_id=2, ord=0, title="别的行业", heading="", body="商业模式商业模式商业模式，目标企业也曾上市。",
             source_id="s", published_at=None),
        dict(article_id=3, ord=0, title="其他公司", heading="", body="商业模式很有趣。",
             source_id="s", published_at=None),
    ]
    conn = Connection([dict(article_id=1, company_id=1, depth="whole"),
                       dict(article_id=2, company_id=1, depth="passing")])
    monkeypatch.setattr(retrieval, "_corpus_stats", lambda c: (10, 100))
    scored = retrieval.score_rows(conn, q, rows, scope=Scope(), company_rows=[company],
                                  semantic={(1, 0): 1, (2, 0): .9, (3, 0): .99})
    assert [p.article_id for p in sorted(scored, key=lambda p: -p.score)] == [1, 2]
    assert scored[0].semantic_hit and not scored[1].semantic_hit


@pytest.mark.parametrize("body, supported", [
    ("风险在于目标企业的护城河可能走弱，如果没有改善应重新评估。", True),
    ("TargetCo's moat comes from lower fulfillment costs.", True),
    ("比如目标企业的护城河也曾变化。", False),
    ("For example, TargetCo's moat was similar.", False),
    ("E.G. TargetCo's moat was similar.", False),
    ("主题机制反复出现，目标企业也曾上市。", False),
    ("TargetCo was listed. Moat is a generic topic.", False),
    ("这里讨论主题机制，正文没有这家公司。", False),
])
def test_article_passing_label_does_not_erase_a_direct_topic_clause(monkeypatch, body, supported):
    company = dict(id=1, name_zh="目标企业", name_en="TargetCo", ticker="TGTX", aliases=[])
    q = query([Term("目标企业", "company", 0, ["目标企业", "TargetCo"], company_id=1, idf=1),
               Term("主题机制", "word", 1, ["主题机制", "护城河", "moat"], idf=5)])
    row = dict(article_id=1, ord=0, title="目标企业", heading="", body=body,
               source_id="s", published_at=None)
    conn = Connection([dict(article_id=1, company_id=1, depth="passing")])
    monkeypatch.setattr(retrieval, "_corpus_stats", lambda c: (10, 100))
    scored = retrieval.score_rows(conn, q, [row], scope=Scope(), company_rows=[company], semantic={(1, 0): .9})
    assert len(scored) == 1
    assert scored[0].semantic == (.9 if supported else 0.0)


def test_lexical_paragraph_subject_does_not_claim_semantic_or_cross_company_support(monkeypatch):
    companies = [dict(id=1, name_zh="目标企业", name_en="TargetCo", ticker="TGTX", aliases=[]),
                 dict(id=2, name_zh="其他企业", name_en="OtherCo", ticker="OTRX", aliases=[])]
    q = query([Term("目标企业", "company", 0, ["目标企业"], company_id=1, idf=1),
               Term("护城河", "word", 1, ["护城河", "moat"], idf=5)])
    bodies = ["风险在于目标企业的门槛下滑。管理层又谈到了护城河。",
              "比如目标企业的门槛下滑。管理层又谈到了护城河。",
              "目标企业已经上市。OtherCo's moat is strong.",
              "目标企业讨论了门槛。\n\n这里泛谈护城河。"]
    rows = [dict(article_id=i, ord=0, title="原文", heading="", body=body,
                 source_id="s", published_at=None) for i, body in enumerate(bodies, 1)]
    conn = Connection([dict(article_id=i, company_id=1, depth="passing") for i in range(1, 5)])
    monkeypatch.setattr(retrieval, "_corpus_stats", lambda c: (10, 100))
    scored = retrieval.score_rows(conn, q, rows, scope=Scope(), company_rows=companies,
                                  semantic={(i, 0): .9 for i in range(1, 5)})
    by_id = {p.article_id: p for p in scored}
    assert by_id[1].score > 4 * max(by_id[i].score for i in (2, 3, 4))
    assert all(p.semantic == 0 for p in scored)


def test_out_of_vocabulary_theme_still_reaches_semantic_channel(monkeypatch):
    q = query([Term("全新表达", "word", 0, ["全新表达"], df=0, idf=1)])
    monkeypatch.setattr(retrieval, "load_company_rows", lambda c: [])
    monkeypatch.setattr(retrieval, "analyze", lambda *a, **kw: q)
    monkeypatch.setattr(retrieval, "_candidates", lambda *a, **kw: [])
    called = []
    monkeypatch.setattr(retrieval, "semantic_candidates", lambda c, actual, *a, **kw: called.append(actual) or ([], {}))
    monkeypatch.setattr(retrieval, "score_rows", lambda *a, **kw: [])
    retrieval.retrieve(Connection(), "全新表达", Scope(source_ids=["s"]), [], purpose="display")
    assert called and called[0].terms[0].text == "全新表达" and not called[0].overview


def test_synthesize_without_evidence_does_not_call_model(monkeypatch):
    monkeypatch.setattr(ask, "chat", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("model call")))
    report = {}
    text, model = ask.synthesize(None, "问题", [], report)
    assert "没有足够" in text and model == "" and report["insufficient_evidence"]


def test_search_facets_and_pages_share_exact_ids_even_when_candidate_cap_misses_rows(monkeypatch):
    q = query([Term("词", "word", 0, ["词"])])
    monkeypatch.setattr(retrieval, "load_company_rows", lambda c: [])
    monkeypatch.setattr(retrieval, "analyze", lambda *a, **kw: q)
    monkeypatch.setattr(retrieval, "_full_match_articles", lambda *a: [dict(id=i, source_id="s") for i in range(1, 14)])
    monkeypatch.setattr(retrieval, "_candidates", lambda *a, **kw: [])
    monkeypatch.setattr(retrieval, "semantic_candidates", lambda c, q, sc, rows: (rows, {}))
    monkeypatch.setattr(retrieval, "_missing_match_rows", lambda c, q, ids: [dict(
        article_id=i, ord=0, heading="", body="原文", title="标题", source_id="s", published_at=None,
        poster_name="真实作者", poster_role="owner",
    ) for i in ids])
    monkeypatch.setattr(retrieval, "score_rows", lambda *a, **kw: [])
    result = retrieval.search_articles(Connection(), "词", Scope(source_ids=["s"]), [], limit=5, offset=10)
    assert result.total == result.counts["s"] == result.available == 13
    assert [hit.article_id for hit in result.hits] == [11, 12, 13]
    assert result.hits[0].poster_name == "真实作者"


def test_original_owner_chunks_reused_only_with_exact_projection_proof():
    article = dict(id=7, source_id="fangwei-zsxq", title="原帖",
                   body_md="星主的实际判断。\n\n## 评论（1）\n\n网友的未经验证判断。",
                   body_text="星主的实际判断。\n\n评论（1）\n\n网友的未经验证判断。",
                   raw_meta={"poster_id": "1825524112", "poster_role": "guest"})
    rows = [dict(article_id=7, source_id="fangwei-zsxq", title="原帖", ord=0, lang="", heading="",
                 body="星主的实际判断。"),
            dict(article_id=7, source_id="fangwei-zsxq", title="原帖", ord=1, lang="", heading="评论（1）",
                 body="网友的未经验证判断。"),
            dict(article_id=7, source_id="fangwei-zsxq", title="原帖", ord=20000, lang="", heading="",
                 body="星主的实际判断。")]
    result = retrieval._safe_chunk_rows(Connection([article]), rows)
    assert [row["ord"] for row in result] == [0]
    assert result[0]["poster_role"] == "owner"


def test_safe_original_entity_match_does_not_need_stale_company_association(monkeypatch):
    company = dict(id=1, name_zh="目标企业", name_en="TargetCo", ticker="TGTX", aliases=[])
    q = query([Term("目标企业", "company", 0, ["目标企业"], company_id=1, idf=1),
               Term("主题改写", "word", 1, ["主题改写"], idf=3)])
    row = dict(article_id=1, ord=0, title="原文", heading="", body="目标企业的原文机制。",
               source_id="s", published_at=None)
    monkeypatch.setattr(retrieval, "_corpus_stats", lambda c: (10, 100))
    scored = retrieval.score_rows(Connection(), q, [row], scope=Scope(), company_rows=[company],
                                  semantic={(1, 0): 1.0})
    assert len(scored) == 1 and scored[0].semantic_hit


def test_chunk_context_cannot_reintroduce_removed_guest_cache():
    article = dict(id=7, source_id="fangwei-zsxq", title="原帖", body_md="星主的实际判断。",
                   body_text="星主的实际判断。", raw_meta={"poster_id": "1825524112"})
    chunk = dict(article_id=7, source_id="fangwei-zsxq", title="原帖", ord=1, lang="", heading="",
                 body="网友不应再进入模型上下文。")

    class ContextConnection(Connection):
        def execute(self, sql, params=None):
            self.statements.append((sql, params))
            return Rows([article] if "SELECT id, source_id" in sql else [chunk])

    passage = retrieval.Passage(7, 0, "", "星主的实际判断。", "原帖", "fangwei-zsxq", None)
    assert retrieval.attach_context(ContextConnection(), [passage], 500) == 0
    assert passage.before == passage.after == ""


def test_generated_answer_must_reference_actual_selected_evidence_numbers():
    import pytest

    ask.validate_citation_numbers("判断[1][3]", [{"n": 1}, {"n": 3}])
    with pytest.raises(ask.LLMError, match="不存在"):
        ask.validate_citation_numbers("编造出处[2]", [{"n": 1}, {"n": 3}])


def test_mixed_demo_records_are_not_silently_labeled_real():
    assert queries.sample_state(Connection([{"n": 10, "samples": 0}]), ["s"]) is None
    assert queries.sample_state(Connection([{"n": 10, "samples": 1}]), ["s"]) == "mixed"
    assert queries.sample_state(Connection([{"n": 10, "samples": 10}]), ["s"]) == "all"


def test_company_code_alias_and_topic_are_general_analyzer_inputs(monkeypatch):
    company = dict(id=17, name_zh="拼多多", name_en="Pinduoduo", ticker="PDD", aliases=["PDD Holdings", "Temu"])
    monkeypatch.setattr(retrieval, "_content_tokens", lambda c, text: ["商业模式"] if "商业模式" in text else [])
    monkeypatch.setattr(retrieval, "_fill_stats", lambda c, terms: terms)
    actual = retrieval.analyze(None, "pdd 商业模式", [], company_rows=[company])
    assert actual.companies[0]["id"] == 17
    assert [(term.kind, term.text) for term in actual.terms] == [("company", "拼多多"), ("word", "商业模式")]


def test_generation_checks_all_retrieved_inputs_even_uncited_and_metadata_only_changes():
    import pytest

    passages = [
        dict(article_id=1, cited=True, _input_content_hash="body1", _input_ocr_hash="", _input_meta_hash="meta1"),
        dict(article_id=2, cited=False, _input_content_hash="body2", _input_ocr_hash="", _input_meta_hash="meta2"),
    ]
    rows = [dict(id=1, content_hash="body1", ocr_hash="", raw_meta_db_hash="meta1"),
            dict(id=2, content_hash="body2", ocr_hash="", raw_meta_db_hash="meta2")]
    ask._check_retrieved_inputs(passages, rows)
    rows[1]["raw_meta_db_hash"] = "identity-or-suppression-changed"
    with pytest.raises(ask.LLMError, match="身份信息已变化"):
        ask._check_retrieved_inputs(passages, rows)


def test_long_question_cannot_evict_explicit_entity_or_quoted_constraint(monkeypatch):
    company = dict(id=9, name_zh="目标企业", name_en="TargetCo", ticker="TGTX", aliases=[])
    monkeypatch.setattr(retrieval, "_content_tokens", lambda c, text: [text] if text.startswith("稀词") else [])

    def fill_stats(_conn, terms):
        for term in terms:
            term.idf = .01 if term.kind in ("company", "phrase") else 10
        return terms

    monkeypatch.setattr(retrieval, "_fill_stats", fill_stats)
    actual = retrieval.analyze(None, 'tgtx "必须原样" ' + ' '.join(f'稀词{i}' for i in range(13)), [],
                               company_rows=[company], expansions={"必须原样": ["别的说法"]})
    assert retrieval._query_company_ids(actual) == [9]
    assert any(term.kind == "company" for term in actual.terms)
    assert any(term.kind == "phrase" and term.text == "必须原样" for term in actual.terms)
    assert sum(term.kind == "word" for term in actual.terms) == 12
    assert not any(term.text == "别的说法" for term in actual.terms)


def test_hard_entity_survives_a_reduced_query_term_list(monkeypatch):
    company = dict(id=9, name_zh="目标企业", name_en="TargetCo", ticker="TGTX", aliases=[])
    q = query([Term("主题", "word", 0, ["主题"], idf=1)], companies=[company])
    rows = [dict(article_id=1, ord=0, title="原文", heading="", body="目标企业的主题机制。",
                 source_id="s", published_at=None),
            dict(article_id=2, ord=0, title="另一家公司", heading="", body="另一家公司的主题机制。",
                 source_id="s", published_at=None)]
    monkeypatch.setattr(retrieval, "_corpus_stats", lambda c: (10, 100))
    result = retrieval.score_rows(Connection(), q, rows, scope=Scope(), company_rows=[company],
                                  semantic={(1, 0): .9, (2, 0): 1.0})
    assert [item.article_id for item in result] == [1]


def test_broad_exact_match_hydration_is_bounded_to_requested_page(monkeypatch):
    q = query([Term("词", "word", 0, ["词"])])
    monkeypatch.setattr(retrieval, "load_company_rows", lambda c: [])
    monkeypatch.setattr(retrieval, "analyze", lambda *a, **kw: q)
    monkeypatch.setattr(retrieval, "_full_match_articles",
                        lambda *a: [dict(id=i, source_id="s") for i in range(1, 5001)])
    monkeypatch.setattr(retrieval, "_candidates", lambda *a, **kw: [])
    monkeypatch.setattr(retrieval, "semantic_candidates", lambda c, q, sc, rows: (rows, {}))
    monkeypatch.setattr(retrieval, "score_rows", lambda *a, **kw: [])
    hydrated = []

    def missing(_conn, _query, ids):
        hydrated.append(ids)
        return [dict(article_id=i, ord=0, heading="", body="原文", title="标题", source_id="s", published_at=None)
                for i in ids]

    monkeypatch.setattr(retrieval, "_missing_match_rows", missing)
    first = retrieval.search_articles(Connection(), "词", Scope(source_ids=["s"]), [], limit=20)
    later = retrieval.search_articles(Connection(), "词", Scope(source_ids=["s"]), [], limit=20, offset=4980)
    assert len(hydrated[0]) == len(hydrated[1]) == 20
    assert [hit.article_id for hit in first.hits] == list(range(1, 21))
    assert [hit.article_id for hit in later.hits] == list(range(4981, 5001))
    assert first.total == later.total == first.counts["s"] == later.available == 5000


@pytest.mark.parametrize("mutate", [False, True])
def test_generation_records_complete_inputs_before_model_and_rechecks_after(monkeypatch, mutate):
    from types import SimpleNamespace

    from researchlib import sources as sources_module

    articles = [dict(id=i, source_id="src", title=f"原帖{i}", body_md=f"判断{i}", body_text=f"判断{i}",
                     raw_meta={}, content_hash=f"body{i}", ocr_hash="", raw_meta_db_hash=f"meta{i}")
                for i in (1, 2)]
    passages = [retrieval.Passage(i, 0, "", f"判断{i}", f"原帖{i}", "src", None,
                                  article_content_hash=f"body{i}", article_meta_hash=f"meta{i}") for i in (1, 2)]
    found = retrieval.Retrieval(passages, [], [], Scope(source_ids=["src"]))
    source = SimpleNamespace(id="src", name="来源", author="作者", enabled=True)
    monkeypatch.setattr(sources_module, "load_sources", lambda path: [source])
    monkeypatch.setattr(sources_module, "enabled_sources", lambda values: values)
    monkeypatch.setattr(ask, "retrieve", lambda *a, **kw: found)

    class JobConnection(Connection):
        def execute(self, sql, params=None):
            self.statements.append((sql, params))
            if "SELECT question, scope" in sql:
                return Rows([dict(question="问题", scope={"source_ids": ["src"], "preset": True})])
            if "SELECT id, source_id" in sql:
                return Rows([dict(row) for row in articles])
            return Rows()

        def commit(self):
            pass

        def rollback(self):
            pass

    conn = JobConnection()

    def model_stub(_settings, _question, items, report, note):
        saved = [(sql, params) for sql, params in conn.statements if "SET scope =" in sql]
        assert len(saved) == 1
        assert set(saved[0][1][0].obj["_input_provenance"]) == {"1", "2"}
        assert "answer = ''" in saved[0][0]
        if mutate:
            articles[1]["raw_meta_db_hash"] = "uncited-input-identity-changed"
        return "判断[1]", "offline-stub"

    monkeypatch.setattr(ask, "synthesize", model_stub)
    ask.run_job(SimpleNamespace(sources_path="unused", display_timezone="Asia/Shanghai"), 1, conn=conn)
    completed = [params for sql, params in conn.statements if "SET answer =" in sql]
    errors = [params for sql, params in conn.statements if "SET status = 'error'" in sql]
    if mutate:
        assert not completed and len(errors) == 1 and "身份信息已变化" in errors[0][0]
    else:
        assert not errors and len(completed) == 1
        assert set(completed[0][5].obj["_input_provenance"]) == {"1", "2"}
