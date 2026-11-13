"""11-14 以前的观点读法装满 6 块就不再读：research-views --truncated 找出真丢了段落的旧判断，按现在的读法重抽。"""

import json
from datetime import datetime
from zoneinfo import ZoneInfo

from researchlib import views as views_mod
from researchlib.ingest.model import ParsedDoc
from researchlib.ingest.store import store_documents
from researchlib.llm import LLMError
from researchlib.sources import load_sources

SHANGHAI = ZoneInfo("Asia/Shanghai")


def _store(conn, settings, external_id: str, body: str) -> int:
    sources = {source.id: source for source in load_sources(settings.sources_path)}
    doc = ParsedDoc(
        external_id=external_id,
        title=f"腾讯长文 {external_id}",
        published_at=datetime(2026, 9, 1, tzinfo=SHANGHAI),
        body_md=body,
        raw_meta={},
        image_refs=[],
    )
    store_documents(conn, source=sources["fangwei-mp"], docs=[doc], data_dir=settings.data_dir,
                    archive_files=1, images_in_archive=0, errors=[])
    return int(conn.execute("SELECT id FROM articles WHERE external_id = %s", (external_id,)).fetchone()["id"])


def _long_body() -> str:
    """通篇都在谈腾讯、十万字上下，结论在最后一节。"""
    sections = [f"## 第 {index} 节\n\n" + f"第 {index} 节：腾讯的广告加载率还有空间，" * 50 for index in range(1, 110)]
    return "\n\n".join(sections) + "\n\n## 结论\n\n结论：腾讯的广告还有空间，这是全文最后的判断。"


def _view(_settings, messages, **_kwargs):
    return json.dumps({"腾讯": {"about": True, "judgment": "广告还有空间", "stance": "正面"}}, ensure_ascii=False), "x"


def test_truncated_old_views_are_found_and_reread_once(conn, settings, monkeypatch, capsys):
    long_id = _store(conn, settings, "mp/long-truncated", _long_body())
    short_id = _store(conn, settings, "mp/short", "腾讯的广告加载率还有空间，腾讯的视频号刚开始商业化。" * 20)
    tencent = conn.execute("SELECT id, name_zh FROM companies WHERE name_zh = '腾讯'").fetchone()
    monkeypatch.setattr(views_mod, "chat", _view)
    for article_id in (long_id, short_id):
        row = conn.execute(views_mod._ARTICLE_SQL, (article_id,)).fetchone()
        views_mod.extract_article(conn, settings, row, "方伟", [tencent])
    # 新读法写的行不算；把它们改回旧读法写的样子。
    assert views_mod.truncated_articles(conn, {"fangwei-mp": "方伟"}) == []
    conn.execute("UPDATE company_views SET reader = 1 WHERE article_id = ANY(%s)", ([long_id, short_id],))
    conn.commit()

    found = views_mod.truncated_articles(conn, {"fangwei-mp": "方伟"})
    # 短文旧读法也读全了，不重抽。
    assert [item["id"] for item in found] == [long_id]
    assert found[0]["companies"] == [tencent["id"]] and found[0]["chars"] > 100000 and found[0]["calls"] >= 6

    monkeypatch.setattr(views_mod.Settings, "from_env", staticmethod(lambda: settings))
    monkeypatch.setattr(views_mod, "ensure_oauth", lambda _settings: None)

    def never(*_args, **_kwargs):
        raise AssertionError("--limit 0 不该问模型")

    monkeypatch.setattr(views_mod, "chat", never)
    assert views_mod.main(["--truncated", "--limit", "0", "--source", "fangwei-mp"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("旧读法丢了段落的 1 篇") and f"{long_id} 腾讯长文" in out

    prompts: list[str] = []

    def capture(settings_, messages, **kwargs):
        prompts.append(messages[1]["content"])
        return _view(settings_, messages, **kwargs)

    monkeypatch.setattr(views_mod, "chat", capture)
    assert views_mod.main(["--truncated", "--source", "fangwei-mp"]) == 0
    assert any("全文最后的判断" in prompt for prompt in prompts) and len(prompts) == found[0]["calls"]
    reader = conn.execute("SELECT reader FROM company_views WHERE article_id = %s", (long_id,)).fetchone()["reader"]
    assert reader == views_mod.READER
    # 重抽过的不再算进来，再跑一遍不花钱。
    assert views_mod.truncated_articles(conn, {"fangwei-mp": "方伟"}) == []


def test_truncated_does_not_mix_with_other_selections():
    import pytest

    for extra in (["--gap"], ["--articles", "1"], ["--ocr-changed"], ["--company", "腾讯"], ["--refresh-blank"]):
        with pytest.raises(SystemExit) as stopped:
            views_mod.main(["--truncated", *extra])
        assert "--truncated" in str(stopped.value)


def test_failed_long_article_is_not_lost(conn, settings, monkeypatch):
    """重抽到一半接口报错：旧行留着、仍是旧读法，下次 --truncated 还会找到它。"""
    long_id = _store(conn, settings, "mp/long-retry", _long_body())
    tencent = conn.execute("SELECT id, name_zh FROM companies WHERE name_zh = '腾讯'").fetchone()
    monkeypatch.setattr(views_mod, "chat", _view)
    row = conn.execute(views_mod._ARTICLE_SQL, (long_id,)).fetchone()
    views_mod.extract_article(conn, settings, row, "方伟", [tencent])
    conn.execute("UPDATE company_views SET reader = 1 WHERE article_id = %s", (long_id,))
    conn.commit()
    calls = {"n": 0}

    def flaky(settings_, messages, **kwargs):
        calls["n"] += 1
        if calls["n"] == 3:
            raise LLMError("模型没有按格式回答")
        return _view(settings_, messages, **kwargs)

    monkeypatch.setattr(views_mod, "chat", flaky)
    jobs = views_mod.truncated_articles(conn, {"fangwei-mp": "方伟"})
    report = views_mod.run_batch(settings, jobs, {"fangwei-mp": "方伟"}, named=True)
    assert report["failed"] == 1
    assert [item["id"] for item in views_mod.truncated_articles(conn, {"fangwei-mp": "方伟"})] == [long_id]
