"""译文对齐：条数必须一一对应；对不齐就拆批。空白段拒绝；中英长短不一收下。

不连数据库、不调真 LLM。模型用脚本替身，专门复现以前会 raise 的 count / short。
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

from researchlib.boilerplate import clean_article
from researchlib.render import render_body
from researchlib.textutil import split_frontmatter, strip_duplicate_title
from researchlib.translate.llm import ChatJsonResult
from researchlib.translate.segments import chunk_segments, leaf_count_problem, segments_of, splice_segments
from researchlib.translate.service import (
    TRANSLATE_BATCH_MAX_CHARS,
    TRANSLATE_BATCH_MAX_SEGMENTS,
    _batch_chunks,
    _render_original,
    _source_leaf_count,
    _translate_batch,
    translate_article,
    translation_problem,
)


class _FakeConn:
    """只回答译制和清洗用到的那几条查询，不连数据库。

    译制现在和文章页一样先过 ``clean_article``：它要知道来源 kind（决定要不要去引流
    尾巴），还会按来源统计模板句、查这份文章的图片哈希。这里都按「没有」回答。
    """

    def __init__(self, article: dict, *, kind: str = "rss") -> None:
        self.article = article
        self.kind = kind
        self.sql = ""
        self.committed = False

    def execute(self, sql, params=None):
        self.sql = sql
        return self

    def fetchone(self):
        if "FROM sources" in self.sql:
            return {"kind": self.kind}
        if "count(*)" in self.sql:
            return {"n": 0, "t": ""}
        return self.article

    def fetchall(self):
        return []

    def commit(self):
        self.committed = True


def _result(items: list[str]) -> ChatJsonResult:
    return ChatJsonResult(data={"t": items}, model="deepseek-flash", usage=None, latency_ms=1)


def _han(text: str) -> int:
    return len(re.findall(r"[\u4e00-\u9fff]", text))


def test_count_mismatch_is_rejected():
    sources = ["<p>One.</p>", "<p>Two.</p>"]
    assert translation_problem(sources, ["<p>一。</p>"]) == "count"
    assert translation_problem(sources, ["<p>一。</p>", "<p>二。</p>", "<p>三。</p>"]) == "count"


def test_empty_segment_is_rejected():
    src = "<p>" + ("Aggregation theory. " * 20) + "</p>"
    assert translation_problem([src], [""]) == "empty"
    assert translation_problem([src], ["   "]) == "empty"
    assert translation_problem([src, "<p>Next.</p>"], ["<p>聚合。</p>", ""]) == "empty"


def test_short_chinese_is_accepted():
    """以前 han < 英文长度的 15% 会判 short 并整批丢掉。长短不一现在收下。"""
    src = "<p>" + ("Aggregation theory explains why platforms win this market. " * 30) + "</p>"
    dst = "<p>平台会赢。</p>"
    assert len(src) >= 80
    assert _han(dst) < len(src) * 0.15
    assert translation_problem([src], [dst]) is None

    # 几乎没有汉字、但这一段不是空的：同样收下，不再拿比例做门槛。
    echo = "<p>" + ("platforms " * 40) + "</p>"
    assert _han(echo) == 0
    assert len(echo) >= 80
    assert translation_problem([src], [echo]) is None


def test_batch_chunks_cap_segment_count_and_keep_order():
    parts = [f"<p>{index}</p>" for index in range(TRANSLATE_BATCH_MAX_SEGMENTS + 5)]
    chunks = _batch_chunks(parts)
    assert [len(chunk) for chunk in chunks] == [TRANSLATE_BATCH_MAX_SEGMENTS, 5]
    assert [part for chunk in chunks for part in chunk] == parts


def test_batch_chunks_keep_one_oversized_segment_intact():
    huge = "a" * (TRANSLATE_BATCH_MAX_CHARS + 50)
    assert _batch_chunks([huge, "b"]) == [[huge], ["b"]]


def test_chunk_segments_honors_max_segments():
    parts = ["aa", "bb", "cc", "dd", "ee"]
    assert chunk_segments(parts, limit=10_000, max_segments=2) == [["aa", "bb"], ["cc", "dd"], ["ee"]]


def test_translate_article_uses_capped_batches():
    source = inspect.getsource(translate_article)
    assert "_batch_chunks(parts)" in source


def test_repair_fixes_count_without_splitting(monkeypatch, tmp_path: Path):
    calls: list[tuple[int, bool]] = []

    def fake(**kwargs):
        segments = kwargs["user"]["segments"]
        repair = "repair" in kwargs["user"]
        calls.append((len(segments), repair))
        if repair:
            return _result([f"<p>译{segment}</p>" for segment in segments])
        return _result(["<p>只有一段</p>"])

    monkeypatch.setattr("researchlib.translate.service.chat_json", fake)
    parts = ["<p>a</p>", "<p>b</p>", "<p>c</p>", "<p>d</p>"]
    got = _translate_batch(parts, {"articleTitle": "T"}, tmp_path)
    assert calls == [(4, False), (4, True)]
    assert got == [f"<p>译{part}</p>" for part in parts]


def test_count_after_repair_splits_and_keeps_one_to_one(monkeypatch, tmp_path: Path):
    """整批和重试都少一段；拆到两段及以下才按原文下标回译文。"""
    calls: list[int] = []

    def fake(**kwargs):
        segments = kwargs["user"]["segments"]
        calls.append(len(segments))
        if len(segments) > 2:
            return _result(["<p>合并掉了</p>"])
        return _result([f"<p>译:{segment}</p>" for segment in segments])

    monkeypatch.setattr("researchlib.translate.service.chat_json", fake)
    parts = [f"<p>block-{index}</p>" for index in range(4)]
    got = _translate_batch(parts, {"articleContext": "full article"}, tmp_path)
    assert calls == [4, 4, 2, 2]
    assert len(got) == len(parts)
    assert got == [f"<p>译:{part}</p>" for part in parts]


def test_count_mismatch_splits_until_single_segments(monkeypatch, tmp_path: Path):
    calls: list[int] = []

    def fake(**kwargs):
        segments = kwargs["user"]["segments"]
        calls.append(len(segments))
        if len(segments) != 1:
            # 多一段，条数永远对不上，直到拆成单段。
            return _result(["<p>x</p>"] * (len(segments) + 1))
        return _result([f"<p>译:{segments[0]}</p>"])

    monkeypatch.setattr("researchlib.translate.service.chat_json", fake)
    parts = [f"<p>p{index}</p>" for index in range(4)]
    got = _translate_batch(parts, {}, tmp_path)
    assert calls == [4, 4, 2, 2, 1, 1, 2, 2, 1, 1]
    assert got == [f"<p>译:{part}</p>" for part in parts]


def test_odd_split_does_not_drop_a_segment(monkeypatch, tmp_path: Path):
    calls: list[int] = []

    def fake(**kwargs):
        segments = kwargs["user"]["segments"]
        calls.append(len(segments))
        if len(segments) != 1:
            return _result(["<p>x</p>"] * (len(segments) - 1))
        return _result([f"<p>{segments[0]}</p>"])

    monkeypatch.setattr("researchlib.translate.service.chat_json", fake)
    parts = ["A", "B", "C"]
    got = _translate_batch(parts, {}, tmp_path)
    assert calls == [3, 3, 1, 2, 2, 1, 1]
    assert got == ["<p>A</p>", "<p>B</p>", "<p>C</p>"]


def test_single_segment_count_mismatch_still_raises(monkeypatch, tmp_path: Path):
    calls: list[bool] = []

    def fake(**kwargs):
        calls.append("repair" in kwargs["user"])
        return _result(["<p>一</p>", "<p>二</p>"])

    monkeypatch.setattr("researchlib.translate.service.chat_json", fake)
    with pytest.raises(RuntimeError, match="translation alignment: count"):
        _translate_batch(["<p>Only one block.</p>"], {}, tmp_path)
    # 原批一次，repair 一次，单段无法再拆。
    assert calls == [False, True]


def test_empty_segment_fails_without_split_or_repair(monkeypatch, tmp_path: Path):
    calls: list[int] = []

    def fake(**kwargs):
        segments = kwargs["user"]["segments"]
        calls.append(len(segments))
        out = [f"<p>译{index}</p>" for index in range(len(segments))]
        out[1] = "  "
        return _result(out)

    monkeypatch.setattr("researchlib.translate.service.chat_json", fake)
    with pytest.raises(RuntimeError, match="translation alignment: empty"):
        _translate_batch(["<p>a</p>", "<p>b</p>", "<p>c</p>"], {}, tmp_path)
    assert calls == [3]


def test_null_item_counts_as_empty(monkeypatch, tmp_path: Path):
    def fake(**kwargs):
        return ChatJsonResult(data={"t": [None]}, model="deepseek-flash", usage=None, latency_ms=1)

    monkeypatch.setattr("researchlib.translate.service.chat_json", fake)
    with pytest.raises(RuntimeError, match="translation alignment: empty"):
        _translate_batch(["<p>Hello.</p>"], {}, tmp_path)


def test_short_translation_is_stored_by_the_batch_path(monkeypatch, tmp_path: Path):
    src = "<p>" + ("The platform aggregates demand across the whole market. " * 25) + "</p>"
    dst = "<p>平台聚合需求。</p>"
    assert _han(dst) < len(src) * 0.15

    def fake(**kwargs):
        return _result([dst])

    monkeypatch.setattr("researchlib.translate.service.chat_json", fake)
    assert _translate_batch([src], {}, tmp_path) == [dst]


def test_split_passes_neighbor_context(monkeypatch, tmp_path: Path):
    seen: list[dict] = []

    def fake(**kwargs):
        user = kwargs["user"]
        seen.append(user)
        segments = user["segments"]
        if len(segments) > 1:
            return _result(["<p>短了</p>"])
        return _result([f"<p>译:{segments[0]}</p>"])

    monkeypatch.setattr("researchlib.translate.service.chat_json", fake)
    parts = ["<p>left</p>", "<p>right</p>"]
    got = _translate_batch(parts, {"articleContext": "WHOLE", "before": "Earlier."}, tmp_path)
    assert got == ["<p>译:<p>left</p></p>", "<p>译:<p>right</p></p>"]
    # 前两次是整批和 repair，之后左段、右段各一次。
    assert [len(user["segments"]) for user in seen] == [2, 2, 1, 1]
    left = seen[2]
    right = seen[3]
    assert left["articleContext"] == "WHOLE"
    assert "right" in left["after"]
    assert right["articleContext"] == "WHOLE"
    assert "译:" in right["before"]
    assert "Earlier." in right["before"]
    assert "repair" not in left
    assert "repair" not in right


def test_missing_t_array_still_raises(monkeypatch, tmp_path: Path):
    def fake(**kwargs):
        return ChatJsonResult(data={"note": "no t"}, model="deepseek-flash", usage=None, latency_ms=1)

    monkeypatch.setattr("researchlib.translate.service.chat_json", fake)
    with pytest.raises(RuntimeError, match="translation JSON missing t"):
        _translate_batch(["<p>Hi.</p>"], {}, tmp_path)


def test_translate_article_does_not_store_when_leaf_counts_diverge(monkeypatch, tmp_path: Path):
    """写回把叶子数改少时不入库。展示层不能靠位置硬配把这段找回来。"""

    conn = _FakeConn(
        {
            "id": 21118,
            "source_id": "stratechery",
            "title": "A daily",
            "published_at": None,
            "body_md": "<p>First paragraph is long enough.</p><p>Second paragraph is also here.</p>",
            "body_text": "First paragraph is long enough. Second paragraph is also here.",
            "raw_meta": {"format": "html"},
            "content_hash": "x",
        }
    )
    monkeypatch.setattr(
        "researchlib.translate.service._translate_batch",
        lambda parts, context, data_dir: ["<p>译</p>"] * len(parts),
    )
    monkeypatch.setattr(
        "researchlib.translate.service.splice_segments",
        lambda html, replacements: "<p>只有一段。</p>",
    )
    with pytest.raises(RuntimeError, match=r"translation alignment: leaves \d+ vs \d+"):
        translate_article(conn, 21118, data_dir=tmp_path)
    assert conn.committed is False


# 22092 的正文形状：标题下一行是归档头 `- source: member-rss`，跟一段三行 <br> 粘起来的正文。
_ARCHIVE_HEADER_BODY = """---
id: "2026/home-at-last"
title: "Home At Last"
---

