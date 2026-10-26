"""雪球「黑黑无常」已抓取 JSON 转成 Markdown 后，能被通用导入器按雪球 id 解析。"""

import json
import shutil
from pathlib import Path

from researchlib.ingest.parse import parse_archive
from researchlib.ingest.xueqiu import convert_directory, main
from researchlib.textutil import SHANGHAI, split_frontmatter

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "xueqiu"
SAMPLES = ("355280496", "382428373", "412690019")
EXPECTED = {
    "355280496": {
        "title": "拼多多为什么是一个超越Costco的零售业创新？",
        "when": (2025, 10, 2, 0, 53),
        "url": "https://xueqiu.com/7342779920/355280496",
        "chars": 8938,
    },
    "382428373": {
        "title": "Temu自营业务的潜在路径",
        "when": (2026, 4, 2, 19, 15),
        "url": "https://xueqiu.com/7342779920/382428373",
        "chars": 1125,
    },
    "412690019": {
        "title": "增长是无关紧要的特质",
        "when": (2026, 10, 17, 14, 26),
        "url": "https://xueqiu.com/7342779920/412690019",
        "chars": 584,
    },
}


def _stage_samples(directory: Path) -> None:
    directory.mkdir()
    for post_id in SAMPLES:
        shutil.copy(FIXTURE / f"{post_id}.json", directory / f"{post_id}.json")
    (directory / "_summary.json").write_text('{"kept_count": 78}\n', encoding="utf-8")
    (directory / "_STOP.json").write_text("{}\n", encoding="utf-8")
    (directory / "_STOP.png").write_bytes(b"\x89PNG\r\n")
    (directory / "_run_meta.json").write_text("{}\n", encoding="utf-8")
    (directory / "notes.json").write_text('{"kept_count": 1}\n', encoding="utf-8")
    # 同名 md 比 body_text 短。转换必须用 JSON 全文。
    (directory / "382428373.md").write_text("# Temu自营业务的潜在路径\n\n只有一句。\n", encoding="utf-8")


def test_sample_posts_parse_with_stable_ids(tmp_path: Path):
    raw = tmp_path / "raw"
    out = tmp_path / "md"
    _stage_samples(raw)
    out.mkdir()
    (out / "999.md").write_text("上一次留下的，不该再导入\n", encoding="utf-8")

    report = convert_directory(raw, out)
    assert report.errors == []
    assert report.ids == list(SAMPLES)
    assert "_summary.json" in report.skipped
    assert "_STOP.json" in report.skipped
    assert "_STOP.png" in report.skipped
    assert "_run_meta.json" in report.skipped
    assert "notes.json" in report.skipped
    assert not (out / "999.md").exists()
    assert not (out / "_STOP.png").exists()
    assert sorted(path.name for path in out.glob("*.md")) == [f"{post_id}.md" for post_id in SAMPLES]

    first = (out / "355280496.md").read_bytes()
    again = convert_directory(raw, out)
    assert again.errors == []
    assert (out / "355280496.md").read_bytes() == first

    docs, errors, files, images = parse_archive(out, tmp_path / "work", id_mode="path")
    assert errors == []
    assert files == 3
    assert images == 0
    by_id = {doc.external_id: doc for doc in docs}
    assert set(by_id) == set(SAMPLES)

    for post_id, expect in EXPECTED.items():
        doc = by_id[post_id]
        raw_post = json.loads((FIXTURE / f"{post_id}.json").read_text(encoding="utf-8"))
        front, body = split_frontmatter(doc.body_md)
        assert doc.title == expect["title"]
        assert str(front["id"]) == post_id
        assert front["author"] == "黑黑无常"
        assert front["url"] == expect["url"]
        assert front["date"] == raw_post["published_at"]
        assert front["published"] == raw_post["published_at"]
        assert body.rstrip("\n") == raw_post["body_text"]
        assert len(raw_post["body_text"]) == expect["chars"]
        assert "只有一句" not in doc.body_md
        assert "h-char" not in doc.body_md
        assert "来源：雪球App" not in body
        when = doc.published_at.astimezone(SHANGHAI)
        assert (when.year, when.month, when.day, when.hour, when.minute) == expect["when"]
        assert doc.raw_meta["time_known"] is True
        assert doc.raw_meta["date_source"] == "frontmatter"

    (out / "382428373.md").rename(out / "renamed-temu.md")
    renamed, rename_errors, rename_files, _images = parse_archive(out, tmp_path / "work-renamed", id_mode="path")
    assert rename_errors == []
    assert rename_files == 3
    assert {doc.external_id for doc in renamed} == set(SAMPLES)


def test_markdown_fallback_and_colon_title(tmp_path: Path):
    raw = tmp_path / "raw"
    out = tmp_path / "md"
    raw.mkdir()
    (raw / "400000001.json").write_text(
        json.dumps(
            {
                "id": "400000001",
                "title": "只有 markdown",
                "url": "https://xueqiu.com/7342779920/400000001",
                "published_at": "2026-04-01 00:53",
                "body_text": "   ",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (raw / "400000001.md").write_text("# 只有 markdown\n\n第一段。\n\n第二段。\n", encoding="utf-8")
    (raw / "400000002.json").write_text(
        json.dumps(
            {
                "id": 400000002,
                "title": "问：增长重要吗？",
                "url": "https://xueqiu.com/1/400000002",
                "published_at": "2026-10-17 14:26",
                "body_text": "正文不截断：" + "甲" * 80,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    report = convert_directory(raw, out)
    assert report.errors == []
    assert report.ids == ["400000001", "400000002"]
    _front, fallback = split_frontmatter((out / "400000001.md").read_text(encoding="utf-8"))
    assert fallback.rstrip("\n") == "第一段。\n\n第二段。"

    docs, errors, files, _images = parse_archive(out, tmp_path / "work", id_mode="path")
    assert errors == []
    assert files == 2
    by_id = {doc.external_id: doc for doc in docs}
    assert by_id["400000001"].title == "只有 markdown"
    fallback_when = by_id["400000001"].published_at.astimezone(SHANGHAI)
    assert (fallback_when.year, fallback_when.month, fallback_when.day, fallback_when.hour, fallback_when.minute) == (
        2026,
        4,
        1,
        0,
        53,
    )
    assert by_id["400000002"].title == "问：增长重要吗？"
    assert by_id["400000002"].external_id == "400000002"
    assert "甲" * 80 in by_id["400000002"].body_md


def test_failed_convert_does_not_wipe_previous_archive(tmp_path: Path):
    raw = tmp_path / "raw"
    out = tmp_path / "md"
    raw.mkdir()
    out.mkdir()
    (raw / "_summary.json").write_text("{}\n", encoding="utf-8")
    (raw / "400000003.json").write_text("{", encoding="utf-8")
    (out / "400000003.md").write_text("上一份\n", encoding="utf-8")
    report = convert_directory(raw, out)
    assert report.written == []
    assert report.errors
    assert (out / "400000003.md").read_text(encoding="utf-8") == "上一份\n"


def test_cli_and_same_directory_refusal(tmp_path: Path):
    raw = tmp_path / "raw"
    out = tmp_path / "md"
    _stage_samples(raw)
    assert main(["--input", str(raw), "--output", str(out)]) == 0
    assert {path.stem for path in out.glob("*.md")} == set(SAMPLES)
    refused = convert_directory(raw, raw)
    assert refused.written == []
    assert refused.errors
    assert (raw / "355280496.json").is_file()
    assert not (raw / "355280496.md").exists()
    assert (raw / "382428373.md").read_text(encoding="utf-8").startswith("# ")
