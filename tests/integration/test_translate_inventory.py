"""中英对照盘点：只读，各类分得对，和待译队列用同一套冷却规则。模型为固定替身。"""

from dataclasses import replace
from datetime import UTC, datetime

from researchlib.ingest.model import ParsedDoc
from researchlib.ingest.store import store_documents
from researchlib.sources import load_sources
from researchlib.translate import inventory, service
from researchlib.translate.llm import ChatJsonResult
from researchlib.translate.provenance import ALIGNMENT_VERSION

BODY = "<p>First paragraph 10 dollars about {name}.</p><p>Second paragraph 20 dollars.</p>"
ZH = "<p>第一段10美元。</p><p>第二段20美元。</p>"


def _article(conn, settings, external_id, *, language="en", body=BODY, title=None):
    # 正文各不相同：一样的正文会被认成同一篇的重复，只留主版本。
    source = next(s for s in load_sources(settings.sources_path) if s.id == "stratechery")
    source = replace(source, min_chars=0)
    store_documents(conn, source=source, docs=[ParsedDoc(
        external_id, title or f"Synthetic {external_id}", datetime(2026, 1, 1, tzinfo=UTC),
        body.format(name=external_id), {"format": "markdown", "language": language}, [],
    )], data_dir=settings.data_dir, archive_files=1, images_in_archive=0, errors=[])
    conn.commit()
    return conn.execute("SELECT id FROM articles WHERE external_id = %s", (external_id,)).fetchone()["id"]


def _model(**kwargs):
    user = kwargs["user"]
    values = []
    for identity, source_hash, html in zip(user["segment_ids"], user["source_hashes"], user["segments"], strict=True):
        translated = html.replace("First paragraph ", "第一段").replace("Second paragraph ", "第二段")
        translated = translated.replace(" dollars", "美元").replace("Synthetic ", "合成")
        values.append({"id": identity, "source_hash": source_hash, "html": translated})
    return ChatJsonResult(data={"t": values}, model="synthetic-model", usage=None, latency_ms=0)


def _old_translation(conn, article_id, *, complete=True):
    conn.execute(
        "INSERT INTO article_translations"
        " (article_id, content_hash, title_zh, body_html, body_text, complete, segments)"
        " SELECT id, content_hash, '合成', %s, '第一段10美元。第二段20美元。', %s, 2 FROM articles WHERE id = %s",
        (ZH, complete, article_id),
    )


def _attempt(conn, article_id, status, reason):
    conn.execute(
        "INSERT INTO translation_attempts (article_id, status, reason, content_hash, alignment_version)"
        " SELECT id, %s, %s, content_hash, %s FROM articles WHERE id = %s",
        (status, reason, ALIGNMENT_VERSION, article_id),
    )


def _counts(conn):
    return conn.execute(
        "SELECT (SELECT count(*) FROM article_translations) AS t, (SELECT count(*) FROM translation_attempts) AS a"
    ).fetchone()


def test_inventory_sorts_every_english_article_and_writes_nothing(conn, settings, monkeypatch):
    done = _article(conn, settings, "done")
    monkeypatch.setattr(service, "chat_json", _model)
    assert service.translate_article(conn, done, data_dir=settings.data_dir).status == "translated"
    legacy = _article(conn, settings, "legacy")
    _old_translation(conn, legacy)
    fresh = _article(conn, settings, "fresh")
    stopped = _article(conn, settings, "stopped")
    _old_translation(conn, stopped, complete=False)
    _attempt(conn, stopped, "rejected", "untranslated:links")
    _attempt(conn, stopped, "rejected", "untranslated:links")
    cooling = _article(conn, settings, "cooling")
    _attempt(conn, cooling, "failed", "request-failed")
    _article(conn, settings, "chinese", language="zh", body="<p>这一篇本来就是中文，不进队列。</p>")
    conn.commit()
    before = dict(_counts(conn))

    report = inventory.collect(conn, settings)

    ids = report["ids"]
    assert report["english"] == 5
    assert ids["receipt"] == [done] and ids["legacy"] == [legacy] and ids["old-receipt"] == []
    assert sorted(ids["none"]) == sorted([fresh, cooling]) and ids["partial"] == [stopped]
    # 和待译队列同一套：拒满两次的停住，刚断线的在冷却，别的现在就能译。
    assert ids["pending-now"] == [fresh] and ids["stopped"] == [stopped] and ids["cooldown"] == [cooling]
    assert report["lastReasons"] == {"untranslated:links": 1, "request-failed": 1}
    assert report["display"]["mapped"] == [done]
    assert report["display"]["legacy-structural"] == [legacy]
    assert report["displayReasons"] == {"legacy-structural": {"missing-provenance": 1}}
    assert report["stateSources"]["none"] == {"stratechery": 2}
    assert report["stateChars"]["legacy"]["stratechery"] == report["displayChars"]["legacy-structural"] > 0
    assert ids["no-text"] == [] and ids["site-hidden"] == []
    assert report["readOnly"] and report["modelCalls"] == 0
    assert dict(_counts(conn)) == before

    lines = inventory.summary(report)
    assert lines[0] == "英文文章 5 篇（stratechery 5）"
    assert "  从没译过 2" in lines
    assert "  这三类里：现在就能进队列 1，冷却中 1，同一版本拒满 2 次停住 1" in lines
    legacy_chars = report["displayChars"]["legacy-structural"]
    assert lines[-1] == f"已译完的 2 篇页面：结构对得上、没核验 1（英文 {legacy_chars:,} 字符），逐段对上 1"