# Home At Last
- source: member-rss

The best Stratechery content from the week of October 12, 2026.

Listen to this Update in your podcast player
X: @twostraws
Welcome back to This Week in Stratechery!

On that note, here were a few of our favorites.
"""


def _home_at_last() -> dict:
    return {
        "id": 22092,
        "source_id": "stratechery",
        "title": "Home At Last",
        "published_at": None,
        "body_md": _ARCHIVE_HEADER_BODY,
        "body_text": "The best Stratechery content from the week of October 12, 2026.",
        "raw_meta": {"format": "markdown"},
        "content_hash": "x",
    }


def test_render_original_drops_archive_header_leaf(tmp_path: Path):
    """归档头不进译制的叶子：不清洗时多一段 `<li>source: member-rss</li>`，和页面差一整段。"""
    article = _home_at_last()
    conn = _FakeConn(article)
    _frontmatter, raw_body = split_frontmatter(article["body_md"])
    raw_html = str(render_body(strip_duplicate_title(raw_body, article["title"]), {}, "markdown"))
    raw_leaves = segments_of(raw_html)
    assert any("member-rss" in leaf for leaf in raw_leaves)

    html, _digest = _render_original(conn, article, tmp_path)
    leaves = segments_of(html)
    assert not any("member-rss" in leaf for leaf in leaves)
    assert len(leaves) == len(raw_leaves) - 1


def test_render_original_uses_the_display_cleaning(tmp_path: Path):
    """译制读的正文和文章页显示的是同一份：叶子段逐段相同，#135 的 <br> 行拆叶也照样成立。"""
    article = _home_at_last()
    conn = _FakeConn(article)
    shown = clean_article(conn, article["id"], article["source_id"], article["body_md"])
    _frontmatter, shown_body = split_frontmatter(shown)
    display_html = str(render_body(strip_duplicate_title(shown_body, article["title"]), {}, "markdown"))

    html, _digest = _render_original(conn, article, tmp_path)
    assert segments_of(html) == segments_of(display_html)
    # 三行软换行没有句号，收成一句；前后两个完整段落仍各是一段。清洗没有把分段关掉。
    assert len(segments_of(html)) == 3


