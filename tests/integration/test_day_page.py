"""日汇总页：没问过这一天的给「问 AI」按钮；从按钮问过以后，那条回答放在最上面。"""

from datetime import datetime

from fastapi.testclient import TestClient

from researchlib import ask as ask_module
from researchlib.ask import PRESETS
from researchlib.ingest.model import ParsedDoc
from researchlib.ingest.store import store_documents
from researchlib.sources import load_sources
from researchlib.textutil import SHANGHAI
from researchlib.web.app import create_app

DAY = "2024-10-19"


def _store(conn, settings, source_id, external_id, title, body):
    source = next(item for item in load_sources(settings.sources_path) if item.id == source_id)
    doc = ParsedDoc(
        external_id=external_id,
        title=title,
        published_at=datetime(2024, 10, 19, 13, 29, tzinfo=SHANGHAI),
        body_md=body,
        raw_meta={"format": "markdown", "filename": f"{external_id}.md"},
        image_refs=[],
    )
    store_documents(
        conn, source=source, docs=[doc], data_dir=settings.data_dir, archive_files=1, images_in_archive=0, errors=[]
    )


def test_day_page_shows_the_answer_asked_from_its_button(conn, settings, monkeypatch):
    _store(conn, settings, "fangwei-mp", "ads", "广告这篇", "腾讯的广告加载率还在提升，视频号是新的广告库存。\n")
    # 飞书文库不进按天，但问库页不分来源问的时候范围里有它。
    _store(conn, settings, "linwenfeng-feishu", "vault", "飞书库备忘录", "只在文库里出现的句子甲乙丙。\n")
    monkeypatch.setattr(ask_module, "chat", lambda _s, _messages, **_k: ("方伟看好腾讯的广告[1]。", "deepseek-test"))
    monkeypatch.setattr(ask_module, "llm_configured", lambda _settings: True)
    monkeypatch.setattr("researchlib.web.routes.schedule_digests", lambda _settings, _jobs: 0)
    with TestClient(create_app(settings)) as client:
        day = client.get(f"/days/{DAY}").text
        assert f'href="/ask?preset=day&amp;from={DAY}&amp;to={DAY}"' in day
        assert "这一天的总结" not in day

        form = client.get("/ask", params={"preset": "day", "from": DAY, "to": DAY}).text
        assert PRESETS["day"] in form
        assert f'name="from" value="{DAY}"' in form
        posted = client.post("/ask", data={"q": PRESETS["day"], "from": DAY, "to": DAY}, follow_redirects=False)
        assert posted.status_code == 303

        day = client.get(f"/days/{DAY}").text
        assert "这一天的主要判断" in day
        assert "方伟看好腾讯的广告" in day
        assert "preset=day" not in day
        # 只看方伟的那一页范围不一样，不拿不分作者问的回答，照旧给按钮。
        only = client.get(f"/days/{DAY}", params={"author": "方伟"}).text
        assert "方伟看好腾讯的广告" not in only
        assert f"preset=day&amp;from={DAY}&amp;to={DAY}&amp;author=" in only

        history = client.get("/ask").text
        assert f'<span class="quiet num">{DAY}</span>' in history
