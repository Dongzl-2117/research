from pathlib import Path

import pytest

from researchlib.attachments import (
    attachment_mapping,
    collect_attachment_refs,
    relative_attachment_path,
    resolve_article_attachment,
    rewrite_markdown_attachment_links,
    rewrite_relative_attachment_links,
    with_stored_attachments,
)
from researchlib.mdlinks import prepare_body_html


@pytest.mark.parametrize(
    "ref",
    ["https://example.com/a.pdf", "//example.com/a.pdf", "/etc/a.pdf", "C:\\a.pdf", "data:a.pdf",
     "javascript:a.pdf", "notes.html", "drawing.svg", "%2Fetc/a.pdf", "a%00.pdf", "#a.pdf"],
)
def test_only_relative_allowlisted_attachments(ref):
    assert relative_attachment_path(ref) is None


def test_collect_resolves_relative_refs_and_never_escapes_archive(tmp_path):
    archive = tmp_path / "archive"
    docs = archive / "docs"
    files = archive / "files"
    docs.mkdir(parents=True)
    files.mkdir()
    pdf = files / "研究 报告.pdf"
    pdf.write_bytes(b"%PDF-1.4\nfixture")
    secret = tmp_path / "outside.pdf"
    secret.write_bytes(b"%PDF-1.4\nprivate")
    md = docs / "topic.md"
    refs = collect_attachment_refs(
        "[PDF](../files/研究%20报告.pdf#page=2) [escape](../../outside.pdf)", md, [archive]
    )
    assert refs == [("../files/研究 报告.pdf", pdf.resolve()), ("../../outside.pdf", None)]


def test_same_basename_is_never_a_guess(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    for folder in ("a", "b"):
        (tmp_path / folder / "note.pdf").write_bytes(b"%PDF-1.4\n" + folder.encode())
    refs = collect_attachment_refs("[note](note.pdf)", tmp_path / "index.md", [tmp_path])
    assert refs == [("note.pdf", None)]


def test_symlink_attachment_is_not_read(tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    target = tmp_path / "outside.pdf"
    target.write_bytes(b"%PDF-1.4\nprivate")
    (archive / "note.pdf").symlink_to(target)
    refs = collect_attachment_refs("[note](note.pdf)", archive / "index.md", [archive])
    assert refs == [("note.pdf", None)]


def _stored(tmp_path):
    pdf = tmp_path / "report.pdf"
    pdf.write_bytes(b"%PDF-1.4\nfixture")
    data = tmp_path / "data"
    meta = with_stored_attachments({"keep": "evidence"}, [("files/report.pdf", pdf)], data, "feishu")
    entry = meta["local_attachments"]["files/report.pdf"]
    return data, meta, entry


def test_import_manifest_mapping_and_readonly_resolution(tmp_path):
    data, meta, entry = _stored(tmp_path)
    filename = Path(entry["storage_path"]).name
    assert meta["keep"] == "evidence"
    assert attachment_mapping(meta, 42, "feishu") == {
        "files/report.pdf": f"/articles/42/attachments/{filename}"
    }
    result = resolve_article_attachment(data, "feishu", meta, filename)
    assert result == ((data / "attachments" / entry["storage_path"]).resolve(), "application/pdf", "report.pdf")
    assert resolve_article_attachment(data, "other-source", meta, filename) is None
    before = result[0].stat().st_mtime_ns
    assert resolve_article_attachment(data, "feishu", meta, filename) == result
    assert result[0].stat().st_mtime_ns == before


@pytest.mark.parametrize("filename", ["../report.pdf", "%2e%2e/report.pdf", "/etc/passwd", "a" * 64 + ".html"])
def test_attachment_download_rejects_arbitrary_paths(tmp_path, filename):
    data, meta, _entry = _stored(tmp_path)
    assert resolve_article_attachment(data, "feishu", meta, filename) is None


def test_manifest_cannot_override_mime_or_escape_storage(tmp_path):
    data, meta, entry = _stored(tmp_path)
    filename = Path(entry["storage_path"]).name
    entry["media_type"] = "text/html"
    assert resolve_article_attachment(data, "feishu", meta, filename)[1] == "application/pdf"
    entry["storage_path"] = "../" + entry["storage_path"]
    assert resolve_article_attachment(data, "feishu", meta, filename) is None


def test_symlink_storage_is_not_served(tmp_path):
    data, meta, entry = _stored(tmp_path)
    target = data / "attachments" / entry["storage_path"]
    target.unlink()
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"%PDF-1.4\nprivate")
    target.symlink_to(outside)
    assert resolve_article_attachment(data, "feishu", meta, target.name) is None


def test_pdf_extension_without_pdf_content_is_not_imported(tmp_path):
    path = tmp_path / "fake.pdf"
    path.write_text("<script>alert(1)</script>")
    assert with_stored_attachments({}, [("fake.pdf", path)], tmp_path / "data", "feishu") == {}


def test_symlink_storage_directory_is_not_written(tmp_path):
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-1.4\nfixture")
    data = tmp_path / "data"
    data.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (data / "attachments").symlink_to(outside, target_is_directory=True)
    assert with_stored_attachments({}, [("source.pdf", source)], data, "feishu") == {}
    assert list(outside.iterdir()) == []


def test_rendered_pdf_maps_or_has_explicit_unavailable_status(tmp_path):
    _data, meta, _entry = _stored(tmp_path)
    mapping = attachment_mapping(meta, 42, "feishu")
    body = "[PDF](files/report.pdf) [absent](files/missing.pdf) [external](https://example.com/report.pdf)"
    html = prepare_body_html(body, "Title", {}, None, None, attachments=mapping)
    assert 'href="/articles/42/attachments/' in html
    assert " download>PDF</a>" in html
    assert "absent<span" in html and "附件未收录" in html
    assert 'href="files/' not in html
    assert 'href="https://example.com/report.pdf"' in html
    assert "附件未收录" in rewrite_relative_attachment_links('<a href="../missing.pdf">missing</a>')


def test_api_markdown_uses_same_mapping_and_leaves_code_images_and_external_urls(tmp_path):
    _data, meta, _entry = _stored(tmp_path)
    mapping = attachment_mapping(meta, 42, "feishu")
    body = ('[PDF](<files/report.pdf> "title") [missing](none.pdf) ![cover](cover.pdf) '
            '[external](https://example.com/report.pdf) `[code](none.pdf)`\n'
            '```md\n[codeblock](none.pdf)\n```\n')
    shown = rewrite_markdown_attachment_links(body, mapping)
    assert "[PDF](/articles/42/attachments/" in shown
    assert "missing（附件未收录）" in shown
    assert "![cover](cover.pdf)" in shown
    assert "[external](https://example.com/report.pdf)" in shown
    assert "`[code](none.pdf)`" in shown
    assert "[codeblock](none.pdf)" in shown


def test_api_markdown_reference_attachments_share_same_safe_mapping(tmp_path):
    _data, meta, _entry = _stored(tmp_path)
    mapping = attachment_mapping(meta, 42, "feishu")
    body = ('[报告][pdf] [缺失][unknown] [unknown][] [unknown]\n\n'
            '[pdf]: <files/report.pdf> "报告"\n[unknown]: missing.pdf\n'
            '[external]: https://example.com/note.pdf\n')
    shown = rewrite_markdown_attachment_links(body, mapping)
    assert "[pdf]: /articles/42/attachments/" in shown
    assert "缺失（附件未收录）" in shown
    assert shown.count("unknown（附件未收录）") == 2
    assert "[unknown]: missing.pdf" not in shown
    assert "[external]: https://example.com/note.pdf" in shown
