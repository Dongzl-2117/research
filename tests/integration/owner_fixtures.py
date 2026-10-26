"""Explicit synthetic owner evidence for tests, never a production bypass.

Only call owner_doc for a fixture declared to represent a verified owner post.
Guest/unknown and historical rows must keep their original missing evidence.
"""

from psycopg.types.json import Jsonb

from researchlib.eligibility import IDENTITY_KEY, TRUSTED_OWNERS, identity_for_topic
from researchlib.ingest.store import _body_text, store_documents
from researchlib.seed import sample_documents, write_sample_png
from researchlib.sources import load_sources
from researchlib.textutil import content_hash


def owner_doc(doc, replies=()):
    if IDENTITY_KEY in doc.raw_meta:
        return doc
    group, account = TRUSTED_OWNERS["fangwei-zsxq"]
    topic = {"topic_id": doc.external_id, "group": {"group_id": group},
             "talk": {"owner": {"user_id": account}, "text": doc.body_md},
             "comments": [{"comment_id": str(i), "owner": {"user_id": account}, "text": reply}
                          for i, reply in enumerate(replies, 1)]}
    doc.raw_meta = {**doc.raw_meta, "synthetic_fixture": True,
                    IDENTITY_KEY: identity_for_topic(topic, "fangwei-zsxq")}
    return doc


def insert_legacy(conn, source_id, doc):
    """Seed an old raw row directly; do not pretend the new importer admitted it."""
    conn.execute(
        "INSERT INTO articles (source_id,external_id,title,published_at,body_md,body_text,content_hash,raw_meta) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
        (source_id, doc.external_id, doc.title, doc.published_at, doc.body_md,
         _body_text(doc.title, doc.body_md), content_hash(doc.title, doc.body_md, doc.published_at),
         Jsonb(doc.raw_meta)),
    )
    conn.commit()


def load_browsing_sample(conn, settings):
    """UI corpus: declared owner fixtures plus an active non-star source.

    Production sample_documents has no star-account evidence. Keep its normal
    rejection intact; never make sample=true grant owner eligibility.
    """
    image = settings.data_dir / "sample/battery.png"
    write_sample_png(image)
    groups = sample_documents(image)
    groups["fangwei-zsxq"] = [owner_doc(doc) for doc in groups["fangwei-zsxq"]]
    groups["gudujinhuati"] = groups.pop("chengfengpolang")
    sources = {source.id: source for source in load_sources(settings.sources_path)}
    for source_id, docs in groups.items():
        report = store_documents(conn, source=sources[source_id], docs=docs, data_dir=settings.data_dir,
                                 archive_files=len(docs), images_in_archive=0, errors=[])
        assert not report.errors and report.rejected == 0
