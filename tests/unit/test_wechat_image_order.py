"""Bounded archive repair preserves prose and fails closed on conflicting evidence."""

import json

import pytest

from researchlib.ingest.parse import parse_archive
from researchlib.ingest.wechat_image_order import restore_image_order
from researchlib.textutil import normalize_newlines

SN = "a" * 32
OTHER = "b" * 32
URL = f"https://mp.weixin.qq.com/s?__biz=test-archive&mid=1&idx=1&sn={SN}"
FIRST = "“价值投资首先要识别好生意与坏生意。”"
SECOND = "作者限定：**不熟悉**这个行业，且[条件](https://example.invalid/condition)仍需核对。"
LAST = "原文保留：100 * 2 = 200；66元是立减金，不是售价。"
SECOND_HTML = (
    '作者限定：<strong>不熟悉</strong>这个行业，且<a href="https://example.invalid/condition">条件</a>仍需核对。'
)
IMAGE_ONE = '<img src="https://example.invalid/one">'
IMAGE_TWO = '<img src="https://example.invalid/two">'


def _record():
    return {
        "url": URL,
        "user_name": "gh_test_archive",
        "biz": "test-archive",
        "author": "测试作者",
        "title": "测试文章",
        "content": f"<p>{FIRST}</p>{IMAGE_ONE}<p>{SECOND_HTML}</p>{IMAGE_TWO}<p>{LAST}</p>",
        "content_multi_text": (
            f"<section><p>{FIRST}</p><p>{SECOND_HTML}</p>{IMAGE_ONE}<p>{LAST}</p>{IMAGE_TWO}</section>"
        ),
        "unused_private": "MUST_NOT_COPY",
    }


def _archive(tmp_path, *, record=None, body=None):
    root = tmp_path / "archive"
    md = root / "md"
    raw = root / "raw" / "articles"
    images = root / "images"
    for directory in (md, raw, images):
        directory.mkdir(parents=True)
    for i in (1, 2):
        (images / f"{SN}_{i:02}.png").write_bytes(b"local-archive-image")
    payload = _record() if record is None else record
    rawpath = raw / f"{SN}.json"
    rawpath.write_text(json.dumps(payload), encoding="utf-8")
    path = md / "article.md"
    prefix = f"""---
title: 测试文章
author: 测试作者
sn: {SN}
ghid: gh_test_archive
url: {URL}
published: 2025-01-01
---

# 测试文章

- 作者：测试作者
- 时间：2025-01-01
- sn：`{SN}`
- 原链接：{URL}
- 原始数据：[raw](../raw/articles/{SN}.json)

"""
    body = body or f"{FIRST}\n\n![](../images/{SN}_01.png)\n\n{SECOND}\n\n![附图](../images/{SN}_02.png)\n\n{LAST}\n"
    path.write_text(prefix + body, encoding="utf-8")
    return root, path, rawpath


def _restore(root, path):
    return restore_image_order(path.read_text(), path, [root])


def test_real_shaped_quote_conditions_links_math_and_promotion_are_not_rewritten(tmp_path):
    root, path, raw = _archive(tmp_path)
    original, rawbytes = path.read_bytes(), raw.read_bytes()
    result, status = _restore(root, path)
    assert status["status"] == "restored"
    assert (
        result.index(FIRST)
        < result.index(SECOND)
        < result.index(f"{SN}_01.png")
        < result.index(LAST)
        < result.index(f"{SN}_02.png")
    )
    assert all(result.count(piece) == 1 for piece in [FIRST, SECOND, LAST])
    assert path.read_bytes() == original and raw.read_bytes() == rawbytes
    assert "https://example.invalid/one" not in result
    assert "MUST_NOT_COPY" not in result + json.dumps(status)
    assert "https://" not in json.dumps(status)


def test_only_explicit_wechat_import_enables_repair_and_preserves_image_bindings(tmp_path):
    root, path, _ = _archive(tmp_path)
    original = path.read_bytes()
    ordinary, errors, _, _ = parse_archive(root, tmp_path / "ordinary", id_mode="path", source_kind="markdown")
    assert not errors and ordinary[0].body_md == normalize_newlines(original.decode())
    assert "wechat_image_order" not in ordinary[0].raw_meta
    repaired, errors, _, _ = parse_archive(root, tmp_path / "wechat", id_mode="path", source_kind="wechat_mp")
    assert not errors and repaired[0].raw_meta["wechat_image_order"]["status"] == "restored"
    assert repaired[0].body_md.index(SECOND) < repaired[0].body_md.index(f"{SN}_01.png")
    assert repaired[0].image_refs == ordinary[0].image_refs
    assert path.read_bytes() == original


