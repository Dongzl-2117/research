from datetime import datetime, timedelta
from html.parser import HTMLParser

from fastapi.testclient import TestClient

from researchlib.article_history import confirm_commits
from researchlib.textutil import SHANGHAI
from researchlib.web.app import create_app


class HistoryForm(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.cutoff_link = ""
        self.fields = {}
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        fields = dict(attrs)
        if tag == "a" and "history?as_of=" in fields.get("href", ""):
            self.cutoff_link = fields["href"]
        if tag == "input" and "name" in fields and fields["name"] in {"as_of", "original_as_of"}:
            self.fields[fields["name"]] = fields.get("value", "")


def test_archived_read_stays_reproducible_if_an_edit_was_open(conn, settings):
    aid = conn.execute(
        """INSERT INTO articles(source_id,external_id,title,body_md,body_text,content_hash)
           VALUES ('fangwei-mp','open-edit','库存','库存尚未下降','库存尚未下降','first') RETURNING id"""
    ).fetchone()["id"]
    conn.commit()
    confirm_commits(conn)
    conn.commit()
    conn.execute("UPDATE articles SET body_md='库存已经下降' WHERE id=%s", (aid,))
    during = conn.execute("SELECT clock_timestamp() AS t").fetchone()["t"].isoformat()
    with TestClient(create_app(settings)) as client:
        def replay():
            return client.get(f"/api/v1/articles/{aid}", params={"as_of": during}).json()
        observed = replay()
        assert observed["body_markdown"] == "库存尚未下降"
        conn.commit()
        assert replay() == observed


def test_historical_form_preserves_unchanged_instant_and_rejects_future_day(conn, settings):
    aid = conn.execute(
        """INSERT INTO articles(source_id,external_id,title,body_md,body_text,content_hash)
           VALUES ('fangwei-mp','precise-cutoff','现金流','原有预测','原有预测','first') RETURNING id"""
    ).fetchone()["id"]
    conn.commit()
    confirm_commits(conn)
    confirmed = conn.execute(
        """SELECT c.confirmed_at FROM articles a JOIN article_evidence_commits c ON c.xid=a.evidence_xid
           WHERE a.id=%s""", (aid,)
    ).fetchone()["confirmed_at"]
    conn.commit()
    # 固定一个具有微秒的有效时点，无论本机时钟精度如何，表单回送都应表示同一时刻。
    cutoff = confirmed + timedelta(microseconds=1 if confirmed.microsecond % 1000 != 999 else 2)
    with TestClient(create_app(settings)) as client:
        path = f"/articles/{aid}/history"
        page = HistoryForm(client.get(path, params={"as_of": cutoff.isoformat()}).text)
        replay = HistoryForm(client.get(path, params=page.fields).text)
        assert page.cutoff_link and replay.cutoff_link == page.cutoff_link
        changed = {**page.fields, "as_of": "2020-01-01T09:15"}
        response = client.get(path, params=changed)
        assert response.status_code == 200 and "该时点早于本库" in response.text
        tomorrow = datetime.now(SHANGHAI).date() + timedelta(days=1)
        assert client.get(path, params={"through": tomorrow.isoformat()}).status_code == 400
