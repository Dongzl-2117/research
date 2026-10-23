from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

from researchlib.digest import ensure_digests
from researchlib.ingest.model import ParsedDoc
from researchlib.ingest.store import store_documents
from researchlib.sources import load_sources
from researchlib.textutil import SHANGHAI
from researchlib.web.app import create_app

PROOF = Path("/tmp/research-feed-proof")


def _source(settings, source_id):
    return next(source for source in load_sources(settings.sources_path) if source.id == source_id)


def _doc(external_id, title, when, body):
    return ParsedDoc(
        external_id=external_id,
        title=title,
        published_at=when,
        body_md=body,
        raw_meta={"format": "markdown", "filename": f"{external_id}.md"},
        image_refs=[],
    )


def test_timeline_feed_hides_feishu_and_cards_use_digest(conn, settings, monkeypatch):
    fangwei = _source(settings, "fangwei-mp")
    feishu = _source(settings, "linwenfeng-feishu")
    when = datetime(2024, 10, 19, 13, 29, tzinfo=SHANGHAI)
    store_documents(
        conn,
        source=fangwei,
        docs=[
            _doc(
                "card-meta",
                "加载率这篇",
                when,
                "\n".join(
                    [
                        "sn: 8821",
                        "likes: 14",
                        "comments: 2",
                        "2024-10-19 13:29",
                        "",
                        "这篇把广告加载率下降写成了核心判断。后面是展开。",
                        "",
                    ]
                ),
            )
        ],
        data_dir=settings.data_dir,
        archive_files=1,
        images_in_archive=0,
        errors=[],
    )
    store_documents(
        conn,
        source=feishu,
        docs=[_doc("vault-note", "飞书库备忘录", when, "只在文库里出现的句子甲乙丙。\n")],
        data_dir=settings.data_dir,
        archive_files=1,
        images_in_archive=0,
        errors=[],
    )
    seen: list[list[tuple[int, str]]] = []

    def fake_schedule(_settings, jobs):
        seen.append(list(jobs))
        return 0

    monkeypatch.setattr("researchlib.digest.schedule_digests", fake_schedule)
    app = create_app(settings)
    with TestClient(app) as client:
        home = client.get("/")
        assert home.status_code == 200
        assert "加载率这篇" in home.text
        assert "这篇把广告加载率下降写成了核心判断" in home.text
        assert "likes:" not in home.text
        assert "sn:" not in home.text
        assert "飞书库备忘录" not in home.text
        # 文库有入口，但不进按天的来源筛选，也不能用 ?source= 打开时间线。
        assert 'href="/sources/linwenfeng-feishu/tree"' in home.text
        assert 'href="/?source=linwenfeng-feishu"' not in home.text
        assert "导读写的是加载率，不是元数据。" not in home.text
        assert "AI 整合" not in home.text
        assert client.get("/", params={"source": "linwenfeng-feishu"}).status_code == 404

        day = client.get("/days/2024-10-19")
        assert day.status_code == 200
        assert "飞书库备忘录" not in day.text
        assert "广告加载率下降写成了核心判断" in day.text
        assert "likes:" not in day.text
        assert "日汇总" in day.text

        author = client.get("/authors/林文丰")
        assert "飞书库备忘录" in author.text
        assert 'href="/days/' not in author.text
        found = client.get("/search", params={"q": "甲乙丙"})
        assert "飞书库备忘录" in found.text
        assert client.get("/days/2024-10-19", params={"author": "林文丰"}).status_code == 404

        article_id = conn.execute("SELECT id FROM articles WHERE external_id = 'card-meta'").fetchone()["id"]
        assert seen == []  # 读取页面不隐式生成摘要。

        def fake_chat(_settings, messages, **_kwargs):
            assert "不要给出投资建议" in messages[0]["content"]
            return (
                '{"summary":"导读写的是加载率，不是元数据。","points":["加载率在下降","原文没有买卖建议"],"tags":["广告"]}',
                "grok-test",
            )

        monkeypatch.setattr("researchlib.digest.chat", fake_chat)
        written = ensure_digests(conn, settings, [article_id], {article_id: "方伟"})
        assert written == 1

        again = client.get("/")
        assert "导读写的是加载率，不是元数据。" in again.text
        assert "原文没有买卖建议" in again.text
        assert "likes:" not in again.text
        assert "sn:" not in again.text
        assert 'class="ai-badge"' in again.text
        assert "AI 整合" not in again.text

        reader = client.get(f"/articles/{article_id}")
        assert reader.status_code == 200
        assert "加载率这篇" in reader.text
        assert "广告加载率下降写成了核心判断" in reader.text
        # 点进文章先看总结：摘要在正文前面。
        assert "导读写的是加载率，不是元数据。" in reader.text
        assert reader.text.index("导读写的是加载率") < reader.text.index("广告加载率下降写成了核心判断")
        assert "打开原文" not in reader.text
        assert "<h2>来源</h2>" not in reader.text
        assert "方伟微信公众号历史归档" not in reader.text
        assert "lang-tabs" not in reader.text
        assert "正文" in reader.text

    PROOF.mkdir(parents=True, exist_ok=True)
    (PROOF / "card-before.html").write_text(home.text, encoding="utf-8")
    (PROOF / "card-after.html").write_text(again.text, encoding="utf-8")
    (PROOF / "article.html").write_text(reader.text, encoding="utf-8")