@pytest.mark.parametrize(
    "change",
    [
        lambda r: r.pop("content_multi_text"),
        lambda r: r.update(content_multi_text=""),
        lambda r: r.update(content=""),
    ],
)
def test_missing_optional_evidence_is_unchanged_without_claiming_verified(tmp_path, change):
    record = _record()
    change(record)
    root, path, _ = _archive(tmp_path, record=record)
    result, status = _restore(root, path)
    assert result == path.read_text() and status["status"] == "no_evidence"


@pytest.mark.parametrize(
    "change",
    [
        lambda r: r.update(url=URL.replace(SN, OTHER)),
        lambda r: r.update(url=URL + "&sn=" + SN),
        lambda r: r.update(url=URL + "&sn="),
        lambda r: r.update(url=URL + "&__biz="),
        lambda r: r.update(url=URL.replace("test-archive", "")),
        lambda r: r.update(url=URL.replace("mp.weixin.qq.com", "example.invalid")),
        lambda r: r.update(user_name="another-account"),
        lambda r: r.update(sn=OTHER),
        lambda r: r.update(ghid="another-account"),
        lambda r: r.update(biz="another-publisher"),
        lambda r: r.update(title="another article"),
        lambda r: r.update(author="another author"),
    ],
)
def test_article_account_or_title_conflict_never_supplies_positions(tmp_path, change):
    record = _record()
    change(record)
    root, path, _ = _archive(tmp_path, record=record)
    result, status = _restore(root, path)
    assert result == path.read_text() and status["status"] == "needs_review"


@pytest.mark.parametrize(
    "change",
    [
        lambda r: r.update(content_multi_text=r["content_multi_text"].replace(LAST, "截断")),
        lambda r: r.update(content_multi_text=r["content_multi_text"] + "额外文字"),
        lambda r: r.update(content_multi_text=r["content_multi_text"][:-10]),
        lambda r: r.update(content_multi_text="<html><body>" + r["content_multi_text"] + "</body></html>"),
        lambda r: r.update(content_multi_text="<script>ignored()</script>" + r["content_multi_text"]),
        lambda r: r.update(
            content_multi_text=r["content_multi_text"].replace(
                IMAGE_ONE, '<img src="https://example.invalid/one" data-src="https://example.invalid/other">'
            )
        ),
        lambda r: r.update(
            content_multi_text=r["content_multi_text"].replace(
                "https://example.invalid/one", "https://example.invalid/two"
            )
        ),
        lambda r: r.update(content_multi_text=r["content_multi_text"].replace(IMAGE_TWO, "")),
        lambda r: r.update(
            content_multi_text=r["content_multi_text"].replace(
                IMAGE_TWO, '<img src="https://example.invalid/two" srcset="https://example.invalid/other 2x">'
            )
        ),
        lambda r: r.update(
            content_multi_text=r["content_multi_text"].replace(
                IMAGE_TWO, '<picture><source srcset="https://example.invalid/other">' + IMAGE_TWO + "</picture>"
            )
        ),
    ],
)
def test_content_structure_or_media_disagreement_falls_back(tmp_path, change):
    record = _record()
    change(record)
    root, path, _ = _archive(tmp_path, record=record)
    result, status = _restore(root, path)
    assert result == path.read_text() and status["status"] == "needs_review"


@pytest.mark.parametrize("container", ["blockquote", "ul", "table", "pre", "code", "a", "figure", "h2", "dd"])
def test_equal_text_offsets_do_not_authorize_moving_images_out_of_semantic_containers(tmp_path, container):
    record = _record()
    image = '<img src="https://example.invalid/one">'
    record["content_multi_text"] = record["content_multi_text"].replace(image, f"<{container}>{image}</{container}>")
    root, path, _ = _archive(tmp_path, record=record)
    result, status = _restore(root, path)
    assert result == path.read_text() and status["status"] == "needs_review"


