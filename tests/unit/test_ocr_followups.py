"""识图写完后，短正文的摘要和星球标题要入队。不连库、不调模型。"""

from __future__ import annotations

from researchlib import digest as digest_mod
from researchlib import followups
from researchlib import ocr as ocr_mod
from researchlib.ocr import SHORT_BODY_CHARS
from researchlib.zsxq_repair import prefer_queued_rows
from researchlib.zsxq_title import title_needs_ai

SHELL = "正文除标题「图片」外没有可读文字，无法提炼作者的核心判断及其依据"
GOOD = "关于携程与Booking"


class _Result:
    def __init__(self, rows=None):
        self._rows = rows or []

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _Conn:
    def __init__(self):
        self.writes: list[tuple] = []
        self.commits = 0

    def execute(self, sql, params=None):
        compact = " ".join(sql.split())
        if "INSERT INTO article_followups" in compact:
            self.writes.append(params)
            return _Result()
        if "SELECT DISTINCT article_id" in compact:
            return _Result([{"article_id": 24012}])
        return _Result()

    def commit(self):
        self.commits += 1


def test_shell_title_needs_ai_and_a_finished_title_does_not():
    assert title_needs_ai(SHELL)
    assert title_needs_ai("本文正文为空")
    assert title_needs_ai(GOOD) is False


def test_short_body_hash_change_requeues_without_reading_the_title():
    """ocr_hash 变了就够。不把标题先拿去 title_needs_ai，否则空壳句子进不了队。"""
    kinds = followups.followup_kinds(
        source_id="fangwei-zsxq",
        body_len=2,
        old_hash="",
        new_hash="abc123",
    )
    assert kinds == [followups.KIND_DIGEST, followups.KIND_TITLE]
    assert title_needs_ai(GOOD) is False
    assert title_needs_ai(SHELL) is True
    assert (
        followups.followup_kinds(
            source_id="fangwei-mp",
            body_len=2,
            old_hash="",
            new_hash="abc123",
        )
        == [followups.KIND_DIGEST]
    )
    assert (
        followups.followup_kinds(
            source_id="fangwei-zsxq",
            body_len=SHORT_BODY_CHARS,
            old_hash="",
            new_hash="abc123",
        )
        == []
    )
    assert (
        followups.followup_kinds(
            source_id="fangwei-zsxq",
            body_len=2,
            old_hash="abc123",
            new_hash="abc123",
        )
        == []
    )
    assert (
        followups.followup_kinds(
            source_id="fangwei-zsxq",
            body_len=2,
            old_hash="",
            new_hash="",
        )
        == []
    )


def test_pending_order_puts_ocr_ahead_of_missing_digests():
    order = digest_mod.PENDING_ORDER
    assert order.index("article_followups") < order.index("content_hash <> a.content_hash")
    assert order.index("ocr_hash IS DISTINCT FROM a.ocr_hash") < order.index("content_hash <> a.content_hash")


def test_run_ocr_requeues_digest_and_title_when_short_body_hash_changes(tmp_path, monkeypatch):
    image = tmp_path / "images" / "a.png"
    image.parent.mkdir()
    image.write_bytes(b"\x89PNG\r\n\x1a\n")
    snaps = iter(
        [
            {"id": 24012, "source_id": "fangwei-zsxq", "ocr_hash": "", "body_len": 2},
            {"id": 24012, "source_id": "fangwei-zsxq", "ocr_hash": "newhash", "body_len": 2},
        ]
    )
    monkeypatch.setattr(
        ocr_mod,
        "pending_images",
        lambda *args, **kwargs: [{"content_hash": "ab", "storage_path": "a.png"}],
    )
    monkeypatch.setattr(ocr_mod, "_ocr_snapshot", lambda conn, article_id: next(snaps))
    monkeypatch.setattr(ocr_mod, "refresh_article_ocr", lambda conn, article_id: "newhash")
    conn = _Conn()
    report = ocr_mod.run_ocr(conn, tmp_path, 5, lambda _path: "方伟：茅台大概率比公牛好")
    kinds = {params[1] for params in conn.writes}
    assert kinds == {"digest", "zsxq_title"}
    assert all(params[0] == 24012 and params[2] == "ocr_hash" for params in conn.writes)
    assert report["followups"]["digest"] == [24012]
    assert report["followups"]["zsxq_title"] == [24012]
    assert report["ocr_changed"] == [24012]
    assert conn.commits >= 1


def test_queued_title_rows_jump_the_scan():
    rows = [{"id": 1}, {"id": 24012}, {"id": 3}]
    ordered = prefer_queued_rows(rows, [24012])
    assert [row["id"] for row in ordered] == [24012, 1, 3]
    assert prefer_queued_rows(rows, []) == rows


def test_drain_rewrites_only_the_ids_just_queued(monkeypatch):
    seen = {}

    def digests(_conn, _settings, article_ids):
        seen["digest"] = list(article_ids)
        return len(article_ids)

    def titles(_conn, _settings, article_ids):
        seen["titles"] = list(article_ids)
        return len(article_ids)

    monkeypatch.setattr("researchlib.llm.llm_configured", lambda _settings: True)
    monkeypatch.setattr(followups, "_drain_digests", digests)
    monkeypatch.setattr(followups, "_drain_titles", titles)
    done = followups.drain_ocr_followups(
        None,
        object(),
        {"digest": [24012, 24013], "zsxq_title": [24012]},
    )
    assert seen == {"digest": [24012, 24013], "titles": [24012]}
    assert done == {"digest": 2, "titles": 1}


def test_drain_leaves_the_queue_when_llm_is_missing(monkeypatch):
    monkeypatch.setattr("researchlib.llm.llm_configured", lambda _settings: False)

    def boom(*_args, **_kwargs):
        raise AssertionError("没有凭证不该打模型")

    monkeypatch.setattr(followups, "_drain_digests", boom)
    monkeypatch.setattr(followups, "_drain_titles", boom)
    done = followups.drain_ocr_followups(None, object(), {"digest": [24012], "zsxq_title": [24012]})
    assert done == {"digest": 0, "titles": 0}
