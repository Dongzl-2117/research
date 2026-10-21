from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from researchlib.web.present import recent_blurb

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ROOT / "src" / "researchlib" / "web" / "templates"


def _env():
    return Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["html"]))


def test_home_recent_views_toggle_is_a_button():
    html = _env().get_template("home.html").render(
        title="按天",
        canonical="https://investment.zldong.com/",
        public_host="investment.zldong.com",
        query="",
        nav="archive",
        sample_banner=False,
        sources=[],
        active_source="",
        notices=[],
        stale=[],
        holdings=[
            {
                "id": 1,
                "name": "腾讯",
                "recent": 5,
                "latest": None,
                "view": {
                    "stance": "positive",
                    "stance_label": "看好",
                    "relative": "8 小时前",
                    "date": "2024-10-19",
                    "article_id": 9,
                    "chunk": None,
                },
                "summary": "导读：视频号广告库存还能抬。",
            }
        ],
        days=[],
        day_scope="",
        empty="还没有文章。",
        page=1,
        pages=1,
        total=0,
        dated_days=0,
        undated=0,
    )
    assert 'aria-label="最近观点"' in html
    assert 'class="recent-views-toggle"' in html
    assert 'aria-expanded="true"' in html
    assert 'aria-controls="recent-views-list"' in html
    assert 'id="recent-views-list"' in html
    assert 'href="/companies">最近观点</a>' in html
    assert "research-recent-views" in html
    assert "腾讯" in html
    assert "看好" in html
    assert 'class="recent-views-line"' in html
    assert "导读：视频号广告库存还能抬。" in html
    assert 'class="recent-item-summary"' in html


def test_recent_blurb_prefers_stored_digest():
    assert recent_blurb({"judgment": "判断句"}, {"summary": "导读句"}) == "导读句"
    assert recent_blurb({"judgment": "判断句"}, None) == "判断句"
    assert recent_blurb(None, None) == ""


class _NavSource:
    def __init__(self, source_id, name, timeline, kind="markdown"):
        self.id = source_id
        self.name = name
        self.timeline = timeline
        self.kind = kind


def test_sidebar_sources_are_one_list():
    html = _env().get_template("base.html").render(
        title="按天",
        canonical="https://investment.zldong.com/",
        public_host="investment.zldong.com",
        query="",
        nav="archive",
        sample_banner=False,
        active_source="fangwei-mp",
        sources=[
            _NavSource("fangwei-mp", "方伟公众号", True),
            _NavSource("linwenfeng-feishu", "林文丰飞书", False, "feishu"),
        ],
    )
    body = html.split("<main", 1)[0]
    group = body.split('<div class="nav-label">来源</div>', 1)[1].split("</nav>", 1)[0]
    assert body.count(">来源<") == 1
    assert ">文库<" not in body
    assert 'href="/?source=fangwei-mp"' in group
    assert 'href="/sources/linwenfeng-feishu/tree"' in group
    assert group.count("nav-link") == 2
    assert group.count('r="3.2"') == 2
    assert "M6 5.5h4.5" not in group


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
    description = "方伟微信公众号历史归档"
    timeline = True


def test_article_template_is_title_body_and_bilingual_tabs():
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
        quoted={"heading": "第一节", "body": "被引用的段落"},
    )
    assert 'class="lang-tabs"' in html and "正文 · 中英对照" not in html
    assert 'href="/articles/1/zh"' in html
    assert 'href="/articles/1/original"' in html
    assert "你好" in html
    assert "被引用的段落" in html
    assert "Note" in html
    assert "方伟" in html
    assert "打开原文" not in html
    assert "问 AI：这篇讲了什么" not in html
    assert "<h2>来源</h2>" not in html
    assert "方伟微信公众号历史归档" not in html
    assert "AI 整合" not in html


def test_day_template_opens_on_a_summary():
    html = _env().get_template("day.html").render(
        title="10月19日",
        canonical="https://investment.zldong.com/days/2024-10-19",
        public_host="investment.zldong.com",
        query="",
        nav="archive",
        sample_banner=False,
        sources=[],
        active_source="",
        back_href="/",
        year=2024,
        weekday="星期六",
        label="10月19日",
        count=1,
        company=None,
        author_name="",
        prev_href="",
        next_href="/days/2024-10-20",
        answer=None,
        brief={
            "lead": "方伟：这篇短帖在讲宁德时代。",
            "authors": 1,
            "companies": [{"id": 9, "name": "宁德时代"}],
            "sections": [
                {
                    "author": "方伟",
                    "count": 1,
                    "items": [
                        {
                            "id": 4,
                            "plain_title": "方伟知识星球短帖",
                            "iso": "2024-10-19T13:29:00+08:00",
                            "time": "13:29",
                            "blurb": "这篇短帖在讲宁德时代。",
                        }
                    ],
                }
            ],
        },
    )
    assert "日汇总" in html
    assert "方伟：这篇短帖在讲宁德时代。" in html
    assert "13:29" in html
    assert "AI 整合" not in html
    assert 'href="/days/2024-10-20"' in html
