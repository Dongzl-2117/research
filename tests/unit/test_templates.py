from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from researchlib.web.assets import asset_url
from researchlib.web.present import company_names, company_summary, recent_blurb

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ROOT / "src" / "researchlib" / "web" / "templates"


def _env():
    env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["html"]))
    env.globals["asset"] = asset_url  # 和 routes.TEMPLATES 一样
    return env


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
    tencent = ["腾讯", "Tencent"]
    assert recent_blurb({"judgment": "判断句"}, {"summary": "导读：腾讯的广告库存还能抬。"}, tencent) == (
        "导读：腾讯的广告库存还能抬。"
    )
    assert recent_blurb({"judgment": "判断句"}, None, tencent) == "判断句"
    assert recent_blurb(None, None, tencent) == ""


def test_recent_blurb_skips_a_digest_about_another_company():
    """公司只是文中的例子时，这篇的导读讲的是别的公司，不放在这家公司下面。"""
    meituan = company_names({"name_zh": "美团", "name_en": "Meituan", "aliases": ["美团点评", "M"]})
    assert meituan == ["美团", "Meituan", "美团点评"]
    other = {"summary": "导读：拼多多的佣金率还在往下压。"}
    assert company_summary(other, meituan) == ""
    assert recent_blurb({"judgment": "外卖的补贴还要打很久"}, other, meituan) == "外卖的补贴还要打很久"
    # 英文名、大小写不同也认。
    assert company_summary({"summary": "MEITUAN 的配送成本又降了。"}, meituan) == "MEITUAN 的配送成本又降了。"
    assert company_summary(None, meituan) == ""


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


STATIC = ROOT / "src" / "researchlib" / "web" / "static"


def _media_blocks(css, query):
    """按出现顺序取出每个 ``@media <query> {`` 块的内容，按花括号配对。"""
    head = "@media " + query + " {"
    blocks, pos = [], css.find(head)
    while pos >= 0:
        start = i = pos + len(head)
        depth = 1
        while depth:
            depth += {"{": 1, "}": -1}.get(css[i], 0)
            i += 1
        blocks.append(css[start:i - 1])
        pos = css.find(head, i)
    return blocks


def test_tablet_widths_get_a_side_rail_and_phones_keep_the_bottom_bar():
    """600 到 1023px（手机横屏、折叠屏展开、iPad 竖屏）导航在左边的图标栏，不再顶栏加底部栏。

    Zhilin 11-10 用折叠屏横屏看，网页宽约 954，以前到 1024 才有侧栏，上下两条占掉四分之一的高度。
    600 以下照旧底部栏；只有底部栏在的宽度，「问这篇」的面板和按钮才让到它上面。
    """
    css = (STATIC / "site.css").read_text(encoding="utf-8")
    rail = _media_blocks(css, "(min-width: 600px) and (max-width: 1023px)")
    assert ".mobile-bar, .tabbar { display: none; }" in rail[0]
    assert "display: flex" in rail[0].split("  .sidebar {", 1)[1].split("}", 1)[0]
    assert "translateY(calc(var(--theme-index, 1) * 100%))" in rail[0]
    [phone] = _media_blocks(css, "(max-width: 599px)")
    assert ".article-chat { bottom: calc(54px + env(safe-area-inset-bottom)); }" in phone
    assert not any("54px" in block for block in rail + _media_blocks(css, "(max-width: 1023px)"))
    ask = (STATIC / "ask-chat.css").read_text(encoding="utf-8")
    [ask_rail] = _media_blocks(ask, "(min-width: 600px) and (max-width: 1023px)")
    assert ".shell:has(.ask-workspace) .frame { height: 100dvh; }" in ask_rail


def test_theme_thumb_position_comes_from_a_css_variable():
    """脚本只写第几个选项（--theme-index），滑块横着走还是竖着走由样式表定：图标栏里开关是竖的。"""
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    assert 'thumb.style.setProperty("--theme-index", String(index))' in base
    assert "translateX(" not in base
    thumb = (STATIC / "site.css").read_text(encoding="utf-8").split(".theme-thumb {", 1)[1].split("}", 1)[0]
    assert "transform: translateX(calc(var(--theme-index, 1) * 100%));" in thumb


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


class _SameNameSource:
    id = "chengfengpolang"
    name = "乘风破浪"
    author = "乘风破浪"
    description = "星球归档"
    timeline = True


def _byline(source, **extra):
    html = _env().get_template("article.html").render(
        title="Note",
        canonical="https://investment.zldong.com/articles/1",
        public_host="investment.zldong.com",
        query="",
        nav="archive",
        sample_banner=False,
        sources=[],
        active_source="",
        source=source,
        article={"id": 1, "title": "Note"},
        iso="2026-01-01",
        date="2026-01-01",
        time="12:00",
        body_language="original",
        body_html="<p>正文</p>",
        original_url="",
        siblings=[],
        **extra,
    )
    return html.split('class="byline"', 1)[1].split("</p>", 1)[0]


