"""隐藏内容的其余漏口（全栈审核 B3、R6、B5、V11）：

- 回答页补「漏了的文章」不收隐藏文章、不按编号显示它的标题（B3）；
- 旧回答引用了后来被隐藏的文章：出处原文、回答正文都收起，接口同样（R6）；
- 还没清评论的旧星球帖：接口的 chunks、文章页 ?chunk= 不给读者评论那几块（B5）；
- 论点的输入不收短帖（hidden），和公司页时间线一个口径（V11）。
"""

from datetime import datetime

from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from researchlib import theses
from researchlib.ingest.model import ParsedDoc
from researchlib.ingest.store import store_documents
from researchlib.sources import load_sources
from researchlib.textutil import SHANGHAI
from researchlib.web.app import create_app

GUEST_TEXT = "网友问的泡泡玛特一句，正文不该在网站上出现。"
OWNER_TEXT = "这是星主自己的长文判断，泡泡玛特的海外门店坪效还在往上走，多写一点免得被当成空壳。"


def _source(settings, source_id):
    return next(source for source in load_sources(settings.sources_path) if source.id == source_id)


def _store(conn, settings, source_id, docs):
    store_documents(
        conn,
        source=_source(settings, source_id),
        docs=docs,
        data_dir=settings.data_dir,
        archive_files=len(docs),
        images_in_archive=0,
        errors=[],
    )


def _id(conn, external_id):
    return conn.execute("SELECT id FROM articles WHERE external_id = %s", (external_id,)).fetchone()["id"]


def _zsxq(external_id, title, body, role, when):
    return ParsedDoc(
        external_id=external_id,
        title=title,
        published_at=when,
        body_md=body,
        raw_meta={
            "format": "markdown",
            "filename": f"{external_id}.md",
            "poster_name": "方伟Rey" if role == "owner" else "王小明",
            "poster_role": role,
            "topic_id": external_id,
        },
        image_refs=[],
    )


def _two_posts(conn, settings):
    when = datetime(2026, 10, 27, 16, 52, tzinfo=SHANGHAI)
    _store(
        conn,
        settings,
        "fangwei-zsxq",
        [
            _zsxq("leak-guest", "网友问的泡泡玛特一句", GUEST_TEXT + "\n", "guest", when),
            _zsxq("leak-owner", "星主谈泡泡玛特坪效", OWNER_TEXT + "\n", "owner", when.replace(hour=18)),
        ],
    )
    return _id(conn, "leak-guest"), _id(conn, "leak-owner")


def _citation(article_id, n, text, cited=True):
    return {
        "n": n,
        "article_id": article_id,
        "ord": 0,
        "title": "当时存下的标题快照",
        "heading": "",
        "text": text,
        "source_id": "fangwei-zsxq",
        "source": "方伟星球",
        "author": "方伟",
        "date": "2026-10-27",
        "url": f"/articles/{article_id}?chunk=0",
        "lang": "",
        "matched": [],
        "before": "",
        "after": "",
        "semantic": False,
        "cited": cited,
    }


def _answer(conn, answer, citations):
    answer_id = conn.execute(
        "INSERT INTO llm_answers (question, scope, answer, citations, model, status)"
        " VALUES ('泡泡玛特的坪效怎么样', %s, %s, %s, 'test', 'done') RETURNING id",
        (Jsonb({}), answer, Jsonb(citations)),
    ).fetchone()["id"]
    conn.commit()
    return answer_id


def _hide(conn, article_id):
    conn.execute(
        "UPDATE articles SET raw_meta = raw_meta || '{\"poster_role\": \"guest\"}'::jsonb WHERE id = %s", (article_id,)
    )
    conn.commit()


def test_missing_article_form_does_not_accept_or_show_hidden_posts(conn, settings):
    guest_id, owner_id = _two_posts(conn, settings)
    answer_id = _answer(conn, "坪效还在往上走[1]。", [_citation(owner_id, 1, OWNER_TEXT)])
    with TestClient(create_app(settings)) as client:
        refused = client.post(
            f"/ask/{answer_id}/feedback", data={"label": "missing", "ref": str(guest_id)}, follow_redirects=False
        )
        assert refused.status_code == 303
        assert "feedback_error=absent" in refused.headers["location"]
        page = client.get(f"/ask/{answer_id}").text
        assert "网友问的泡泡玛特一句" not in page

        # 以前收下了的（或者收下以后这篇才被隐藏的）：回答页也不按编号把标题显示出来。
        conn.execute(
            "INSERT INTO answer_feedback (answer_id, question, scope, article_id, chunk_ord, label)"
            " VALUES (%s, '泡泡玛特的坪效怎么样', '{}'::jsonb, %s, NULL, 'missing')",
            (answer_id, guest_id),
        )
        conn.commit()
        page = client.get(f"/ask/{answer_id}").text
        assert "网友问的泡泡玛特一句" not in page
        assert f"/articles/{guest_id}\"" not in page

        # 看得见的文章照常能补、照常列出来。
        added = client.post(
            f"/ask/{answer_id}/feedback",
            data={"label": "missing", "ref": f"/articles/{owner_id}"},
            follow_redirects=False,
        )
        assert added.headers["location"].endswith("#missing")
        assert "星主谈泡泡玛特坪效" in client.get(f"/ask/{answer_id}").text


