"""拒绝后的待译队列：冷却写在库里，重启进程也不会立刻再花钱。"""

from datetime import UTC, datetime, timedelta

import pytest
from test_bilingual_provenance_db import article

from researchlib.translate import service
from researchlib.translate.provenance import ALIGNMENT_VERSION, AlignmentError
from researchlib.translate.service import REJECT_RETRY_BUDGET, list_pending_english, record_translation_failure


def _reject(**_kwargs):
    raise AlignmentError("slot-0-numbers")


def test_a_fresh_alignment_reject_leaves_the_pending_queue(conn, settings, monkeypatch):
    article_id = article(conn, settings)
    monkeypatch.setattr(service, "chat_json", _reject)
    with pytest.raises(AlignmentError):
        service.translate_article(conn, article_id, data_dir=settings.data_dir)
    conn.rollback()
    record_translation_failure(conn, article_id, AlignmentError("slot-0-numbers"))
    assert article_id not in list_pending_english(conn, source_id="stratechery", limit=50)

    row = conn.execute(
        "SELECT content_hash FROM articles WHERE id = %s", (article_id,),
    ).fetchone()
    conn.execute(
        "UPDATE translation_attempts SET alignment_version = %s WHERE article_id = %s",
        (ALIGNMENT_VERSION - 1, article_id),
    )
    conn.commit()
    assert article_id in list_pending_english(conn, source_id="stratechery", limit=50)

    conn.execute(
        """
        UPDATE translation_attempts
        SET alignment_version = %s, created_at = %s
        WHERE article_id = %s
        """,
        (ALIGNMENT_VERSION, datetime.now(UTC) - timedelta(hours=13), article_id),
    )
    conn.commit()
    assert article_id in list_pending_english(conn, source_id="stratechery", limit=50)

    conn.execute(
        """
        INSERT INTO translation_attempts
          (article_id, status, reason, content_hash, alignment_version, created_at)
        VALUES (%s, 'rejected', 'slot-0-numbers', %s, %s, %s)
        """,
        (
            article_id,
            row["content_hash"],
            ALIGNMENT_VERSION,
            datetime.now(UTC) - timedelta(hours=13),
        ),
    )
    conn.commit()
    assert REJECT_RETRY_BUDGET == 2
    assert article_id not in list_pending_english(conn, source_id="stratechery", limit=50)

    conn.execute("UPDATE articles SET content_hash = %s WHERE id = %s", ("hash-after-edit", article_id))
    conn.commit()
    assert article_id in list_pending_english(conn, source_id="stratechery", limit=50)


def test_queue_can_translate_new_revision_after_source_changes_during_request(conn, settings, monkeypatch, capsys):
    from test_bilingual_provenance_db import BODY, model

    from researchlib.db import connect
    from researchlib.translate import cli, inventory

    article_id = article(conn, settings)
    edited = BODY + "<p>First paragraph 30 dollars.</p>"
    calls = []

    def model_during_edit(**kwargs):
        if not calls:
            with connect(settings) as importer:
                assert article(importer, settings, body=edited) == article_id
        calls.append(kwargs["user"]["segments"])
        return model(**kwargs)

    monkeypatch.setattr(cli.Settings, "from_env", staticmethod(lambda: settings))
    monkeypatch.setattr(cli, "llm_provider", lambda: "deepseek")
    monkeypatch.setattr(cli, "deepseek_api_key", lambda: "sk-test")
    monkeypatch.setattr(service, "chat_json", model_during_edit)
    args = ["--source", "stratechery", "--limit", "1", "--anytime"]
    assert cli.main(args) == 1
    assert "source-changed-during-request" in capsys.readouterr().err
    current = conn.execute("SELECT content_hash, body_md FROM articles WHERE id=%s", (article_id,)).fetchone()
    assert "30 dollars" in current["body_md"]
    assert conn.execute("SELECT count(*) AS n FROM article_translations").fetchone()["n"] == 0
    assert list_pending_english(conn, source_id="stratechery") == [article_id]
    assert conn.execute("SELECT count(*) AS n FROM translation_attempts").fetchone()["n"] == 0

    # 老版本可能已将两次失效回执绑定在当前正文上；重启之后，队列与盘点都应把它交回。
    for reason in ("source-changed-during-request", "canonical-source-changed-during-request"):
        conn.execute(
            "INSERT INTO translation_attempts (article_id, status, reason, content_hash, alignment_version)"
            " VALUES (%s, 'rejected', %s, %s, %s)",
            (article_id, reason, current["content_hash"], ALIGNMENT_VERSION),
        )
    conn.commit()
    assert list_pending_english(conn, source_id="stratechery") == [article_id]
    report = inventory.collect(conn, settings)["ids"]
    assert report["pending-now"] == [article_id] and not report["stopped"] and not report["cooldown"]
    assert cli.main(args) == 0
    translated = conn.execute("SELECT content_hash, body_html, complete FROM article_translations").fetchone()
    assert translated["content_hash"] == current["content_hash"] and translated["complete"]
    assert "30美元" in translated["body_html"]
