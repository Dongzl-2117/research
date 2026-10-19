from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ROOT / "src" / "researchlib" / "web" / "templates"


def _env():
    return Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["html"]))


def test_search_template_keeps_author_count_markup():
    html = _env().get_template("search.html").render(
        title="搜索 腾讯",
        canonical="https://investment.zldong.com/search?q=%E8%85%BE%E8%AE%AF",
        public_host="investment.zldong.com",
        query="腾讯",
        nav="search",
        sample_banner=False,
        sources=[],
        active_source="",
        author_counts=[{"name": "方伟", "count": 3}, {"name": "乘风破浪", "count": 0}],
        source_counts=[{"id": "fangwei-mp", "name": "方伟公众号", "count": 3}],
        results=[],
        total=0,
        total_all=3,
        filters={"author": "", "source": "", "company": None, "from": "", "to": ""},
        authors=["方伟", "乘风破浪"],
        has_filter=False,
        company=None,
        entity_companies=[],
        entity_authors=[],
        page=1,
        pages=1,
        base_query="q=%E8%85%BE%E8%AE%AF",
        page_query="q=%E8%85%BE%E8%AE%AF",
        ask_query="q=%E8%85%BE%E8%AE%AF",
    )
    assert "方伟</span><strong>3</strong>" in html
    assert "乘风破浪</span><strong>0</strong>" in html
    assert "investment.zldong.com" in html
    assert "research.zldong.com" not in html
    assert 'class="pills search-pills"' in html
    assert "没有找到" in html


class _Source:
    id = "fangwei-mp"
    name = "方伟公众号"
    author = "方伟"
    description = ""


def test_article_template_keeps_bilingual_layout_and_ask_tools():
    html = _env().get_template("article.html").render(
        title="Note",
        canonical="https://investment.zldong.com/articles/1",
        public_host="investment.zldong.com",
        query="",
        nav="archive",
        sample_banner=False,
        sources=[],
        active_source="",
        source=_Source(),
        article={"id": 1, "title": "Note"},
        iso="2026-01-01",
        date="2026-01-01",
        time="12:00",
        relative="昨天",
        title_zh="笔记",
        body_language="pairs",
        has_translation=True,
        translation_complete=True,
        pairs=[{"en": "Hello", "zh": "你好", "anchor": "p-1"}],
        body_html=None,
        original_url="",
        siblings=[],
        mentions=[{"id": 3, "name_zh": "腾讯", "depth": "title", "depth_label": "标题", "hit_count": 2}],
        digest={"model": "grok-4.7", "summary": "摘要一句", "points": ["要点"]},
        quoted={"heading": "第一节", "body": "被引用的段落"},
    )
    assert "正文 · 中英对照" in html
    assert 'href="/articles/1/zh"' in html
    assert 'href="/articles/1/original"' in html
    assert "你好" in html
    assert "问 AI：这篇讲了什么" in html
    assert "被引用的段落" in html
    assert "腾讯" in html