def test_old_answer_withholds_passages_from_posts_hidden_later(conn, settings):
    _guest_id, owner_id = _two_posts(conn, settings)
    other = "另一段星主写的原文，回答里没有引用它，只是检索时一起拿到的。"
    answer_id = _answer(
        conn,
        "坪效还在往上走[1]。",
        [_citation(owner_id, 1, OWNER_TEXT), _citation(owner_id, 2, other, cited=False)],
    )
    with TestClient(create_app(settings)) as client:
        before = client.get(f"/ask/{answer_id}").text
        assert "坪效还在往上走" in before and "海外门店坪效" in before

        _hide(conn, owner_id)
        page = client.get(f"/ask/{answer_id}")
        assert page.status_code == 200
        assert "海外门店坪效" not in page.text
        assert other not in page.text
        assert "坪效还在往上走" not in page.text
        assert "这条出处已不展示" in page.text
        assert "请重新问一次" in page.text

        data = client.get(f"/api/v1/answers/{answer_id}").json()
        assert data["answer"] is None
        assert "请重新问一次" in data["error"]
        assert "海外门店坪效" not in str(data) and other not in str(data)
        assert all(item.get("withheld") for item in data["citations"])


def test_only_uncited_passage_hidden_keeps_answer(conn, settings):
    guest_id, owner_id = _two_posts(conn, settings)
    answer_id = _answer(
        conn,
        "坪效还在往上走[1]。",
        [_citation(owner_id, 1, OWNER_TEXT), _citation(guest_id, 2, GUEST_TEXT, cited=False)],
    )
    with TestClient(create_app(settings)) as client:
        page = client.get(f"/ask/{answer_id}").text
        assert "坪效还在往上走" in page and "海外门店坪效" in page
        assert "正文不该在网站上出现" not in page
        data = client.get(f"/api/v1/answers/{answer_id}").json()
        assert data["answer"] == "坪效还在往上走[1]。"
        assert "正文不该在网站上出现" not in str(data)


def test_unrepaired_comment_chunks_are_not_served(conn, settings):
    when = datetime(2026, 9, 27, 14, 41, tzinfo=SHANGHAI)
    body = (
        "Temu：值得重视的关税利好\n\n"
        "中美这次国事访问后达成的对等降税安排，主要是针对几类商品：小家电、玩具、节庆装饰品等。\n\n"
        "## 评论（2）\n\n"
        "- **王小明**（2026-09-27 15:00）\n  同意，半托管的灯笼订单早就铺开了。\n"
        "- **乘风破浪（作者）**（2026-09-27 15:10）\n  铺开了，这次降税对小家电最直接。\n"
    )
    _store(
        conn,
        settings,
        "chengfengpolang",
        [ParsedDoc("cf-chunks", "Temu：值得重视的关税利好", when, body, {"format": "markdown"}, [])],
    )
    article_id = _id(conn, "cf-chunks")
    rows = conn.execute(
        "SELECT ord, heading, body FROM article_chunks WHERE article_id = %s AND lang = '' ORDER BY ord", (article_id,)
    ).fetchall()
    leaky = [row for row in rows if "灯笼订单" in row["body"]]
    assert leaky, "库里的块还带着读者评论，这个测试才有意义"

    with TestClient(create_app(settings)) as client:
        data = client.get(f"/api/v1/articles/{article_id}").json()
        assert "王小明" not in str(data) and "灯笼订单" not in str(data)
        assert any("对等降税" in chunk["text"] for chunk in data["chunks"])
        page = client.get(f"/articles/{article_id}", params={"chunk": str(leaky[0]["ord"])})
        assert page.status_code == 200
        assert "灯笼订单" not in page.text and "王小明" not in page.text


def test_thesis_input_leaves_out_short_posts(conn, settings):
    _guest_id, owner_id = _two_posts(conn, settings)
    company = conn.execute(
        "INSERT INTO companies (name_zh) VALUES ('测试泡泡')"
        " ON CONFLICT (name_zh) DO UPDATE SET name_zh = EXCLUDED.name_zh RETURNING id"
    ).fetchone()["id"]
    conn.execute(
        "INSERT INTO company_views"
        " (article_id, company_id, about, judgment, stance, content_hash, model, prompt_version)"
        " VALUES (%s, %s, true, '坪效还在往上走', 'positive', 'h', 'test', 1)",
        (owner_id, company),
    )
    conn.commit()
    by_id = {source.id: source for source in load_sources(settings.sources_path)}
    assert [view["article_id"] for view in theses.views_for(conn, company, by_id, settings)] == [owner_id]
    assert theses.companies_with_views(conn, list(by_id), minimum=1)

    conn.execute(
        "UPDATE articles SET raw_meta = raw_meta || '{\"hidden\": \"short_post\"}'::jsonb WHERE id = %s", (owner_id,)
    )
    conn.commit()
    assert theses.views_for(conn, company, by_id, settings) == []
    assert not [row for row in theses.companies_with_views(conn, list(by_id), minimum=1) if row["id"] == company]