def test_article_byline_names_a_one_person_source_once():
    """来源名就是作者名（乘风破浪、孤独进化体、黑黑无常）时署名只写一次，和列表卡片一样。"""
    same = _byline(_SameNameSource())
    assert same.count("乘风破浪") == 1
    assert 'href="/?source=chengfengpolang"' in same
    assert same.count('aria-hidden="true">·</span>') == 2  # 名字 · 时间 · 问这篇
    both = _byline(_Source())
    assert "方伟公众号" in both and 'href="/authors/%E6%96%B9%E4%BC%9F"' in both
    guest = _byline(_SameNameSource(), guest=True, poster_name="王小明")
    assert "发帖人 王小明" in guest and "不是乘风破浪的原创" in guest


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
    assert html.index("<h1>") < html.index('class="byline"')
    assert "方伟" in html
    assert "打开原文" not in html
    assert "问 AI：这篇讲了什么" not in html
    assert 'id="article-chat"' in html
    assert 'data-article="1"' in html
    assert 'class="chat-toggle"' in html
    assert ">问 AI</span>" in html
    assert 'action="/ask"' in html
    assert "chat-chip" not in html
    assert "chat-hint" not in html
    assert 'class="chat-composer"' in html
    assert "Enter 发送" not in html
    assert "先看这篇" not in html
    assert 'id="article-chat-q"' in html
    assert 'placeholder=' not in html.split('id="article-chat-q"', 1)[1].split(">", 1)[0]
    assert "主要看这篇文章" not in html
    assert 'data-chat-close' in html
    assert 'aria-label="收起"' in html
    assert 'class="chat-split"' in html
    assert 'role="separator"' in html
    assert "research-article-chat" in html
    assert "<h2>来源</h2>" not in html
    assert "方伟微信公众号历史归档" not in html
    assert "AI 整合" not in html


def test_bilingual_template_hides_empty_english_on_leftover_zh():
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
        pairs=[
            {"en": "Hello", "zh": "你好", "anchor": "p-1"},
            {"en": "", "zh": "多出来的译文。", "anchor": "p-2"},
            {"en": "", "zh": None, "anchor": None},
        ],
        body_html=None,
        original_url="",
        siblings=[],
        quoted=None,
    )
    assert html.count("bilingual-en") == 1
    assert html.count("bilingual-zh") == 2
    assert "多出来的译文。" in html
    assert 'id="p-2"' in html
    assert html.count("bilingual-pair") == 2


def _pairs_page(pairs, **extra):
    return _env().get_template("article.html").render(
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
        time="",
        relative="",
        title_zh="",
        body_language="pairs",
        has_translation=True,
        translation_complete=True,
        pairs=pairs,
        body_html=None,
        original_url="",
        siblings=[],
        quoted=None,
        **extra,
    )


def test_bilingual_page_keeps_mapping_status_out_of_the_reader_text():
    """「旧译文未记录源段映射」这类话是维护用的：页面上不显示，状态写在 data 属性里。"""
    pairs = [{"en": "Hello", "zh": "你好", "anchor": "p-1"}]
    for status in ("legacy", "legacy-anchors", "mapped"):
        html = _pairs_page(pairs, pair_correspondence=status, pair_notice="")
        assert f'class="bilingual" data-correspondence="{status}">' in html
        assert "body-note" not in html
        for jargon in ("源段映射", "按结构对照", "核验", "锚点", "粗段"):
            assert jargon not in html


def test_bilingual_page_tells_the_reader_how_to_read_two_full_texts():
    """退成两份全文时只说怎么读；具体原因只进 data-note，不进正文。"""
    reason = "分段无法保真，以下保留完整原文和译文。"
    html = _pairs_page(
        [{"en": "Hello world.", "zh": None, "anchor": None}, {"en": "", "zh": "你好，世界。", "anchor": None}],
        pair_correspondence="unmapped",
        pair_notice=reason,
    )
    note = '<p class="body-note">这篇译文和原文没法逐段对上，下面先放完整原文，再放完整译文。</p>'
    assert html.count(note) == 1
    assert html.count(reason) == 1
    assert f'data-correspondence="unmapped" data-note="{reason}">' in html
    assert html.index(note) < html.index("Hello world.") < html.index("你好，世界。")


def test_day_template_lists_the_day_and_offers_to_ask():
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
        ask_href="/ask?preset=day&from=2024-10-19&to=2024-10-19",
        brief={
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
    # 导读只在文章那一行出现一次，上面不再拿它冒充这一天的总结。
    assert html.count("这篇短帖在讲宁德时代。") == 1
    assert "这一天的总结" not in html
    assert 'href="/ask?preset=day&amp;from=2024-10-19&amp;to=2024-10-19"' in html
    assert "13:29" in html
    assert "AI 整合" not in html
    assert 'href="/days/2024-10-20"' in html
