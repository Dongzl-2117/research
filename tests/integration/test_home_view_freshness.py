import re
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from researchlib.views import PROMPT_VERSION
from researchlib.web.app import create_app


def test_home_fills_eight_cards_from_current_company_views(conn, settings):
    companies = [row["id"] for row in conn.execute("SELECT id FROM companies ORDER BY id LIMIT 17")]
    assert len(companies) == 17
    first = datetime(2025, 3, 1, tzinfo=UTC)

    def add_view(company, day, stance):
        article = conn.execute(
            """INSERT INTO articles (source_id, external_id, title, published_at, body_md, body_text,
                                     content_hash, ocr_hash)
               VALUES ('fangwei-mp', %s, '经营情况', %s, '当期销量', '当期销量', 'body-1', 'ocr-1')
               RETURNING id""",
            (f"home-views-{day}", first + timedelta(days=day)),
        ).fetchone()["id"]
        conn.execute(
            """INSERT INTO company_views (article_id, company_id, content_hash, ocr_hash,
                                          prompt_version, stance, judgment)
               VALUES (%s, %s, 'body-1', 'ocr-1', %s, %s, '销量仍在变化')""",
            (article, company, PROMPT_VERSION, stance),
        )
        return article

    for index, company in enumerate(companies[:9]):
        add_view(company, index, "positive")
    # 八家只有失效判断。另两家有更新但已失效的看淡判断：一家原本排第九，一家原本排第一。
    for index, company in enumerate([*companies[9:], companies[0], companies[8]]):
        article = add_view(company, 30 + index, "negative")
        content_hash, ocr_hash = (("body-2", "ocr-1"), ("body-1", "ocr-2"), ("body-1", ""))[index % 3]
        conn.execute("UPDATE articles SET content_hash=%s, ocr_hash=%s WHERE id=%s", (content_hash, ocr_hash, article))
    conn.commit()

    with TestClient(create_app(settings)) as client:
        response = client.get("/")
    assert response.status_code == 200
    cards = re.findall(r'<li class="holdings-row recent-item">(.*?)</li>', response.text, re.S)
    assert len(cards) == 8
    for card, company in zip(cards, reversed(companies[1:9]), strict=True):
        assert f'class="holdings-name" href="/companies/{company}"' in card
        assert 'class="stance stance-positive"' in card
