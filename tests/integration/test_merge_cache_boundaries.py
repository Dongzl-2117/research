"""Same-topic grouping cannot certify changed or unproven model inputs."""

import pytest
from test_duplicates import _store, _topic_doc

from researchlib.duplicates import _hand_over_views
from researchlib.ingest.model import ParsedDoc
from researchlib.views import PROMPT_VERSION


@pytest.mark.parametrize("drift", [None, "hash", "cache", "title", "body", "ocr", "proof", "source"])
def test_topic_view_transfer_requires_identical_proven_inputs(conn, settings, drift):
    body = "腾讯的视频号广告需要结合用户时长和商家投放评估。"
    _store(conn, settings, "fangwei-zsxq", [_topic_doc("old-copy", body), _topic_doc("new-copy", body)])
    ids = {r["external_id"]: r["id"] for r in conn.execute("SELECT id, external_id FROM articles")}
    old_id, current_id = ids["old-copy"], ids["new-copy"]
    company = conn.execute("SELECT id FROM companies WHERE name_zh = '腾讯'").fetchone()["id"]
    conn.execute(
        "INSERT INTO company_views (article_id, company_id, content_hash, prompt_version, judgment, stance) "
        "SELECT id, %s, content_hash, %s, 'synthetic view', 'neutral' FROM articles WHERE id = %s",
        (company, PROMPT_VERSION, old_id),
    )
    before = conn.execute("SELECT content_hash FROM company_views WHERE article_id = %s", (old_id,)).fetchone()
    changes = {
        "hash": "content_hash = 'changed'", "title": "title = 'different title'",
        "body": "body_md = 'different body'", "ocr": "ocr_hash = 'different OCR input'",
        "proof": "raw_meta = '{}'::jsonb", "source": "source_id = 'fangwei-mp'",
    }
    if drift == "cache":
        conn.execute("UPDATE company_views SET content_hash = 'stale' WHERE article_id = %s", (old_id,))
        before = {"content_hash": "stale"}
    elif drift:
        conn.execute(f"UPDATE articles SET {changes[drift]} WHERE id = %s", (current_id,))
    assert _hand_over_views(conn, old_id, current_id) == (1 if drift is None else 0)
    view = conn.execute("SELECT article_id, content_hash FROM company_views").fetchone()
    assert view == {"article_id": current_id if drift is None else old_id, "content_hash": before["content_hash"]}


@pytest.mark.parametrize("field", ["title", "date"])
def test_owner_title_or_date_change_does_not_refresh_a_cached_view(conn, settings, field):
    doc = _topic_doc("original", "腾讯的视频号广告需要结合用户时长和商家投放评估。")
    _store(conn, settings, "fangwei-zsxq", [doc])
    row = conn.execute("SELECT id, content_hash FROM articles").fetchone()
    company = conn.execute("SELECT id FROM companies WHERE name_zh = '腾讯'").fetchone()["id"]
    conn.execute(
        "INSERT INTO company_views (article_id, company_id, content_hash, prompt_version, judgment, stance) "
        "VALUES (%s,%s,%s,%s,'synthetic view','neutral')",
        (row["id"], company, row["content_hash"], PROMPT_VERSION),
    )
    conn.commit()
    changed = ParsedDoc(doc.external_id, "changed title" if field == "title" else doc.title,
                        doc.published_at.replace(day=13) if field == "date" else doc.published_at,
                        doc.body_md, doc.raw_meta, [])
    assert _store(conn, settings, "fangwei-zsxq", [changed]).updated == 1
    current = conn.execute("SELECT content_hash FROM articles WHERE id = %s", (row["id"],)).fetchone()
    view = conn.execute("SELECT content_hash FROM company_views WHERE article_id = %s", (row["id"],)).fetchone()
    assert current["content_hash"] != row["content_hash"]
    assert view["content_hash"] == row["content_hash"]
