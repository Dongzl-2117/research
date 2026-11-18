"""手记页面和资产的正常入口无需数据库或模型；个人记录没有服务器提交入口。"""

from fastapi.testclient import TestClient

from researchlib.web.app import create_app


def test_journal_page_is_local_and_available_without_database():
    client = TestClient(create_app())
    page = client.get("/journal")
    assert page.status_code == 200
    assert 'href="/journal" aria-current="page"' in page.text
    assert "本机" in page.text and 'id="journal-controls" hidden' in page.text
    for asset in ("journal-store.js", "journal.js", "journal.css"):
        assert f"/static/{asset}?v=" in page.text
        assert client.get(f"/static/{asset}").status_code == 200
    assert client.post("/journal").status_code == 405