def test_inventory_can_skip_rendering_pages(conn, settings, monkeypatch):
    legacy = _article(conn, settings, "legacy")
    _old_translation(conn, legacy)
    conn.commit()
    monkeypatch.setattr(inventory, "_page", lambda *args: (_ for _ in ()).throw(AssertionError))
    monkeypatch.setattr(inventory, "_has_text", lambda *args: (_ for _ in ()).throw(AssertionError))
    report = inventory.collect(conn, settings, pages=False)
    assert report["ids"]["legacy"] == [legacy] and report["display"] == {}


def test_a_listed_category_is_translated_until_it_has_this_receipt(conn, settings, monkeypatch, capsys):
    """--from-inventory：清单里那一类的篇照译（停住的不看冷却），译完有了这一版回执的再跑就跳过。"""
    import json

    from researchlib.translate import cli

    legacy = _article(conn, settings, "legacy")
    _old_translation(conn, legacy)
    stopped = _article(conn, settings, "stopped")
    _old_translation(conn, stopped, complete=False)
    _attempt(conn, stopped, "rejected", "untranslated:links")
    _attempt(conn, stopped, "rejected", "untranslated:links")
    conn.commit()
    report = inventory.collect(conn, settings)
    path = inventory.inventory_path(settings.data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report), encoding="utf-8")
    assert inventory.listed(settings.data_dir, "unmapped") == ([], report["generatedAt"])

    monkeypatch.setattr(service, "chat_json", _model)
    monkeypatch.setattr(cli.Settings, "from_env", staticmethod(lambda: settings))
    monkeypatch.setattr(cli, "llm_provider", lambda: "deepseek")
    monkeypatch.setattr(cli, "deepseek_api_key", lambda: "sk-test")
    assert cli.main(["--from-inventory", "legacy", "--workers", "2"]) == 0
    assert f"#{legacy} translated" in capsys.readouterr().out
    assert inventory.unfinished(conn, [legacy, stopped], settings.data_dir) == [stopped]
    assert cli.main(["--from-inventory", "legacy"]) == 0
    assert "legacy 这一类都已经译完、有这一版回执、对照页逐段对得上了。" in capsys.readouterr().out
    # 拒满两次停住的，队列不会再拿；点这一类就照译。
    assert cli.main(["--from-inventory", "stopped"]) == 0
    assert f"#{stopped} translated" in capsys.readouterr().out
    assert inventory.unfinished(conn, [legacy, stopped], settings.data_dir) == []


