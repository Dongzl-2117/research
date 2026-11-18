from datetime import datetime, timedelta
from urllib.parse import urlsplit

from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from researchlib.article_history import confirm_commits
from researchlib.web.app import create_app


def _insert(conn, *, source="fangwei-mp", body="第一版假设", meta=None):
    row = conn.execute(
        """
        INSERT INTO articles (source_id, external_id, title, published_at, body_md, body_text, content_hash, raw_meta)
        VALUES (%s, 'history', '估值记录', '2020-01-01', %s, %s, 'first', %s) RETURNING id
    """,
        (source, body, body, Jsonb(meta or {})),
    ).fetchone()
    return row["id"]


def _now(conn):
    return conn.execute("SELECT clock_timestamp() AS t").fetchone()["t"]


def _mcp(client, name, args):
    return client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}},
    ).json()["result"]


def test_history_uses_observed_versions_and_keeps_earlier_answers_reproducible(conn, settings):
    article_id = _insert(conn)
    # 写事务还没提交，当时不能从另一个会话读到这篇。
    during_import = _now(conn)
    confirm_commits(conn)
    conn.commit()
    confirm_commits(conn)
    first = _now(conn)
    conn.commit()
    endpoint = f"/api/v1/articles/{article_id}"
    with TestClient(create_app(settings)) as client:

        def get(when):
            return client.get(endpoint, params={"as_of": when.isoformat()}).json()

        assert get(first - timedelta(days=30))["status"] == "not_yet_archived"
        response = get(during_import)
        assert response["status"] == "unverified" and "body_markdown" not in response
        original = get(first)
        assert original["status"] == "verified" and original["body_markdown"] == "第一版假设"
        assert datetime.fromisoformat(original["published_at"]) < datetime.fromisoformat(original["first_seen_at"])

        conn.execute("UPDATE articles SET body_md='未提交中间稿' WHERE id=%s", (article_id,))
        conn.execute("UPDATE articles SET body_md='第二版' WHERE id=%s", (article_id,))
        between = _now(conn)
        conn.commit()
        confirm_commits(conn)
        second = _now(conn)
        conn.commit()
        assert get(between)["status"] == "unverified"
        assert get(second)["body_markdown"] == "第二版"
        assert get(first) == original
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM article_evidence_history WHERE article_id=%s", (article_id,)
            ).fetchone()["n"]
            == 1
        )
        conn.execute("UPDATE articles SET body_md='第一版假设' WHERE id=%s", (article_id,))
        conn.commit()
        confirm_commits(conn)
        third = _now(conn)
        conn.commit()
        assert get(third)["body_markdown"] == original["body_markdown"] and get(second)["body_markdown"] == "第二版"
        assert get(third)["confirmed_at"] != original["confirmed_at"]
        page = client.get(f"/articles/{article_id}/history", params={"as_of": first.isoformat()})
        assert "第一版假设" in page.text and "第二版" not in page.text
        assert f"/articles/{article_id}/history" in client.get(f"/articles/{article_id}").text
        via_tool = _mcp(client, "get_article", {"id": article_id, "as_of": first.isoformat()})["structuredContent"]
        assert via_tool["body_markdown"] == original["body_markdown"]
        assert via_tool["confirmed_at"] == original["confirmed_at"]
        assert "第一版假设" in client.get(endpoint + ".md", params={"as_of": first.isoformat()}).text
        future = client.get(endpoint, params={"as_of": (_now(conn) + timedelta(days=1)).isoformat()})
        assert future.status_code == 400 and "body_markdown" not in future.json()
        assert client.get(endpoint, params={"as_of": "昨天"}).status_code == 400
        conn.execute("UPDATE articles SET raw_meta=%s WHERE id=%s", (Jsonb({"poster_role": "guest"}), article_id))
        conn.commit()
        assert client.get(endpoint, params={"as_of": first.isoformat()}).status_code == 404


