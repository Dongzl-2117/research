"""存量段永平文章的回扫：洗页码垃圾，博友回复和非论证不进观点。"""

from datetime import datetime
from zoneinfo import ZoneInfo

from researchlib.ingest.duanyongping.reclassify import reclassify_source
from researchlib.ingest.model import ParsedDoc
from researchlib.ingest.store import store_documents
from researchlib.sources import load_sources

SHANGHAI = ZoneInfo("Asia/Shanghai")


def _source(settings):
    return next(source for source in load_sources(settings.sources_path) if source.id == "duanyongping-blog")


def _doc(external_id: str, title: str, body: str, voice: str = "all") -> ParsedDoc:
    return ParsedDoc(
        external_id=external_id,
        title=title,
        published_at=datetime(2010, 1, 1, 8, 0, tzinfo=SHANGHAI),
        body_md=body,
        raw_meta={"format": "markdown", "author_voice": voice},
        image_refs=[],
    )


def test_reclassify_scrubs_junk_and_drops_non_arguments(conn, settings):
    store_documents(
        conn,
        source=_source(settings),
        docs=[
            _doc(
                "dyp-junk",
                "茅台的生意",
                "## 读者问\n\n茅台怎么样？\n\n## 段永平\n\n茅台的生意模式很好。file:///H.HTM 我还拿着。\n",
                voice="段永平",
            ),
            _doc("dyp-method", "能力圈", "不要太保守，做对的事情，把事情做对。\n"),
            _doc("dyp-guest", "回复段永平", "茅台我看好，生意模式没问题，我已经买了不少。\n", voice="段永平"),
            _doc("dyp-thanks", "周末", "感谢茅台的朋友，周末一起吃饭，聊得很开心。\n"),
        ],
        data_dir=settings.data_dir,
        archive_files=4,
        images_in_archive=0,
        errors=[],
    )
    report = reclassify_source(conn)
    assert report["counts"]["scrubbed"] >= 1
    assert report["counts"]["guest"] >= 1

    junk = conn.execute(
        "SELECT body_md, body_text, raw_meta FROM articles WHERE external_id = 'dyp-junk'"
    ).fetchone()
    assert "file://" not in junk["body_md"]
    assert "H.HTM" not in junk["body_text"]
    assert "我还拿着" in junk["body_text"]
    assert junk["raw_meta"].get("off_views") is not True
    mention = conn.execute(
        """
        SELECT c.name_zh
        FROM article_mentions m
        JOIN articles a ON a.id = m.article_id
        JOIN companies c ON c.id = m.company_id
        WHERE a.external_id = 'dyp-junk'
        """
    ).fetchone()
    assert mention["name_zh"] == "贵州茅台"

    method = conn.execute("SELECT raw_meta FROM articles WHERE external_id = 'dyp-method'").fetchone()
    assert method["raw_meta"]["off_views"] is True
    assert method["raw_meta"]["suppressed"] == "no_judgment"
    assert method["raw_meta"]["off_timeline"] is True
    guest = conn.execute("SELECT raw_meta FROM articles WHERE external_id = 'dyp-guest'").fetchone()
    assert guest["raw_meta"]["poster_role"] == "guest"
    assert guest["raw_meta"]["off_views"] is True
    guest_mentions = conn.execute(
        """
        SELECT count(*) AS n
        FROM article_mentions m
        JOIN articles a ON a.id = m.article_id
        WHERE a.external_id = 'dyp-guest'
        """
    ).fetchone()
    assert guest_mentions["n"] == 0
    thanks = conn.execute(
        """
        SELECT count(*) AS n
        FROM article_mentions m
        JOIN articles a ON a.id = m.article_id
        WHERE a.external_id = 'dyp-thanks'
        """
    ).fetchone()
    assert thanks["n"] == 0
    thanks = conn.execute("SELECT raw_meta FROM articles WHERE external_id = 'dyp-thanks'").fetchone()
    assert thanks["raw_meta"]["off_views"] is True
    assert thanks["raw_meta"]["suppressed"] == "no_judgment"
    assert thanks["raw_meta"]["off_timeline"] is True
