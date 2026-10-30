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


def test_qa_headings_drop_the_asker_name():
    """星球问答的「提问（网友 → 方伟Rey）」「回答（方伟Rey）」当章节名时不带网友的昵称。没有这对标题的不动。"""
    body = "# 问：怎么看茅台\n\n## 提问（王小明 → 方伟Rey）\n\n怎么看茅台？\n\n## 回答（方伟Rey）\n\n先看批价。\n"
    assert make_chunks("问：怎么看茅台", body) == [("提问", "怎么看茅台？"), ("回答", "先看批价。")]
    notes = "## 回答（续）\n\n上回没说完的。\n"
    assert make_chunks("笔记", notes) == [("回答（续）", "上回没说完的。")]


def test_digest_prompt_is_a_guide_not_advice():
    from researchlib.digest import PROMPT

    assert "不要给出投资建议" in PROMPT
    assert "该不该买" in PROMPT
    assert "目标价" in PROMPT
    assert "AI 整合" not in PROMPT


def test_schedule_does_not_queue_during_pytest(monkeypatch, tmp_path):
    from researchlib import digest as digest_mod
    from researchlib.config import Settings

    settings = Settings(
        database_url="postgresql://research:research@127.0.0.1:5432/research_test",
        data_dir=tmp_path,
        public_base_url="https://investment.zldong.com",
        bind_host="127.0.0.1",
        bind_port=8088,
        allow_nonlocal_bind=False,
        sources_path=tmp_path / "sources.yaml",
        display_timezone="Asia/Shanghai",
    )
    monkeypatch.setattr(digest_mod, "llm_configured", lambda _settings: True)
    before = list(digest_mod._order)
    assert digest_mod.schedule_digests(settings, [(987654, "方伟")]) == 0
    assert digest_mod._order == before


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
