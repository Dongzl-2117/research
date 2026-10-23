from copy import deepcopy

from researchlib.answer_provenance import (
    INPUTS_KEY,
    OUTLINE_KEY,
    fingerprint_article,
    provenance_article_ids,
    public_scope,
    snapshot_input_provenance,
    verify_input_provenance,
)
from researchlib.eligibility import TRUSTED_OWNERS
from researchlib.sources import Source
from researchlib.web import routes


def article(article_id=1):
    return {"id": article_id, "source_id": "notes", "title": f"题目{article_id}",
            "body_md": f"可信正文{article_id}", "body_text": f"可信正文{article_id}",
            "content_hash": f"hash-{article_id}", "ocr_hash": "", "raw_meta": {}}


def passage(article_id=1, *, lang="", cited=False):
    return {"n": article_id, "article_id": article_id, "ord": 1, "source_id": "notes", "lang": lang,
            "title": f"题目{article_id}", "heading": "", "text": f"可信正文{article_id}",
            "before": "", "after": "", "author": "作者", "date": "2026-10-23", "cited": cited}


def source():
    return Source("notes", "笔记", "作者", "markdown", True)


def test_complete_immutable_inputs_keep_new_answer_visible(monkeypatch):
    rows = [article(1), article(2)]
    passages = [passage(1, cited=True), passage(2)]
    receipt = snapshot_input_provenance(rows, passages)
    assert receipt and provenance_article_ids(receipt) == [1, 2]
    saved = {"answer": "仅引用第一篇[1]。", "citations": passages, "scope": {"source_ids": ["notes"], **receipt},
             "status": "done"}
    monkeypatch.setattr(routes, "_answer_input_articles", lambda *args: rows)
    assert routes._safe_saved_answer(None, saved, [source()]) is saved
    assert public_scope(saved["scope"]) == {"source_ids": ["notes"]}


def test_changed_uncited_original_input_hides_entire_answer(monkeypatch):
    rows = [article(1), article(2)]
    passages = [passage(1, cited=True), passage(2)]
    saved = {"answer": "第一篇的结论[1]。", "citations": passages, "scope": snapshot_input_provenance(rows, passages),
             "status": "done"}
    changed = deepcopy(rows)
    changed[1]["body_md"] = "后来修改的第二篇"
    changed[1]["content_hash"] = "new-hash"
    monkeypatch.setattr(routes, "_answer_input_articles", lambda *args: changed)
    masked = routes._safe_saved_answer(None, saved, [source()])
    assert masked["answer"] == "" and masked["citations"] == [] and masked["cache_blocked"]
    assert saved["answer"] == "第一篇的结论[1]。"


def test_legacy_guest_answer_cannot_resurface_after_canonical_reingest(monkeypatch):
    _group, owner = TRUSTED_OWNERS["fangwei-zsxq"]
    trusted_now = {**article(), "source_id": "fangwei-zsxq", "raw_meta": {"poster_id": owner}}
    assert fingerprint_article(trusted_now)  # The current base text can be trusted independently.
    saved = {"answer": "过去网友正文被错误写成星主观点。", "citations": [passage()],
             "scope": {"source_ids": ["fangwei-zsxq"]}, "status": "done"}

    def forbidden(*args):
        raise AssertionError("current trust must not replace missing historical input evidence")

    monkeypatch.setattr(routes, "_answer_input_articles", forbidden)
    masked = routes._safe_saved_answer(None, saved, [source()])
    assert masked["answer"] == "" and masked["citations"] == []
    assert "原始输入" in masked["error"]


def test_changing_or_trimming_saved_passages_invalidates_receipt():
    rows = [article(1), article(2)]
    passages = [passage(1, cited=True), passage(2)]
    receipt = snapshot_input_provenance(rows, passages)
    assert verify_input_provenance(receipt, rows, passages) == ""
    changed = deepcopy(passages)
    changed[0]["text"] = "替换后的原文"
    assert verify_input_provenance(receipt, rows, changed)
    assert verify_input_provenance(receipt, rows, passages[:1])


def test_outline_only_input_articles_also_have_immutable_receipts():
    rows = [article(1), article(2)]
    passages = [passage(1)]
    outline = [{"article_id": 2, "title": "题目2"}]
    receipt = snapshot_input_provenance(rows, passages, outline=outline)
    assert receipt and set(receipt[INPUTS_KEY]) == {"1", "2"} and receipt[OUTLINE_KEY] == outline
    assert verify_input_provenance(receipt, rows, passages) == ""
    changed = deepcopy(rows)
    changed[1]["title"] = "后来的目录标题"
    assert verify_input_provenance(receipt, changed, passages)
    assert snapshot_input_provenance(rows, passages, outline=[{"article_id": 2, "title": "错误标题"}]) is None


def test_old_ocr_roles_cannot_receive_new_answer_provenance():
    row = article()
    row["ocr_hash"] = "ocr-hash"
    row["raw_meta"]["ocr_role_version"] = 2
    passages = [passage(lang="ocr")]
    assert snapshot_input_provenance([row], passages) is None
    row["raw_meta"]["ocr_role_version"] = 3
    receipt = snapshot_input_provenance([row], passages)
    assert receipt and verify_input_provenance(receipt, [row], passages) == ""
    changed = deepcopy(row)
    changed["ocr_hash"] = "later-ocr-hash"
    assert verify_input_provenance(receipt, [changed], passages)


def test_source_identity_and_visibility_changes_invalidate_input():
    row = article()
    passages = [passage()]
    receipt = snapshot_input_provenance([row], passages)
    for changed in ({**row, "source_id": "different"}, {**row, "raw_meta": {"hidden": "held"}}):
        assert verify_input_provenance(receipt, [changed], passages)
    bad = deepcopy(passages)
    bad[0]["source_id"] = "unrelated"
    assert snapshot_input_provenance([row], bad) is None


def test_bad_manifest_and_missing_complete_inputs_fail_closed():
    rows = [article()]
    passages = [passage()]
    receipt = snapshot_input_provenance(rows, passages)
    assert receipt
    malformed = deepcopy(receipt)
    malformed[INPUTS_KEY]["1"]["langs"] = [{}]
    assert provenance_article_ids(malformed) is None
    assert verify_input_provenance({}, rows, passages)
    assert verify_input_provenance(receipt, [], passages)
    assert snapshot_input_provenance(rows + rows, passages) is None
    assert snapshot_input_provenance(rows, [{**passages[0], "lang": {}}]) is None