def test_historical_replies_need_both_archive_and_reply_time(conn, settings):
    replies = [
        {"text": "已确认回复", "published_at": "2021-01-01T00:00:00+08:00"},
        {"text": "时间未知"},
        {"text": "未来的回复", "published_at": "2100-01-01T00:00:00+08:00"},
    ]
    aid = _insert(
        conn,
        source="fangwei-zsxq",
        body="主帖正文\n\n## 评论里的回复\n\n旧格式追加的观点",
        meta={"author_replies": replies},
    )
    conn.commit()
    confirm_commits(conn)
    mark = _now(conn)
    conn.commit()
    with TestClient(create_app(settings)) as client:
        response = client.get(f"/api/v1/articles/{aid}", params={"as_of": mark.isoformat()})
        assert response.json()["body_markdown"] == "主帖正文"
        assert [r["text"] for r in response.json()["author_replies"]] == ["已确认回复"]
        assert "旧格式追加" not in response.text and "未来的回复" not in response.text
        history_url = f"/articles/{aid}/history?through=2020-02-01"
        assert "早于本库首次入库" in client.get(history_url).text
        for page in ["/ask", "/search"]:
            dated = client.get(page, params={"to": "2020-02-01", "article": aid, "q": "估值记录"})
            assert "按发表日筛选" in dated.text and history_url in dated.text


def test_saved_citation_never_marks_reused_chunk_number(conn, settings):
    aid = _insert(conn, body="引用时的判断。")
    conn.execute("INSERT INTO article_chunks (article_id,ord,body) VALUES (%s,0,'引用时的判断。')", (aid,))
    items = [
        {
            "n": 1,
            "cited": True,
            "article_id": aid,
            "ord": 0,
            "text": "引用时的判断。",
            "lang": "",
            "source_id": "fangwei-mp",
            "title": "估值记录",
            "url": f"/articles/{aid}?chunk=0",
        }
    ]
    answer_id = conn.execute(
        """INSERT INTO llm_answers (question,answer,citations,status)
                               VALUES ('为什么？','作者给出了判断[1]。',%s,'done') RETURNING id""",
        (Jsonb(items),),
    ).fetchone()["id"]
    conn.commit()
    with TestClient(create_app(settings)) as client:
        payload = client.get(f"/api/v1/answers/{answer_id}").json()
        target = payload["citations"][0]["url"]
        assert "&evidence=" in target and "无法核对这条引用" not in client.get(target).text
        link = _mcp(client, "get_answer", {"id": answer_id})["structuredContent"]["citations"][0]["links"]["html"]
        assert urlsplit(link).query == urlsplit(target).query
        conn.execute("UPDATE article_chunks SET body='重切后的另一个判断。' WHERE article_id=%s AND ord=0", (aid,))
        conn.commit()
        page = client.get(target).text
        assert "无法核对这条引用" in page and 'class="is-quoted"' not in page
        assert client.get(f"/api/v1/answers/{answer_id}").json()["citations"][0]["text"] == "引用时的判断。"


def test_api_and_mcp_pagination_dates_and_stale_translations(conn, settings):
    aid = _insert(conn, body="现金流估值")
    conn.execute(
        """INSERT INTO article_chunks(article_id,ord,body)
                SELECT %s,n,'现金流估值第' || n || '节。' FROM generate_series(0,5) n""",
        (aid,),
    )
    conn.execute("INSERT INTO article_chunks(article_id,ord,body,lang) VALUES(%s,10000,'过期译文','zh')", (aid,))
    conn.execute(
        """INSERT INTO article_translations (article_id,content_hash,body_html,body_text)
           VALUES(%s,'old','<p>过期译文</p>','过期译文')""",
        (aid,),
    )
    conn.commit()
    with TestClient(create_app(settings)) as client:
        for response in [
            client.get(f"/api/v1/articles/{aid}").json(),
            _mcp(client, "get_article", {"id": aid})["structuredContent"],
        ]:
            assert "zh" not in {p["lang"] for p in response["chunks"]}
        params = {"q": "现金流估值", "article": aid, "level": "chunk", "limit": 2}
        first = client.get("/api/v1/search", params=params).json()
        second = _mcp(client, "search_library", params | {"offset": first["next_offset"]})["structuredContent"]
        assert [r["chunk"] for r in first["passages"] + second["passages"]] == [0, 1, 2, 3]
        last = client.get("/api/v1/search", params=params | {"offset": second["next_offset"]}).json()
        assert len(last["passages"]) == 2 and last["next_offset"] is None
        assert client.get("/api/v1/search", params=params | {"offset": 1000}).json()["passages"] == []
        for bad in ["2024-02-30", "2024-01-01typo", "20240101"]:
            assert client.get("/api/v1/search", params=params | {"from": bad}).status_code == 400
            assert _mcp(client, "search_library", params | {"to": bad})["isError"]
