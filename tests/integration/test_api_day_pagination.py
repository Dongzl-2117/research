"""日报分页按上海日历日返回可见总数、稳定页序和明确的结束位置。"""

import re

from fastapi.testclient import TestClient

from researchlib.web.app import create_app


def _mcp(client, day, **options):
    return client.post(
        "/api/mcp",
        json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "get_day", "arguments": {"date": day, **options}},
        },
    ).json()["result"]


def test_busy_day_can_be_read_to_the_last_article_via_rest_mcp_and_html(conn, settings):
    day = "2025-07-16"
    inserted = conn.execute(
        """
        INSERT INTO articles(source_id, external_id, content_hash, title, body_md, body_text, published_at)
        SELECT 'fangwei-mp', 'day-'||i, 'day-'||i, '测试文章'||i, '资料正文', '资料正文',
               '2025-07-16 15:59:59+00'
        FROM generate_series(1, 503) n(i)
        RETURNING id
        """
    ).fetchall()
    ids = sorted((row["id"] for row in inserted), reverse=True)
    # 新一天和同篇重复都不在这天的总数里。其余 501 条同秒发表，按 id 消除并列。
    conn.execute("UPDATE articles SET published_at = '2025-07-16 16:00:00+00' WHERE id = %s", (ids[0],))
    conn.execute("UPDATE articles SET duplicate_of = %s WHERE id = %s", (ids[2], ids[1]))
    conn.commit()
    wanted = ids[2:]
    path = f"/api/v1/days/{day}"
    with TestClient(create_app(settings)) as client:
        for fetch in (
            lambda **options: client.get(path, params=options).json(),
            lambda **options: _mcp(client, day, **options)["structuredContent"],
        ):
            first = fetch()
            assert first["from"] == first["to"] == day
            assert (first["total"], first["limit"], first["offset"], first["next_offset"]) == (501, 500, 0, 500)
            assert [item["id"] for item in first["items"]] == wanted[:500]
            last = fetch(limit=3, offset=first["next_offset"])
            assert (last["total"], last["offset"], last["next_offset"]) == (501, 500, None)
            assert [item["id"] for item in last["items"]] == wanted[500:]
            beyond = fetch(offset=700)
            assert (beyond["total"], beyond["items"], beyond["next_offset"]) == (501, [], None)
        next_day = client.get("/api/v1/days/2025-07-17").json()
        assert [item["id"] for item in next_day["items"]] == [ids[0]]
        empty = client.get("/api/v1/days/2025-07-18").json()
        assert (empty["total"], empty["items"], empty["next_offset"]) == (0, [], None)
        page = client.get(f"/days/{day}")
        assert page.status_code == 200 and '<span class="num">501</span> 篇' in page.text
        linked = {int(value) for value in re.findall(r'href="/articles/(\d+)"', page.text)}
        assert linked == set(wanted)


def test_day_rejects_invalid_calendar_dates_and_page_parameters(conn, settings):
    with TestClient(create_app(settings)) as client:
        for date_text, params in (
            ("20250716", {}), ("2025-W29-3", {}), ("2025-02-29", {}),
            ("2025-07-16", {"limit": 0}), ("2025-07-16", {"limit": 501}),
            ("2025-07-16", {"offset": -1}), ("2025-07-16", {"offset": 2**63}),
            ("2025-07-16", {"limit": 1.5}), ("2025-07-16", {"offset": "abc"}),
        ):
            result = client.get(f"/api/v1/days/{date_text}", params=params)
            assert result.status_code == 400
            assert _mcp(client, date_text, **params)["isError"] is True
        assert client.get("/api/v1/days/2024-02-29").status_code == 200
