"""来源的短帖门槛（min_chars）改了：启动时按新门槛重标已入库的帖子；隐藏的不译。"""

import dataclasses
from datetime import datetime
from zoneinfo import ZoneInfo

from psycopg.types.json import Jsonb

from researchlib.empty_answer import HIDDEN, LENGTH_STATE_PREFIX, SHORT_POST, SOURCE_PAUSED, refresh_length_floors
from researchlib.ingest.model import ParsedDoc
from researchlib.ingest.store import store_documents
from researchlib.sources import load_sources
from researchlib.translate.service import list_pending_english

SHANGHAI = ZoneInfo("Asia/Shanghai")
# 编的英文：长的一篇六千多字符，短的是几百词的每周汇总那种。
LONG = "\n\n".join(
    " ".join(f"Paragraph {index} sentence {line} explains the aggregation argument in detail." for line in range(8))
    for index in range(16)
)
ROUNDUP = "This week in the newsletter: a piece on bundling, an interview, and a short update on chips."


def _doc(external_id, title, body):
    return ParsedDoc(
        external_id=external_id,
        title=title,
        published_at=datetime(2025, 3, 12, 9, 0, tzinfo=SHANGHAI),
        body_md=body,
        raw_meta={},
        image_refs=[],
    )


def _hidden(conn, external_id):
    row = conn.execute("SELECT raw_meta FROM articles WHERE external_id = %s", (external_id,)).fetchone()
    return (row["raw_meta"] or {}).get(HIDDEN)


def test_new_floor_rehides_old_posts_and_hidden_ones_are_not_translated(conn, settings):
    sources = {source.id: source for source in load_sources(settings.sources_path)}
    strat = sources["stratechery"]
    assert strat.min_chars == 5500
    store_documents(
        conn,
        source=strat,
        docs=[_doc("2025/long.md", "Long", LONG), _doc("2025/roundup.md", "Home At Last", ROUNDUP)],
        data_dir=settings.data_dir,
        archive_files=2,
        images_in_archive=0,
        errors=[],
    )
    # 导入时就按门槛标好了。
    assert _hidden(conn, "2025/long.md") is None
    assert _hidden(conn, "2025/roundup.md") == SHORT_POST

    # 模拟门槛上线之前入库的数据：没有标记，库里记的门槛是 0。
    conn.execute("UPDATE articles SET raw_meta = raw_meta - 'hidden'")
    conn.execute(
        """
        INSERT INTO app_state (key, value, updated_at) VALUES (%s, '0', now())
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
        """,
        (LENGTH_STATE_PREFIX + strat.id,),
    )
    conn.commit()
    roundup_id = conn.execute("SELECT id FROM articles WHERE external_id = '2025/roundup.md'").fetchone()["id"]
    assert roundup_id in list_pending_english(conn, limit=10, source_id=strat.id)

    assert refresh_length_floors(conn, [strat]) == {strat.id: 1}
    conn.commit()
    assert _hidden(conn, "2025/roundup.md") == SHORT_POST
    assert _hidden(conn, "2025/long.md") is None
    # 隐藏的不进待译。
    assert roundup_id not in list_pending_english(conn, limit=10, source_id=strat.id)
    # 门槛没变，再跑一遍什么都不改。
    assert refresh_length_floors(conn, [strat]) == {}

    # 门槛去掉：短帖标记收回；别的原因隐藏的不动。
    conn.execute(
        "UPDATE articles SET raw_meta = raw_meta || %s WHERE external_id = '2025/long.md'",
        (Jsonb({HIDDEN: SOURCE_PAUSED}),),
    )
    conn.commit()
    assert refresh_length_floors(conn, [dataclasses.replace(strat, min_chars=0)]) == {strat.id: 1}
    conn.commit()
    assert _hidden(conn, "2025/roundup.md") is None
    assert _hidden(conn, "2025/long.md") == SOURCE_PAUSED