def test_a_stale_receipt_is_resigned_without_asking_the_model(conn, settings, monkeypatch):
    """清洗规则改过、原文指纹变了：页面分开展示，这一版回执也照样再译；没变的段全复用，只重签回执。"""
    article = _article(conn, settings, "resign")
    monkeypatch.setattr(service, "chat_json", _model)
    assert service.translate_article(conn, article, data_dir=settings.data_dir).status == "translated"
    conn.execute(
        "UPDATE article_translations SET provenance = jsonb_set(provenance, '{source_fingerprint}', '\"stale\"')"
        " WHERE article_id = %s",
        (article,),
    )
    conn.commit()
    report = inventory.collect(conn, settings)
    assert report["ids"]["receipt"] == [article] and report["display"] == {"unmapped": [article]}
    assert report["displayReasons"] == {"unmapped": {"source-fingerprint": 1}}
    assert "  分开展示的原因：原文清洗规则改过、回执过期 1" in inventory.summary(report)
    # 待译队列看不出来（原文 hash 没变、complete），盘点清单里的这一类才能补上。
    assert article not in service.list_pending_english(conn, limit=10)
    assert inventory.unfinished(conn, [article], settings.data_dir) == [article]

    monkeypatch.setattr(service, "chat_json", lambda **kwargs: (_ for _ in ()).throw(AssertionError("不该调模型")))
    result = service.translate_article(conn, article, data_dir=settings.data_dir)
    assert result.status == "translated" and result.reused == result.segments
    assert inventory._page(conn, article, settings.data_dir)[0] == "mapped"
    assert inventory.unfinished(conn, [article], settings.data_dir) == []
    # 再跑一遍，回执也一样了，就不再写。
    written = conn.execute("SELECT updated_at FROM article_translations WHERE article_id = %s", (article,)).fetchone()
    assert service.translate_article(conn, article, data_dir=settings.data_dir).status == "translated"
    again = conn.execute("SELECT updated_at FROM article_translations WHERE article_id = %s", (article,)).fetchone()
    assert again == written


def test_posts_the_site_hides_and_image_only_posts_are_counted_apart(conn, settings):
    """网友帖、「图片」评论壳网站不显示，不进队列、单列；只有图的帖子译制会跳过，单列、不交给 --from-inventory。"""
    guest = _article(conn, settings, "guest")
    # 导入时按来源的作者名单标发帖人；这里直接标成网友帖。
    conn.execute(
        "UPDATE articles SET raw_meta = raw_meta || '{\"poster_role\": \"guest\"}'::jsonb WHERE id = %s", (guest,),
    )
    shell = _article(conn, settings, "shell", title="「图片」 评论（3）")
    image = _article(conn, settings, "image", body="![](https://example.com/{name}.png)")
    fresh = _article(conn, settings, "fresh")
    conn.commit()

    pending = service.list_pending_english(conn, limit=10)
    assert guest not in pending and shell not in pending and fresh in pending

    report = inventory.collect(conn, settings)
    ids = report["ids"]
    assert sorted(ids["site-hidden"]) == sorted([guest, shell])
    assert sorted(ids["none"]) == sorted([image, fresh]) and ids["no-text"] == [image]
    lines = inventory.summary(report)
    assert "  网站不显示的网友帖、「图片」评论壳 2 篇（stratechery 2），不算进下面各类，也不译" in lines
    assert "  这三类里只有图、没有要译的字的 1 篇：译制直接跳过，不调模型" in lines

    import json

    import pytest

    path = inventory.inventory_path(settings.data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report), encoding="utf-8")
    assert inventory.listed(settings.data_dir, "none")[0] == [fresh]
    with pytest.raises(LookupError, match="网站不显示"):
        inventory.listed(settings.data_dir, "site-hidden")


def test_the_omission_sample_reads_legacy_pages_from_the_inventory(conn, settings, monkeypatch, capsys):
    """research-bilingual-qa --omissions --from-inventory legacy-structural：没回执的旧稿按页面上并排的段对核。"""
    import json

    from researchlib.translate import cli_qa, omission

    legacy = _article(conn, settings, "legacy")
    _old_translation(conn, legacy)
    conn.commit()
    report = inventory.collect(conn, settings)
    path = inventory.inventory_path(settings.data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report), encoding="utf-8")
    asked = []

    def reviewer(**kwargs):
        asked.extend((pair["en"], pair["zh"]) for pair in kwargs["user"]["pairs"])
        return ChatJsonResult(data={"t": []}, model="synthetic-model", usage=None, latency_ms=0)

    monkeypatch.setattr(omission, "chat_json", reviewer)
    monkeypatch.setattr(cli_qa, "ensure_llm", lambda settings: None)
    monkeypatch.setattr(cli_qa.Settings, "from_env", staticmethod(lambda: settings))
    before = dict(_counts(conn))
    assert cli_qa.main(["--omissions", "--from-inventory", "legacy-structural", "--limit", "50"]) == 0
    out = capsys.readouterr().out
    assert asked == [
        ("First paragraph 10 dollars about legacy.", "第一段10美元。"),
        ("Second paragraph 20 dollars.", "第二段20美元。"),
    ]
    assert f"#{legacy} pairs=2 flagged=0 按页面配对" in out
    assert dict(_counts(conn)) == before