def test_translation_from_cleaned_body_keeps_leaf_counts(tmp_path: Path):
    """按清洗后的叶子译一遍、原位写回，叶子数不变；不会再有「来源：member-rss」这一对。"""
    article = _home_at_last()
    conn = _FakeConn(article)
    html, _digest = _render_original(conn, article, tmp_path)
    leaves = segments_of(html)
    zh = [f"<p>第 {index} 段中文</p>" for index, _leaf in enumerate(leaves, 1)]
    written = splice_segments(html, dict(enumerate(zh)))
    assert leaf_count_problem(html, written) is None
    assert "member-rss" not in written


def test_source_leaf_count_ignores_archive_header():
    """重检用的原文段数也跟着清洗，归档头不算叶子。"""
    article = _home_at_last()
    assert _source_leaf_count(_FakeConn(article), article) == 3


def test_clean_article_strips_rss_link():
    """`- rss_link:` 和 `- source:` 一样，clean_article 在送译前去掉，不进叶子。"""
    body = """# Home At Last

- source: member-rss
- rss_link: https://stratechery.com/?access_token=example

The best sentence of the week stays in the body.
"""
    article = _home_at_last()
    article["body_md"] = body
    cleaned = clean_article(_FakeConn(article), article["id"], article["source_id"], body)
    assert "rss_link" not in cleaned
    assert "access_token" not in cleaned
    assert "member-rss" not in cleaned
    assert "best sentence" in cleaned
