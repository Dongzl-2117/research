"""Sanitized time evidence through the real converter and generic importer."""

import json

import pytest

from researchlib.backfill import backfill_published_at
from researchlib.ingest.parse import parse_archive
from researchlib.ingest.xueqiu import convert_directory
from researchlib.textutil import SHANGHAI, parse_datetime, resolve_published_at, split_frontmatter

EDITED = "2026-04-25 14:21"
CREATED = "2026-04-20 08:09"


@pytest.mark.parametrize("basis", ["修改于", "edited", "未知依据"])
def test_edit_or_uncertain_basis_never_falls_back_to_exported_date(basis):
    when, source, known = resolve_published_at(
        frontmatter={"published": EDITED},
        hints={"published_at": EDITED, "published_at_basis": basis},
        filename="2026-04-25_note.md",
        extra_filenames=("2026-04-25/export.md",),
        text=f"- 时间：{EDITED}\n正文",
    )
    assert (when, source, known) == (None, "", False)


@pytest.mark.parametrize("key", ["created_at", "created_time", "create_time"])
def test_reliable_creation_survives_coexisting_edit_time(key):
    when, source, known = resolve_published_at(
        frontmatter={"date": EDITED},
        hints={"published_at": EDITED, "published_at_basis": "修改于", key: CREATED},
        filename="2026-04-25_note.md",
        text=f"- 时间：{EDITED}",
    )
    assert when == parse_datetime(CREATED)
    assert source == "meta" and known is True


def test_edit_field_alone_does_not_authenticate_filename():
    assert resolve_published_at(
        frontmatter={"updated_at": EDITED}, filename="2026-04-25_note.md"
    ) == (None, "", False)


def test_separate_creation_and_edit_fields_without_basis_survive_exported_edit_date():
    when, source, known = resolve_published_at(
        frontmatter={"date": EDITED}, hints={"created_at": CREATED, "updated_at": EDITED},
        filename="2026-04-25_note.md",
    )
    assert when == parse_datetime(CREATED)
    assert source == "meta" and known is True


def test_explicit_published_basis_and_ordinary_documents_keep_dates():
    for front in ({"published": CREATED}, {"published": CREATED, "published_at_basis": "发布于"}):
        when, source, known = resolve_published_at(frontmatter=front)
        assert when == parse_datetime(CREATED)
        assert source == "frontmatter" and known is True
    when, source, known = resolve_published_at(filename="2026-04-20_note.md")
    assert when.date().isoformat() == "2026-04-20"
    assert source == "filename" and known is False


@pytest.mark.parametrize("created", [None, CREATED])
def test_converter_keeps_edit_evidence_through_generic_import(tmp_path, created):
    raw = tmp_path / "input"
    raw.mkdir()
    record = {
        "id": "500000001", "title": "时间示例", "body_text": "独立正文。",
        "published_at": EDITED, "published_at_basis": "修改于",
        "private_unused": "must not enter metadata",
    }
    if created:
        record["created_at"] = created
    (raw / "500000001.json").write_text(json.dumps(record), encoding="utf-8")
    output = tmp_path / "md"
    assert not convert_directory(raw, output).errors
    rendered = (output / "500000001.md").read_text()
    front, _ = split_frontmatter(rendered)
    assert "date" not in front and "published" not in front
    assert front["published_at_basis"] == "修改于"
    assert front["source_updated_at"] == EDITED
    docs, errors, _, _ = parse_archive(output, tmp_path / "work", id_mode="path")
    assert not errors and len(docs) == 1
    doc = docs[0]
    assert doc.published_at == parse_datetime(created)
    assert doc.raw_meta["published_at_basis"] == "修改于"
    assert doc.raw_meta["source_updated_at"] == EDITED
    assert doc.raw_meta["publication_time_status"] == ("known" if created else "unknown")
    assert "updated_at" not in doc.raw_meta
    assert "private_unused" not in doc.raw_meta
    assert "hidden" not in doc.raw_meta and "off_timeline" not in doc.raw_meta


def _archive(tmp_path, *, record=None, href="../../raw/500000002.json", front_extra=""):
    archive = tmp_path / "archive"
    md = archive / "md" / "2026"
    md.mkdir(parents=True)
    raw = archive / "raw"
    raw.mkdir()
    if record is not None:
        (raw / "500000002.json").write_text(json.dumps(record), encoding="utf-8")
    path = md / "2026-04-25_500000002_sample.md"
    path.write_text(
        f'---\nid: "500000002"\ntitle: 示例\npublished: "{EDITED}"\n{front_extra}---\n'
        f'\n- 时间：{EDITED}\n- 原始数据：[raw]({href})\n\n独立正文。\n',
        encoding="utf-8",
    )
    return archive, raw, path


def _parse(archive, tmp_path):
    docs, errors, _, _ = parse_archive(archive, tmp_path / "work", id_mode="path")
    assert not errors and len(docs) == 1
    return docs[0]


@pytest.mark.parametrize("created", [None, CREATED])
def test_existing_markdown_uses_only_matched_linked_raw_time_evidence(tmp_path, created):
    record = {
        "id": "500000002", "published_at": EDITED, "published_at_basis": "修改于",
        "url": "https://example.invalid/?signed=DO_NOT_COPY", "body_text": "DO_NOT_COPY",
    }
    if created:
        record["create_time"] = created
    archive, _, path = _archive(tmp_path, record=record)
    original = path.read_bytes()
    doc = _parse(archive, tmp_path)
    assert doc.published_at == parse_datetime(created)
    assert doc.raw_meta["published_at_basis"] == "修改于"
    assert doc.raw_meta["source_updated_at"] == EDITED
    assert "DO_NOT_COPY" not in json.dumps(doc.raw_meta)
    assert path.read_bytes() == original


