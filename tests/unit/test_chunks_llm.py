from pathlib import Path

import pytest

from researchlib.chunks import MAX_CHARS, make_chunks
from researchlib.digest import parse_digest
from researchlib.llm import LLMError
from researchlib.retrieval import cited_numbers
from researchlib.snippets import answer_html, mention_snippet

ROOT = Path(__file__).resolve().parents[2]


def test_chunks_follow_sections_and_stay_bounded():
    body = "# 标题\n\n开头一段。\n\n## 第一节\n\n" + "腾讯的游戏业务。" * 200 + "\n\n## 第二节\n\n短的一节。\n"
    chunks = make_chunks("标题", body)
    assert chunks[0] == ("", "开头一段。")
    assert {heading for heading, _ in chunks[1:-1]} == {"第一节"}
    assert chunks[-1] == ("第二节", "短的一节。")
    assert all(len(text) <= MAX_CHARS for _, text in chunks)


def test_chunks_strip_frontmatter_and_empty_body():
    assert make_chunks("t", "---\ndate: 2024-01-01\n---\n") == []
    assert make_chunks("t", "---\ndate: 2024-01-01\n---\n正文") == [("", "正文")]


def test_parse_digest_takes_json_inside_prose():
    data = parse_digest('好的：\n{"summary": "看好", "points": ["一", ""], "tags": ["白酒"]}')
    assert data == {"summary": "看好", "points": ["一"], "tags": ["白酒"]}
    with pytest.raises(LLMError):
        parse_digest("没有 JSON")


def test_cited_numbers_ignore_out_of_range():
    assert cited_numbers("结论 [2][1]，另见 [2] 和 [9]", 3) == [2, 1]


def test_answer_html_escapes_and_links_citations():
    html = str(answer_html("看好 <script>x</script> [1]"))
    assert "<script>" not in html
    assert '<a class="cite" href="#c-1">[1]</a>' in html


def test_mention_snippet_marks_first_hit():
    text = "前面很多字。" * 30 + "宁德时代的毛利率" + "后面。" * 30
    start = text.index("宁德时代")
    snippet = str(mention_snippet(text, [(start, start + 4, 1)], radius=10))
    assert "<mark>宁德时代</mark>" in snippet
    assert snippet.startswith("…") and snippet.endswith("…")


def test_startup_does_not_run_digest():
    app = (ROOT / "src/researchlib/web/app.py").read_text(encoding="utf-8")
    db = (ROOT / "src/researchlib/db.py").read_text(encoding="utf-8")
    assert "researchlib.digest" not in app
    assert "digest_article" not in app
    assert "digest_article" not in db
    assert "backfill_missing_chunks" in db
    assert "research-digest" not in (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "research-digest" not in (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
