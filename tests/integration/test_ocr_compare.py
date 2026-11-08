"""新旧识图对比包（research-ocr --compare）：挑样本、用现在的说明重认、不写库，原图和新旧结果打成一个包。"""

import json
import tarfile
from pathlib import Path

import pytest
from psycopg.types.json import Jsonb

from researchlib.ocr import VISION_PROMPT_VERSION, Recognition, pending_images, refresh_article_ocr
from researchlib.ocr_compare import pick_sample, run_compare
from researchlib.ocr_units import format_units, normalize_units

BODY = "截图。"
CHAT_OLD = "王小明：毛利率：35%，比去年高\n方伟：先看现金流"
CHAT_NEW = normalize_units(
    {
        "kind": "chat",
        "blocks": [
            {"type": "message", "speaker": "王小明", "self": False, "text": "毛利率：35%，比去年高"},
            {"type": "message", "speaker": "", "self": True, "text": "先看现金流"},
        ],
    }
)


def _setup(conn, data_dir: Path) -> dict[str, int]:
    conn.execute(
        """
        INSERT INTO articles (source_id, external_id, content_hash, title, published_at, body_md, body_text)
        VALUES ('fangwei-zsxq', 'shots', 's', '连着截的群聊', '2026-11-01 12:00+08', %(b)s, %(b)s),
               ('fangwei-zsxq', 'single', 'g', '一张截图', '2026-10-20 12:00+08', %(b)s, %(b)s),
               ('fangwei-mp', 'chart', 'c', '一张图表', '2026-10-10 12:00+08', %(b)s, %(b)s)
        """,
        {"b": BODY},
    )
    ids = {row["external_id"]: row["id"] for row in conn.execute("SELECT id, external_id FROM articles").fetchall()}
    images = [("shots", "a", CHAT_OLD), ("shots", "b", CHAT_OLD + "\n王小明：好的"), ("single", "c", CHAT_OLD),
              ("chart", "d", "营收 2024 120 亿\n营收 2025 150 亿")]
    for external_id, digit, text in images:
        content_hash = digit * 64
        conn.execute(
            """
            INSERT INTO article_images (article_id, source_id, original_ref, storage_path, content_hash)
            SELECT id, source_id, %s, %s, %s FROM articles WHERE external_id = %s
            """,
            (f"{digit}.png", f"x/{digit}.png", content_hash, external_id),
        )
        conn.execute(
            "INSERT INTO image_texts (content_hash, engine, text) VALUES (%s, 'deepseek-flash-vision', %s)",
            (content_hash, text),
        )
        path = data_dir / "images" / "x" / f"{digit}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + digit.encode() * 32)
    for article_id in ids.values():
        refresh_article_ocr(conn, article_id)
    conn.commit()
    return ids


def test_sample_takes_whole_chat_sequences_then_single_dialogue_then_random(conn, settings):
    ids = _setup(conn, Path(settings.data_dir))
    sample = pick_sample(conn, 6)
    strata = {row["content_hash"][0]: (row["stratum"], row["article_id"]) for row in sample}
    assert strata["a"] == ("shots", ids["shots"]) and strata["b"] == ("shots", ids["shots"])
    assert strata["c"] == ("dialogue", ids["single"])
    assert strata["d"] == ("random", ids["chart"])
    # 同一个库每次抽到同一批。
    assert [row["content_hash"] for row in pick_sample(conn, 6)] == [row["content_hash"] for row in sample]


def test_sample_stays_the_same_batch_after_the_prompt_changes(conn, settings):
    """说明改了版本以后，上一版结构认的图全库重认也要认，但不进对比样本：改说明前后两个包是同一批旧图。"""
    _setup(conn, Path(settings.data_dir))
    before = [row["content_hash"] for row in pick_sample(conn, 6)]
    structured = "e" * 64
    conn.execute(
        """
        INSERT INTO article_images (article_id, source_id, original_ref, storage_path, content_hash)
        SELECT id, source_id, 'e.png', 'x/e.png', %s FROM articles WHERE external_id = 'single'
        """,
        (structured,),
    )
    conn.execute(
        "INSERT INTO image_texts (content_hash, engine, text, units, prompt_version)"
        " VALUES (%s, 'deepseek-flash-vision', %s, %s, %s)",
        (structured, format_units(CHAT_NEW), Jsonb(CHAT_NEW), VISION_PROMPT_VERSION - 1),
    )
    conn.commit()
    assert structured in {row["content_hash"] for row in pending_images(conn, 10, redo=True)}
    assert [row["content_hash"] for row in pick_sample(conn, 6)] == before


def test_compare_writes_a_bundle_and_leaves_the_library_alone(conn, settings):
    ids = _setup(conn, Path(settings.data_dir))
    before = conn.execute("SELECT content_hash, engine, text, units FROM image_texts ORDER BY 1").fetchall()
    ocr_before = conn.execute("SELECT id, ocr_text, ocr_hash FROM articles ORDER BY id").fetchall()

    def recognize(_path: Path) -> Recognition:
        return Recognition(format_units(CHAT_NEW), CHAT_NEW, {"prompt": 10, "completion": 100, "reasoning": 90})

    report = run_compare(conn, Path(settings.data_dir), recognize, 6, workers=2)
    assert conn.execute("SELECT content_hash, engine, text, units FROM image_texts ORDER BY 1").fetchall() == before
    assert conn.execute("SELECT id, ocr_text, ocr_hash FROM articles ORDER BY id").fetchall() == ocr_before
    assert (report["images"], report["structured"], report["failed"]) == (4, 4, 0)
    assert report["tokens"] == {"prompt": 40, "completion": 400, "reasoning": 360}

    entry = next(e for e in report["entries"] if e["content_hash"].startswith("c"))
    assert entry["old"]["raw"] == CHAT_OLD
    assert entry["new"]["presented"] == "王小明：毛利率：35%，比去年高\n\n方伟：先看现金流"
    whole = report["whole_articles"]
    assert [item["article_id"] for item in whole] == [ids["shots"]]
    assert whole[0]["old"] == conn.execute("SELECT ocr_text FROM articles WHERE id = %s", (ids["shots"],)).fetchone()[
        "ocr_text"
    ]
    # 两张一样的结构化截图首尾重合，拼成一份。
    assert whole[0]["new"] == "王小明：毛利率：35%，比去年高\n\n方伟：先看现金流"

    bundle = Path(report["bundle"])
    with tarfile.open(bundle) as tar:
        names = tar.getnames()
        root = names[0].split("/")[0]
        assert f"{root}/report.md" in names and f"{root}/report.json" in names
        assert sum(1 for name in names if name.startswith(f"{root}/images/")) == 4
        saved = json.loads(tar.extractfile(f"{root}/report.json").read())
    assert saved["images"] == 4 and saved["entries"][0]["image"].startswith("images/")


def test_compare_stops_when_the_api_refuses_the_key(conn, settings):
    _setup(conn, Path(settings.data_dir))

    def refused(_path: Path) -> Recognition:
        raise RuntimeError("DeepSeek 接口返回 401：invalid key")

    with pytest.raises(RuntimeError, match="401"):
        run_compare(conn, Path(settings.data_dir), refused, 4, workers=1)
