"""知识星球旧档重放共享作者身份规则，避免弱线索改写已知账户和公司观点。"""

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from researchlib.ingest.model import ParsedDoc
from researchlib.ingest.store import store_documents
from researchlib.posters import backfill_posters
from researchlib.sources import load_sources


@pytest.mark.parametrize("action", ["backfill", "unchanged", "updated"])
@pytest.mark.parametrize(("old", "replayed", "expected", "keep_view"), [
    ({"poster_id": "222", "poster_name": "方伟"}, {"poster_name": "方伟"}, ("222", "方伟", "guest"), False),
    ({"poster_id": "111", "poster_name": "王小明"}, {"poster_name": "王小明"}, ("111", "王小明", "owner"), True),
    ({"poster_id": "111", "poster_name": "方伟"}, {"poster_id": "111"}, ("111", "方伟", "owner"), True),
    ({"poster_id": "111", "poster_name": "方伟"}, {"poster_id": "222", "poster_name": "李四"},
     ("111", "方伟", "owner"), True),
    ({"poster_name": "方伟"}, {"poster_id": "222", "poster_name": "方伟"}, ("222", "方伟", "guest"), False),
    ({"poster_id": "111"}, {"poster_id": "222", "poster_name": "李四"}, ("111", None, "owner"), True),
    ({"poster_id": "111", "poster_name": "方伟"}, {"poster_id": "111", "poster_name": "新昵称"},
     ("111", "新昵称", "owner"), True),
    ({"poster_id": "111", "poster_name": "方伟"}, {}, ("111", "方伟", "owner"), True),
    ({"poster_name": "王小明"}, {"poster_name": "方伟"}, (None, "王小明", "guest"), False),
], ids=["guest-matching-owner-name", "owner-using-alias", "name-absent", "different-account",
        "stronger-id-corrects-role", "other-account-name-not-copied", "same-id-renamed",
        "poster-missing", "nickname-conflict"])
def test_partial_poster_replay(conn, settings, action, old, replayed, expected, keep_view):
    source = replace(next(s for s in load_sources(settings.sources_path) if s.id == "fangwei-zsxq"),
                     owner_ids=("111",))
    doc = ParsedDoc("333", "现金流", datetime(2025, 1, 1, tzinfo=UTC),
                    "贵州茅台的经营现金流增长，销售回款增加。", {"topic_id": "333", **old}, [])

    def store(item):
        report = store_documents(conn, source=source, docs=[item], data_dir=settings.data_dir,
                                 archive_files=1, images_in_archive=0, errors=[])
        assert not report.errors
        return report

    assert store(doc).inserted == 1
    article = conn.execute("SELECT id, content_hash, raw_meta FROM articles WHERE external_id='333'").fetchone()
    if article["raw_meta"]["poster_role"] == "owner":
        conn.execute(
            """INSERT INTO company_views(article_id, company_id, content_hash, prompt_version, judgment, stance)
               SELECT %s, id, %s, 1, '观察销售回款', 'positive' FROM companies WHERE name_zh='贵州茅台'""",
            (article["id"], article["content_hash"]),
        )
        conn.commit()
    incoming = replace(doc, raw_meta={"topic_id": "333", **replayed})
    if action == "updated":
        incoming = replace(incoming, body_md=doc.body_md + " 更新后的销售收入增加。")
    for attempt in range(2):
        if action == "backfill":
            stats = backfill_posters(conn, source, [incoming])
            if attempt:
                assert stats["updated"] == 0 and stats["views_cleared"] == 0
        else:
            report = store(incoming)
            if action == "updated" and attempt == 0:
                assert report.updated == 1
            else:
                assert report.unchanged == 1
        meta = conn.execute("SELECT raw_meta FROM articles WHERE id=%s", (article["id"],)).fetchone()["raw_meta"]
        count = conn.execute("SELECT count(*) AS n FROM company_views WHERE article_id=%s", (article["id"],))
        assert count.fetchone()["n"] == int(keep_view)
        assert tuple(meta.get(field) for field in ("poster_id", "poster_name", "poster_role")) == expected
