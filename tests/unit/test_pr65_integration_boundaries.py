"""Selective upstream fixes must preserve the existing evidence boundary."""
import copy
import http.client
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_embedding_provenance import Connection, current_row

from researchlib import embeddings, retrieval
from researchlib.retrieval import BUDGETS, Query
from researchlib.web.queries import Scope

ALIAS_CASES = json.loads((Path(__file__).resolve().parents[1] / "fixtures/entity_alias_cases.json").read_text())


@pytest.mark.parametrize("company,text,expected", ALIAS_CASES["cases"])
def test_python_alias_case_and_boundaries_are_preserved(company, text, expected):
    row = ALIAS_CASES["companies"][company]
    assert bool(retrieval.FocusMatcher([row]).count(text)) is expected
    assert bool(retrieval.CompanyMatcher([row]).count(text)) is expected


@pytest.mark.parametrize("purpose,count,whole", [("llm", 70, True), ("deep", 70, True),
                                                 ("display", 40, False), ("api", 40, False)])
def test_medium_article_model_context_reaches_the_last_chunk(monkeypatch, purpose, count, whole):
    rows = [{"article_id": 1, "ord": i, "title": "研究", "source_id": "notes", "heading": f"节{i}",
             "body": "限定条件。" * 80, "lang": "", "published_at": None} for i in range(70)]
    monkeypatch.setattr(retrieval, "_article_rows", lambda *a: rows)
    q = Query("条件", [], [], [], [], [], [], False, False, False, False)
    found = retrieval._retrieve_article(None, q, Scope(article_id=1), BUDGETS[purpose],
                                        purpose=purpose, semantic_ok=False)
    assert len(found.passages) == count and found.whole == whole
    assert bool(found.outline) != whole
    if whole:
        assert found.passages[-1].ord == 69


class VectorConnection:
    def __init__(self, *, enough):
        self.calls = []
        self.rows = []
        self.enough = enough

    def execute(self, sql, params):
        self.calls.append((sql, params))
        if "ann_pool" in params:
            self.rows = [{"article_id": 7, "ord": 0}, {"article_id": 7, "ord": 20000}]
        elif "ann_ids" in params:
            self.rows = [{"article_id": 7, "ord": 0, "sim": .8}] if self.enough else []
        else:
            self.rows = [{"article_id": 8, "ord": 0, "sim": .7}]
        return self

    def fetchall(self):
        return self.rows


@pytest.mark.parametrize("enough", [False, True])
def test_ann_pairs_and_exact_fallback_keep_all_evidence_and_entity_gates(monkeypatch, enough):
    c = VectorConnection(enough=enough)
    monkeypatch.setattr(embeddings, "tune_search", lambda *a: None)
    monkeypatch.setattr(embeddings, "model_name", lambda: "current-model")
    result = retrieval._vector_keys(c, "vector", "[1,0]", Scope(source_ids=["fangwei-zsxq"]), 1, company_ids=[23])
    assert result == ({(7, 0): .8} if enough else {(8, 0): .7})
    assert len(c.calls) == (2 if enough else 3)
    for sql, params in c.calls[1:]:
        assert "JOIN article_chunks c" in sql and "projection_md" in sql
        assert "e.model = %(vec_model)s" in sql and "e.text_hash = md5" in sql
        assert "_entity_m.depth <> 'passing'" in sql and "_entity_alias" in sql
        assert params["vec_model"] == "current-model"
    assert c.calls[1][1]["ann_ords"] == [0, 20000]
    assert "JOIN unnest" in c.calls[1][0]


@pytest.mark.parametrize("scope", [Scope(article_id=7), Scope(tree_id=7)])
def test_narrow_vector_scope_uses_only_the_guarded_exact_path(monkeypatch, scope):
    c = VectorConnection(enough=False)
    monkeypatch.setattr(embeddings, "tune_search", lambda *a: None)
    retrieval._vector_keys(c, "vector", "[1,0]", scope, 1)
    assert len(c.calls) == 1 and "ann_pool" not in c.calls[0][1]


def proved_row(aid, text):
    row = current_row(body=text)
    row.update(article_id=aid, article_body_md=text, article_body_text=text, hash=f"hash-{aid}")
    row["raw_meta"]["zsxq_identity"]["projection_md"] = text
    return row


class CurrentConnection(Connection):
    def __init__(self, rows):
        super().__init__(rows[0])
        self.live = {(r["article_id"], r["ord"]): r for r in rows}

    def execute(self, sql, params=None):
        if "FROM unnest" in sql:
            self.current = [copy.deepcopy(self.live[k]) for k in zip(*params, strict=True) if k in self.live]
            return self
        return super().execute(sql, params)


@pytest.mark.parametrize("change,when", [("proof", "probe"), ("text", "probe"),
                                         ("proof", "first-retry"), ("text", "first-retry")])
def test_each_individual_retry_revalidates_proof_and_text(monkeypatch, change, when):
    rows = [proved_row(7, "first owner body"), proved_row(8, "second owner body")]
    c = CurrentConnection(rows)
    sent = []

    def mutate():
        live = c.live[(8, 0)]
        if change == "proof":
            live["raw_meta"]["hidden"] = "withdrawn"
        else:
            live.update(hash="changed", body="new owner body", article_body_md="new owner body")
            live["raw_meta"]["zsxq_identity"]["projection_md"] = "new owner body"

    def embed(texts):
        sent.append(texts)
        if len(texts) == 2:
            raise embeddings.EmbeddingError("synthetic bad batch")
        if texts == ["向量服务检查"] and when == "probe":
            mutate()
        if texts == ["owner title\nfirst owner body"] and when == "first-retry":
            mutate()
        return [[1., 0.] for _ in texts]

    monkeypatch.setattr(embeddings, "embed_texts", embed)
    bad = []
    pairs = embeddings._embed_rows(c, copy.deepcopy(rows), bad)
    assert [r["article_id"] for r, _ in pairs] == [7]
    assert (8, 0) in bad
    assert not any("second owner body" in text for call in sent[1:] for text in call)


