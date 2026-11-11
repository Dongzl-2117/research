"""Stratechery 导出的引文、列表项写了两遍：单看原文、.md 接口、中英对照和只看中文都收成一份。"""

import re
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from researchlib.ingest.model import ParsedDoc
from researchlib.ingest.store import store_documents
from researchlib.sources import load_sources
from researchlib.web.app import create_app

SHANGHAI = ZoneInfo("Asia/Shanghai")
QUOTE = "A note on these numbers comes first."
ITEM = "which keeps growing every single quarter."

BODY = (
    "The opening paragraph sets up the argument about three businesses, how each of them makes money, "
    "and why the next few years matter more than the last few for every one of them.\n"
    "\n"
    f"> {QUOTE}\n"
    "\n"
    f"{QUOTE}\n"
    "\n"
    f"- Search: The first point is about the ads business , {ITEM}\n"
    "\n"
    f"**Search:** The first point is about the [ads business](https://example.com/ads), {ITEM}\n"
    "\n"
    "The closing paragraph wraps it up."
)

ZH_HTML = (
    "<p>开头这一段讲三门生意各自怎么赚钱，以及为什么接下来几年比过去几年更要紧。</p>"
    "<blockquote><p>先说一下这些数字。</p></blockquote>"
    "<p>先说一下这些数字。</p>"
    "<ul><li><p>搜索：第一点是广告业务，每个季度都在长。</p></li></ul>"
    "<p><strong>搜索：</strong>第一点是广告业务，每个季度都在长。</p>"
    "<p>最后一段收尾。</p>"
)


def _store(conn, settings, external_id: str = "2026/three-businesses", body: str = BODY) -> int:
    sources = {source.id: source for source in load_sources(settings.sources_path)}
    store_documents(
        conn,
        source=sources["stratechery"],
        docs=[
            ParsedDoc(
                external_id=external_id,
                title="Three Businesses",
                published_at=datetime(2026, 5, 2, 8, 0, tzinfo=SHANGHAI),
                body_md=body,
                raw_meta={"language": "en"},
                image_refs=[],
            )
        ],
        data_dir=settings.data_dir,
        archive_files=1,
        images_in_archive=0,
        errors=[],
    )
    conn.commit()
    row = conn.execute("SELECT id FROM articles WHERE external_id = %s", (external_id,)).fetchone()
    return row["id"]


def _translate(conn, article_id: int, zh_html: str, segments: int) -> None:
    content_hash = conn.execute("SELECT content_hash FROM articles WHERE id = %s", (article_id,)).fetchone()[
        "content_hash"
    ]
    conn.execute(
        """
        INSERT INTO article_translations (article_id, content_hash, title_zh, body_html, body_text, complete, segments)
        VALUES (%s, %s, '三门生意', %s, %s, true, %s)
        """,
        (article_id, content_hash, zh_html, zh_html, segments),
    )
    conn.commit()


def _prose(page: str) -> str:
    return page.split('<div class="prose', 1)[1].split("</div>", 1)[0]


def test_original_only_page_and_markdown_show_each_quote_once(conn, settings):
    article_id = _store(conn, settings)
    with TestClient(create_app(settings)) as client:
        page = client.get(f"/articles/{article_id}").text
        md = client.get(f"/api/v1/articles/{article_id}.md").text
    prose = _prose(page)
    assert prose.count(QUOTE) == 1
    assert prose.count(ITEM) == 1
    item = prose.split("<li>", 1)[1].split("</li>", 1)[0]
    # 列表项里换成了带加粗和链接的那份。
    assert "<strong>Search:</strong>" in item and "https://example.com/ads" in item
    assert md.count(QUOTE) == 1
    assert "- **Search:** The first point is about the [ads business](https://example.com/ads)" in md
    # 库里的原文不改。
    stored = conn.execute("SELECT body_md FROM articles WHERE id = %s", (article_id,)).fetchone()["body_md"]
    assert stored.count(QUOTE) == 2


