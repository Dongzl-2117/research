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
