from pathlib import Path

import pytest

from researchlib.chunks import MAX_CHARS, make_chunks
from researchlib.digest import parse_digest
from researchlib.llm import LLMError
from researchlib.mentions import CompanyMatcher
from researchlib.retrieval import cited_numbers
from researchlib.snippets import answer_html, mention_sentence, mention_snippet, mention_spans

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


def test_mention_snippet_takes_the_sentence():
    # 提及原句：从命中所在那句的句首取到句末，不从半句、半个词开始。
    text = "前面很多字。" * 30 + "宁德时代的毛利率还在往上走，储能占比也在提高。" + "后面。" * 30
    start = text.index("宁德时代")
    snippet = str(mention_snippet(text, [(start, start + 4, 1)], radius=40))
    assert snippet == "<mark>宁德时代</mark>的毛利率还在往上走，储能占比也在提高。"
    # 句子比 radius 长：在 radius 处截，带省略号。
    text = "前面很多字" * 30 + "宁德时代的毛利率" + "后面" * 30
    start = text.index("宁德时代")
    snippet = str(mention_snippet(text, [(start, start + 4, 1)], radius=10))
    assert "<mark>宁德时代</mark>" in snippet
    assert snippet.startswith("…") and snippet.endswith("…")


def test_mention_sentence_for_the_api():
    english = "Apple grew services. Tencent keeps compounding in games while ads recover. Next one here."
    start = english.index("Tencent")
    assert mention_sentence(english, [(start, start + 7, 1)]) == (
        "Tencent keeps compounding in games while ads recover."
    )
    # 没有句末可停：前后在 radius 处截，英文不截在半个词上。
    words = "word " * 40 + "Tencent" + " more" * 40
    start = words.index("Tencent")
    sentence = mention_sentence(words, [(start, start + 7, 1)], radius=32)
    assert sentence.startswith("…word ") and sentence.endswith(" more…")
    # 提及在一行很短的话题行上（「-拼多多」）：接着取到下一句，不只给这一行。
    topic = "-拼多多\n提问了一个很糟糕的问题，对于渠道而言，拼单不是关键。后面还有很多话。"
    assert mention_sentence(topic, [(1, 4, 1)]) == "-拼多多 提问了一个很糟糕的问题，对于渠道而言，拼单不是关键。"
    assert mention_sentence("", [(0, 1, 1)]) == "" and mention_sentence("拼多多", []) == ""


def test_mention_spans_fall_back_to_image_text():
    matcher = CompanyMatcher([{"id": 7, "name_zh": "拼多多", "name_en": "PDD", "aliases": []}])
    text, spans = mention_spans(matcher, "美团", "「图片」", "")
    assert spans == []
    text, spans = mention_spans(matcher, "美团", "「图片」", "拼多多的百亿补贴还在，美团的外卖守住了")
    assert spans and text[spans[0][0] : spans[0][1]] == "拼多多"
    # 正文里有提及时只看正文。
    text, spans = mention_spans(matcher, "", "拼多多三季度的广告收入", "图里也写着拼多多")
    assert text == "拼多多三季度的广告收入" and spans[0][:2] == (0, 3)


def test_startup_does_not_run_digest():
    app = (ROOT / "src/researchlib/web/app.py").read_text(encoding="utf-8")
    db = (ROOT / "src/researchlib/db.py").read_text(encoding="utf-8")
    assert "researchlib.digest" not in app
    assert "digest_article" not in app
    assert "digest_article" not in db
    assert "backfill_missing_chunks" in db
    assert "research-digest" not in (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "research-digest" not in (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