def test_bilingual_and_chinese_pages_show_each_quote_once(conn, settings):
    article_id = _store(conn, settings)
    _translate(conn, article_id, ZH_HTML, 6)
    with TestClient(create_app(settings)) as client:
        pairs = client.get(f"/articles/{article_id}").text
        chinese = client.get(f"/articles/{article_id}/zh").text
        original = client.get(f"/articles/{article_id}/original").text
    # 对照页和原文页同一条规则：纯文字那一对不再显示，留带链接、加粗的那份，套回引文、列表项里。
    assert pairs.count('class="prose bilingual-en with-toolbar"') == 4
    assert pairs.count(QUOTE) == 1 and pairs.count(ITEM) == 1
    assert pairs.count("先说一下这些数字。") == 1 and pairs.count("第一点是广告业务") == 1
    assert f"<blockquote><p>{QUOTE}</p></blockquote>" in pairs
    assert "<ul><li><p><strong>Search:</strong> The first point is about the" in pairs
    assert 'about the <a href="https://example.com/ads">ads business</a>' in pairs
    assert "<ul><li><p><strong>搜索：</strong>第一点是广告业务" in pairs
    # 只看中文：一样收成一份，段落号还在。
    prose = _prose(chinese)
    assert prose.count("先说一下这些数字。") == 1 and prose.count("第一点是广告业务") == 1
    assert "<blockquote" in prose and "<strong>搜索：</strong>第一点是广告业务" in prose
    assert {f'id="p-{number}"' for number in range(1, 7)} <= set(re.findall(r'id="p-\d+"', prose))
    # 点「原文」只看英文：收成一份。
    assert _prose(original).count(QUOTE) == 1
    assert _prose(original).count(ITEM) == 1


ANSWER = "We will use the same ad units, and the same targeting."
QA_BODY = (
    "The opening paragraph sets up the interview about logged-out users, what they are worth, "
    "and why the answer matters for the advertising business over the next few years.\n"
    "\n"
    "> Q: How do you think about the users who are not logged in?\n"
    ">\n"
    f"> CEO: {ANSWER}\n"
    "\n"
    "**Q:** How do you think about the users who are not logged in?\n"
    "\n"
    "**CEO:** We will use the same [ad units](https://example.com/ads), and the same targeting.\n"
    "\n"
    "The closing paragraph wraps it up."
)
QA_ZH = (
    "<p>开头这一段讲没登录的用户值多少钱，以及这个答案为什么对接下来几年的广告生意要紧。</p>"
    "<blockquote><p>问：你怎么看那些没登录的用户？</p><p>CEO：我们会用同样的广告单元，定向也一样。</p></blockquote>"
    "<p><strong>问：</strong>你怎么看那些没登录的用户？</p>"
    '<p><strong>CEO：</strong>我们会用同样的<a href="https://example.com/ads">广告单元</a>，定向也一样。</p>'
    "<p>最后一段收尾。</p>"
)


def test_quote_written_in_paragraphs_shows_once_on_every_page(conn, settings):
    """纯文字那份也分段的引文：原文页、对照页、只看中文页都只留带格式的那一份。"""
    article_id = _store(conn, settings, "2026/logged-out-users", QA_BODY)
    with TestClient(create_app(settings)) as client:
        before = _prose(client.get(f"/articles/{article_id}").text)
    assert before.count("not logged in") == 1 and before.count("<strong>CEO:</strong>") == 1
    _translate(conn, article_id, QA_ZH, 5)
    with TestClient(create_app(settings)) as client:
        pairs = client.get(f"/articles/{article_id}").text
        chinese = _prose(client.get(f"/articles/{article_id}/zh").text)
    assert pairs.count("not logged in") == 1 and pairs.count("and the same targeting") == 1
    assert pairs.count("你怎么看那些没登录的用户") == 1 and pairs.count("定向也一样") == 1
    assert "<blockquote><p><strong>Q:</strong> How do you think" in pairs
    assert chinese.count("你怎么看那些没登录的用户") == 1 and chinese.count("定向也一样") == 1
    quote = chinese.split("<blockquote", 1)[1].split("</blockquote>", 1)[0]
    assert "<strong>问：</strong>" in quote and 'href="https://example.com/ads"' in quote