def test_same_boundary_consecutive_images_keep_repeated_occurrences(tmp_path):
    record = _record()
    record["content"] = record["content"].replace("https://example.invalid/two", "https://example.invalid/one")
    record["content_multi_text"] = f"<p>{FIRST}</p><p>{SECOND_HTML}</p><p>{LAST}</p>{IMAGE_ONE}{IMAGE_ONE}"
    root, path, _ = _archive(tmp_path, record=record)
    result, status = _restore(root, path)
    assert status["status"] == "restored" and status["image_occurrences"] == 2
    assert result.index(LAST) < result.index(f"{SN}_01.png") < result.index(f"{SN}_02.png")


def test_changed_old_image_offsets_break_the_binding_witness(tmp_path):
    body = f"{FIRST}\n\n{SECOND}\n\n![](../images/{SN}_01.png)\n\n![附图](../images/{SN}_02.png)\n\n{LAST}\n"
    root, path, _ = _archive(tmp_path, body=body)
    result, status = _restore(root, path)
    assert result == path.read_text() and status["reason"] == "old_image_position_disagreement"


@pytest.mark.parametrize("mode", ["missing", "symlink", "wrong_ordinal", "outside_raw", "duplicate_raw", "code_raw"])
def test_local_evidence_must_be_explicit_contained_complete_and_ordered(tmp_path, mode):
    root, path, raw = _archive(tmp_path)
    if mode == "missing":
        (root / "images" / f"{SN}_02.png").unlink()
    elif mode == "symlink":
        image = root / "images" / f"{SN}_02.png"
        image.unlink()
        image.symlink_to(root / "images" / f"{SN}_01.png")
    elif mode == "wrong_ordinal":
        path.write_text(path.read_text().replace(f"{SN}_01.png", f"{SN}_02.png"))
    elif mode == "outside_raw":
        outside = tmp_path / f"{SN}.json"
        outside.write_bytes(raw.read_bytes())
        path.write_text(path.read_text().replace(f"../raw/articles/{SN}.json", f"../../{SN}.json"))
    elif mode == "duplicate_raw":
        path.write_text(path.read_text() + f"\n- 原始数据：[other](../raw/articles/{SN}.json)\n\n")
    elif mode == "code_raw":
        line = f"- 原始数据：[raw](../raw/articles/{SN}.json)"
        path.write_text(path.read_text().replace(line, "```md\n" + line + "\n```"))
    result, status = _restore(root, path)
    assert result == path.read_text() and status["status"] == ("no_evidence" if mode == "code_raw" else "needs_review")


def test_proven_leading_image_positions_preserve_first_prose_and_repeated_occurrences(tmp_path):
    record = _record()
    text = f"<p>{FIRST}</p><p>{SECOND_HTML}</p><p>{LAST}</p>"
    record["content_multi_text"] = IMAGE_ONE + IMAGE_TWO + text
    root, path, _ = _archive(tmp_path, record=record)
    result, status = _restore(root, path)
    assert status["status"] == "restored"
    assert result.index(f"{SN}_01.png") < result.index(f"{SN}_02.png") < result.index(FIRST)
    assert FIRST + "\n\n" + SECOND + "\n\n" + LAST in result


def test_standard_markdown_literal_star_disagreement_is_not_relaxed_for_repair(tmp_path):
    record = _record()
    record["content"] = record["content"].replace(FIRST, FIRST + "*两个*字面星号")
    record["content_multi_text"] = record["content_multi_text"].replace(FIRST, FIRST + "*两个*字面星号")
    root, path, _ = _archive(tmp_path, record=record)
    path.write_text(path.read_text().replace(FIRST, FIRST + "*两个*字面星号"))
    result, status = _restore(root, path)
    assert result == path.read_text() and status["reason"] == "visible_text_disagreement"


def test_canonical_url_fragments_do_not_change_article_identity(tmp_path):
    record = _record()
    record["url"] += "#article-location"
    root, path, _ = _archive(tmp_path, record=record)
    result, status = _restore(root, path)
    assert status["status"] == "restored"
    assert "#article-location" not in result


@pytest.mark.parametrize(
    "url",
    [
        URL.replace("mp.weixin.qq.com", "mp.weixin.qq.com:444"),
        URL.replace("mp.weixin.qq.com", "mp.weixin.qq.com:invalid"),
    ],
)
def test_noncanonical_ports_do_not_supply_identity(tmp_path, url):
    record = _record()
    record["url"] = url
    root, path, _ = _archive(tmp_path, record=record)
    result, status = _restore(root, path)
    assert result == path.read_text() and status["status"] == "needs_review"