@pytest.mark.parametrize("record", [None, {"id": "999999999", "published_at": CREATED}, {"id": True}])
def test_missing_or_wrong_identity_raw_does_not_override_independent_frontmatter(tmp_path, record):
    archive, _, _ = _archive(tmp_path, record=record)
    doc = _parse(archive, tmp_path)
    assert doc.published_at == parse_datetime(EDITED)
    assert "published_at_basis" not in doc.raw_meta
    assert "source_updated_at" not in doc.raw_meta


def test_matching_id_but_wrong_sn_is_rejected(tmp_path):
    archive, _, _ = _archive(
        tmp_path, record={"id": "500000002", "sn": "wrong", "create_time": CREATED},
        front_extra='sn: "expected"\n',
    )
    doc = _parse(archive, tmp_path)
    assert doc.published_at == parse_datetime(EDITED)
    assert "create_time" not in doc.raw_meta


@pytest.mark.parametrize("symlink_parent", [False, True])
def test_raw_symlink_file_or_parent_is_never_time_evidence(tmp_path, symlink_parent):
    archive, raw, _ = _archive(tmp_path)
    actual = archive / "actual"
    actual.mkdir()
    (actual / "500000002.json").write_text(
        json.dumps({"id": "500000002", "create_time": CREATED})
    )
    if symlink_parent:
        raw.rmdir()
        raw.symlink_to(actual, target_is_directory=True)
    else:
        (raw / "500000002.json").symlink_to(actual / "500000002.json")
    doc = _parse(archive, tmp_path)
    assert doc.published_at == parse_datetime(EDITED)
    assert "create_time" not in doc.raw_meta


def test_raw_escape_does_not_read_outside_archive_or_use_same_basename(tmp_path):
    outside = tmp_path / "raw"
    outside.mkdir()
    record = {"id": "500000002", "create_time": CREATED}
    (outside / "500000002.json").write_text(json.dumps(record))
    archive, _, _ = _archive(tmp_path, record=record, href="../../../raw/500000002.json")
    doc = _parse(archive, tmp_path)
    assert doc.published_at == parse_datetime(EDITED)
    assert "create_time" not in doc.raw_meta


@pytest.mark.parametrize("wrapper", ["`{}`", "```md\n{}\n```", "~~~md\n{}\n~~~", "<!-- {} -->", r"\{}"])
def test_raw_link_in_code_comment_or_escaped_example_supplies_no_evidence(tmp_path, wrapper):
    archive, _, path = _archive(tmp_path, record={
        "id": "500000002", "published_at": EDITED, "published_at_basis": "修改于",
    })
    text = path.read_text()
    path.write_text(text.replace(
        '- 原始数据：[raw](../../raw/500000002.json)',
        wrapper.format('[raw](../../raw/500000002.json)'),
    ))
    doc = _parse(archive, tmp_path)
    assert doc.published_at == parse_datetime(EDITED)
    assert "published_at_basis" not in doc.raw_meta


def test_verified_publication_raw_keeps_publication_time(tmp_path):
    archive, _, _ = _archive(tmp_path, record={
        "id": "500000002", "published_at": EDITED, "published_at_basis": "发布于",
    })
    doc = _parse(archive, tmp_path)
    assert doc.published_at == parse_datetime(EDITED)
    assert doc.published_at.tzinfo == SHANGHAI
    assert doc.raw_meta["published_at_basis"] == "发布于"
    assert "source_updated_at" not in doc.raw_meta


def test_frontmatter_raw_link_string_is_not_a_body_reference(tmp_path):
    archive, _, path = _archive(tmp_path, record={
        "id": "500000002", "published_at": EDITED, "published_at_basis": "修改于",
    })
    path.write_text(
        f'---\nid: "500000002"\ntitle: "[raw](../../raw/500000002.json)"\npublished: "{EDITED}"\n---\n'
        '\n独立正文。\n'
    )
    doc = _parse(archive, tmp_path)
    assert doc.published_at == parse_datetime(EDITED)
    assert "published_at_basis" not in doc.raw_meta


def test_conflicting_matched_raw_records_do_not_restore_edit_date(tmp_path):
    archive, raw, path = _archive(tmp_path, record={
        "id": "500000002", "published_at": EDITED, "published_at_basis": "修改于",
    })
    (raw / "second.json").write_text(json.dumps({
        "id": "500000002", "published_at": "2026-04-26 15:22", "published_at_basis": "修改于",
    }))
    path.write_text(path.read_text() + '\n[第二份](../../raw/second.json)\n')
    doc = _parse(archive, tmp_path)
    assert doc.published_at is None
    assert doc.raw_meta["published_at_basis"] == "unknown"


def test_backfill_does_not_relabel_edit_time_as_publication():
    class Cursor:
        def fetchall(self):
            return [{"id": 1, "title": "示例", "body_md": "- 时间：" + EDITED,
                     "raw_meta": {"published_at_basis": "修改于", "source_updated_at": EDITED},
                     "external_id": "2026-04-25_note.md"}]

        def fetchone(self):
            return {"n": 1}

    class Connection:
        def execute(self, query, params=None):
            assert query.lstrip().startswith("SELECT"), "Unknown publication must not be written"
            return Cursor()

    assert backfill_published_at(Connection()) == {"scanned": 1, "updated": 0, "remaining": 1}