NESTED_BODY = (
    "The opening paragraph sets up the argument about copying a rival product, what it costs, "
    "and why the answer matters for the advertising business over the next few years.\n"
    "\n"
    "> From the encyclopedia definition:\n"
    "> Web 2.0 refers to websites that emphasize user-generated content.\n"
    "\n"
    "From the [encyclopedia](https://example.com/wiki) definition:\n"
    "\n"
    "> Web 2.0 refers to websites that emphasize user-generated content.\n"
    "\n"
    "That is where the argument starts.\n"
    "\n"
    "> This is why the copy works for the company and its users.\n"
    "> You cannot just recreate another product.\n"
    "> […]\n"
    "> You have a completely different audience.\n"
    "> That right there is the tell.\n"
    "\n"
    "This is why the copy [works](https://example.com/works) for the company and its users.\n"
    "\n"
    "> You cannot just recreate another product.\n"
    "> […]\n"
    "> You have a completely different audience.\n"
    "\n"
    "You cannot just recreate another product.\n"
    "\n"
    "[…]\n"
    "\n"
    "You have a completely different audience.\n"
    "\n"
    "That right there is the tell.\n"
    "\n"
    "The closing paragraph wraps it up."
)
# 旧稿（没有回执）照原文的分行译：引文里每行一个 <br>。
NESTED_ZH = (
    "<p>开头这一段讲照搬对手的产品要花多少，以及这个答案为什么对接下来几年的广告生意要紧。</p>"
    "<blockquote><p>百科的定义：<br />Web 2.0 指强调用户生成内容的网站。</p></blockquote>"
    '<p>百科的<a href="https://example.com/wiki">定义</a>：</p>'
    "<blockquote><p>Web 2.0 指强调用户生成内容的网站。</p></blockquote>"
    "<p>论证从这里开始。</p>"
    "<blockquote><p>这就是照搬对公司和用户都管用的原因。<br />你不能只是复制另一个产品。<br />[…]<br />"
    "你面对的是完全不同的受众。<br />这就是关键所在。</p></blockquote>"
    '<p>这就是照搬对公司和用户都<a href="https://example.com/works">管用</a>的原因。</p>'
    "<blockquote><p>你不能只是复制另一个产品。<br />[…]<br />你面对的是完全不同的受众。</p></blockquote>"
    "<p>你不能只是复制另一个产品。</p>"
    "<p>[…]</p>"
    "<p>你面对的是完全不同的受众。</p>"
    "<p>这就是关键所在。</p>"
    "<p>最后一段收尾。</p>"
)


def test_quotes_inside_the_copies_show_once_on_every_page(conn, settings):
    """带格式那几段里套着引文、引文里的引文自己也写了两遍：原文页、.md、对照页、只看中文页都只留一份。"""
    article_id = _store(conn, settings, "2026/copying-well", NESTED_BODY)
    with TestClient(create_app(settings)) as client:
        before = _prose(client.get(f"/articles/{article_id}").text)
        md = client.get(f"/api/v1/articles/{article_id}.md").text
    assert before.count("user-generated content") == 1 and before.count("You cannot just") == 1
    assert before.count("the tell") == 1 and len(re.findall(r"<blockquote\b", before)) == 4
    assert md.count("You cannot just") == 1 and "> > You cannot just recreate another product." in md
    _translate(conn, article_id, NESTED_ZH, 13)
    with TestClient(create_app(settings)) as client:
        pairs = client.get(f"/articles/{article_id}").text
        chinese = _prose(client.get(f"/articles/{article_id}/zh").text)
    for text in ("user-generated content", "You cannot just", "the tell"):
        assert pairs.count(text) == 1, text
    for text in ("指强调用户生成内容", "你不能只是", "完全不同的受众", "这就是关键所在"):
        assert pairs.count(text) == 1, text
        assert chinese.count(text) == 1, text
    assert len(re.findall(r"<blockquote\b", chinese)) == 4