def test_proof_revocation_during_embedding_cannot_be_persisted(monkeypatch):
    row = proved_row(7, "current owner body")
    c = CurrentConnection([row])
    pending = [[{"article_id": 7, "ord": 0}], []]
    monkeypatch.setattr(embeddings, "_pending", lambda *a: pending.pop(0))
    monkeypatch.setattr(embeddings, "column_type", lambda *a: "vector")

    def embed(texts):
        c.live[(7, 0)]["raw_meta"]["hidden"] = "withdrawn"
        return [[1., 0.]]

    monkeypatch.setattr(embeddings, "embed_texts", embed)
    assert embeddings.backfill(c) == 0 and c.written == []


def test_coverage_requires_current_model_current_text_and_provenance(monkeypatch):
    class C:
        def execute(self, sql, params):
            assert "e.model = %s" in sql and params == ("current-model",)
            assert "JOIN article_chunks c" in sql and "e.text_hash = md5" in sql
            assert "projection_md" in sql and "suppressed" in sql
            return self

        def fetchone(self):
            return {"done": 1, "total": 3}

    monkeypatch.setattr(embeddings, "column_type", lambda *a: "vector")
    monkeypatch.setattr(embeddings, "model_name", lambda: "current-model")
    assert embeddings.coverage(C()) == (1, 3)


def test_coverage_cache_does_not_cross_database_hosts_or_models(monkeypatch):
    embeddings._state.coverage.clear()
    seen = []
    model = ["one"]
    monkeypatch.setattr(embeddings, "model_name", lambda: model[0])
    monkeypatch.setattr(embeddings, "coverage", lambda c: seen.append(c.info.host) or (1, 2))
    for host in ["first", "first", "second"]:
        embeddings._cached_coverage(SimpleNamespace(info=SimpleNamespace(dbname="same", host=host, port=5432)))
    model[0] = "two"
    embeddings._cached_coverage(SimpleNamespace(info=SimpleNamespace(dbname="same", host="second", port=5432)))
    assert seen == ["first", "second", "second"]


@pytest.mark.parametrize("response", [[], {"data": "wrong"}, {"data": [None]},
    {"data": [{"index": 0, "embedding": [0] * embeddings.DIM}] * 2},
    {"data": [{"index": "0", "embedding": [0] * embeddings.DIM}]}])
def test_malformed_vector_response_cannot_misalign_source_chunks(monkeypatch, response):
    monkeypatch.setattr(embeddings.urllib.request, "urlopen",
                        lambda *a, **kw: io.BytesIO(json.dumps(response).encode()))
    with pytest.raises(embeddings.EmbeddingError):
        embeddings._http_embedder("http://unused.invalid", "test", "", 4)(["first", "second"])


def test_http_disconnect_does_not_leak_service_details(monkeypatch):
    def disconnect(*a, **kw):
        raise http.client.RemoteDisconnected("private service detail")

    monkeypatch.setattr(embeddings.urllib.request, "urlopen", disconnect)
    with pytest.raises(embeddings.EmbeddingError, match="RemoteDisconnected") as error:
        embeddings._http_embedder("http://unused.invalid", "test", "", 4)(["query"])
    assert "private service detail" not in str(error.value)


def test_indexed_response_is_reordered_to_the_source_inputs(monkeypatch):
    response = {"data": [{"index": 1, "embedding": [1] * embeddings.DIM},
                          {"index": 0, "embedding": [0] * embeddings.DIM}]}
    monkeypatch.setattr(embeddings.urllib.request, "urlopen",
                        lambda *a, **kw: io.BytesIO(json.dumps(response).encode()))
    vectors = embeddings._http_embedder("http://unused.invalid", "test", "", 4)(["first", "second"])
    assert vectors[0] == [0] * embeddings.DIM and vectors[1] == [1] * embeddings.DIM


def test_partial_coverage_is_visible_even_above_98_percent(monkeypatch):
    monkeypatch.setattr(embeddings, "enabled", lambda: True)
    monkeypatch.setattr(embeddings, "column_type", lambda c: "vector")
    monkeypatch.setattr(embeddings, "paused", lambda: False)
    monkeypatch.setattr(embeddings, "_cached_coverage", lambda c: (99, 100))
    assert "99/100" in embeddings.status_note(None)


def test_semantic_note_reaches_the_rendered_search_page():
    from pathlib import Path

    from jinja2 import Environment, FileSystemLoader, select_autoescape

    root = Path(__file__).resolve().parents[2] / "src/researchlib/web/templates"
    env = Environment(loader=FileSystemLoader(root), autoescape=select_autoescape())
    env.filters["datetime"] = lambda value: str(value or "")
    html = env.get_template("search.html").render(
        filters={}, sources=[], results=[], query="合成查询", semantic_note="覆盖不全 <script>test</script>")
    assert "覆盖不全 &lt;script&gt;test&lt;/script&gt;" in html
