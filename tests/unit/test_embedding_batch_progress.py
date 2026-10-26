"""Rejected candidates must not starve later proven rows in a backfill run."""

import copy

import pytest
from test_pr65_integration_boundaries import CurrentConnection, proved_row

from researchlib import embeddings


def queue(monkeypatch, rows, *, revoke_after_selection=(), revoke_during_request=()):
    conn = CurrentConnection(rows)
    selections, sent = [], []

    def pending(_conn, size, _model, skip):
        assert len(selections) <= len(rows) + 1, "candidate loop did not make progress"
        stored = {(params[3], params[4]) for params in conn.written}
        selected = [copy.deepcopy(row) for key, row in conn.live.items()
                    if key not in skip and key not in stored][:size]
        selections.append([(r["article_id"], r["ord"]) for r in selected])
        for key in revoke_after_selection:
            conn.live[key]["raw_meta"]["hidden"] = "withdrawn"
        return selected

    def embed(texts):
        sent.extend(texts)
        for key in revoke_during_request:
            conn.live[key]["raw_meta"]["hidden"] = "withdrawn"
        return [[1., 0.] for _ in texts]

    monkeypatch.setattr(embeddings, "column_type", lambda _: "vector")
    monkeypatch.setattr(embeddings, "model_name", lambda: "offline-stub")
    monkeypatch.setattr(embeddings, "_pending", pending)
    monkeypatch.setattr(embeddings, "embed_texts", embed)
    return conn, selections, sent


@pytest.mark.parametrize("batch", [1, 2])
def test_rejected_first_candidate_does_not_starve_a_later_valid_row(monkeypatch, batch):
    rows = [proved_row(7, "revoked writing"), proved_row(8, "current writing")]
    conn, selections, sent = queue(monkeypatch, rows, revoke_after_selection=[(7, 0)])
    skipped = []
    assert embeddings.backfill(conn, batch=batch, limit=1, skipped=skipped) == 1
    assert skipped == [(7, 0)]
    assert sent == ["owner title\ncurrent writing"]
    assert [(p[3], p[4]) for p in conn.written] == [(8, 0)]
    assert selections == [[(7, 0)], [(8, 0)]]


def test_mixed_batch_reports_rejected_keys_and_advances(monkeypatch):
    rows = [proved_row(7, "revoked writing"), proved_row(8, "first current"), proved_row(9, "later current")]
    conn, selections, sent = queue(monkeypatch, rows, revoke_after_selection=[(7, 0)])
    skipped = []
    assert embeddings.backfill(conn, batch=2, skipped=skipped) == 2
    assert skipped == [(7, 0)] and selections[-1] == []
    assert sent == ["owner title\nfirst current", "owner title\nlater current"]


def test_all_rejected_candidates_terminate_without_a_model_request(monkeypatch):
    rows = [proved_row(i, "revoked writing") for i in (7, 8, 9)]
    conn, selections, sent = queue(monkeypatch, rows, revoke_after_selection=[(i, 0) for i in (7, 8, 9)])
    skipped = []
    assert embeddings.backfill(conn, batch=2, skipped=skipped) == 0
    assert skipped == [(7, 0), (8, 0), (9, 0)] and selections[-1] == []
    assert not sent and not conn.written


def test_revocation_during_request_does_not_store_or_starve_later_rows(monkeypatch):
    rows = [proved_row(7, "first current"), proved_row(8, "later current")]
    conn, selections, _sent = queue(monkeypatch, rows, revoke_during_request=[(7, 0)])
    skipped = []
    assert embeddings.backfill(conn, batch=1, skipped=skipped) == 1
    assert skipped == [(7, 0)] and selections[-1] == []
    assert [(p[3], p[4]) for p in conn.written] == [(8, 0)]


def test_empty_pending_queue_is_not_an_infinite_loop(monkeypatch):
    conn, selections, sent = queue(monkeypatch, [proved_row(7, "unused")])
    conn.live.clear()
    assert embeddings.backfill(conn) == 0
    assert selections == [[]] and not sent
