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


def test_receipts_from_an_older_prompt_are_listed_apart_and_rewritten(conn, settings, monkeypatch, capsys):
    """旧提示词译的有回执的篇：页面照常逐段对上，盘点单列 old-prompt；按这一类整篇重译，译过的再跑就跳过。

    用现在这一版译过、个别段重译失败退回旧译文的（prompt_versions 不止一版）不算，再跑也不重译。
    """
    import json

    from researchlib.translate import cli
    from researchlib.translate.provenance import PROMPT_VERSION

    older = _article(conn, settings, "older")
    mixed = _article(conn, settings, "mixed")
    newer = _article(conn, settings, "newer")
    calls = []

    def model(**kwargs):
        calls.append(kwargs["user"]["segment_ids"])
        return _model(**kwargs)

    monkeypatch.setattr(service, "chat_json", model)
    for article in (older, mixed, newer):
        assert service.translate_article(conn, article, data_dir=settings.data_dir).status == "translated"
    for article, versions in ((older, [PROMPT_VERSION - 1]), (mixed, [PROMPT_VERSION - 1, PROMPT_VERSION])):
        conn.execute(
            "UPDATE article_translations SET provenance = jsonb_set(provenance, '{prompt_versions}', %s::jsonb)"
            " WHERE article_id = %s",
            (json.dumps(versions), article),
        )
    conn.commit()

    report = inventory.collect(conn, settings)
    ids = report["ids"]
    assert ids["receipt"] == [newer, mixed, older] and ids["old-prompt"] == [older]
    assert sorted(report["display"]["mapped"]) == sorted([older, mixed, newer])
    assert report["mixedPrompts"] == 1
    old_chars = report["stateChars"]["old-prompt"]["stratechery"]
    lines = inventory.summary(report)
    assert (
        f"  其中第 {PROMPT_VERSION} 版提示词以前译的：有回执的 1（old-prompt，英文 {old_chars:,} 字符），"
        "没回执的旧稿 0（legacy，英文 0 字符）；要换成现在的写法，research-translate --from-inventory 这两类"
    ) in lines
    assert f"  用第 {PROMPT_VERSION} 版译过、个别段重译失败退回旧译文的 1 篇，不算 old-prompt" in lines
    # 别的类照旧把它算译完；点 old-prompt 才算没译完。
    assert inventory.unfinished(conn, [newer, mixed, older], settings.data_dir) == []
    assert inventory.unfinished(conn, [newer, mixed, older], settings.data_dir, prompt=True) == [older]

    path = inventory.inventory_path(settings.data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report), encoding="utf-8")
    monkeypatch.setattr(cli.Settings, "from_env", staticmethod(lambda: settings))
    monkeypatch.setattr(cli, "llm_provider", lambda: "deepseek")
    monkeypatch.setattr(cli, "deepseek_api_key", lambda: "sk-test")
    calls.clear()
    assert cli.main(["--from-inventory", "old-prompt"]) == 0
    out = capsys.readouterr().out
    assert f"里 old-prompt 共 1 篇，已经用第 {PROMPT_VERSION} 版提示词译过的跳过" in out
    assert f"#{older} translated" in out
    # 旧提示词的段不复用，整篇送模型重译；回执记成这一版。
    assert calls and sum(len(batch) for batch in calls) >= 2
    receipt = conn.execute(
        "SELECT provenance->'prompt_versions' AS versions FROM article_translations WHERE article_id = %s", (older,),
    ).fetchone()
    assert receipt["versions"] == [PROMPT_VERSION]
    assert inventory.unfinished(conn, [older], settings.data_dir, prompt=True) == []
    calls.clear()
    assert cli.main(["--from-inventory", "old-prompt"]) == 0
    assert f"old-prompt 这一类都已经用第 {PROMPT_VERSION} 版提示词译过了。" in capsys.readouterr().out
    assert calls == []


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


ECHO_BODY = (
    "The company rebuilt its app first because loyalty starts with the booking flow every {name} customer sees.\n\n"
    "It then fixed the operating data so that every delay could be traced back to a single cause in the plan.\n\n"
    "Only after that did it change how the managers talked about blame in their weekly meetings with the crews.\n\n"
    "The result was a culture that cared about fixing problems rather than finding someone to take the fall."
)
ECHO_ZH = {
    "The company rebuilt": "这家公司先重做了 App，因为忠诚度从每位顾客都会看到的订票流程开始。",
    "It then fixed": "接着它理顺了运营数据，每次延误都能追到计划里的一个原因。",
    "Only after that": "在那之后，它才改了经理们每周和机组开会时谈责任的方式。",
    "The result was": "结果形成了一种重在解决问题、不找人背锅的文化。",
}


COPY_BODY = (
    "When a great team meets a lousy market, the market wins and the team goes home for {name} good.\n\n"
    "When a lousy team meets a great market, the market still wins and the team gets carried along.\n\n"
    "When a great team meets a great market, something special happens and everyone can see it.\n\n"
    "You can obviously screw up a great market, but a good market will usually lift a decent team."
)
COPY_ZH = {
    "When a great team meets a lousy": "当优秀的团队遇上糟糕的市场，市场赢，团队只能回家。",
    "When a lousy team": "当平庸的团队遇上巨大的市场，还是市场赢，团队被市场带着走。",
    "When a great team meets a great": "当优秀的团队遇上巨大的市场，就会有特别的事情发生，谁都看得见。",
    "You can obviously": "巨大的市场当然也可能被搞砸，但好市场通常会把一支还行的团队托起来。",
}
ORDER = list(COPY_ZH)


def _key(html):
    return next(key for key in (*ECHO_ZH, *COPY_ZH) if key in html)


def _misplacing_model(*, misplace):
    """``misplace`` 为真时两种串段都出：

    - 交回英文：第二段的中文写进第一段，第二段原样交回英文。
    - 错开一格：第一段的中文没了，后面每段拿下一段的中文，最后一段和前一段写成一样。
    """

    def model(**kwargs):
        user = kwargs["user"]
        values = []
        for identity, source_hash, html in zip(user["segment_ids"], user["source_hashes"], user["segments"],
                                               strict=True):
            if user["segment_ids"] == ["title"]:
                values.append({"id": identity, "source_hash": source_hash, "html": "合成标题"})
                continue
            key = _key(html)
            zh = ECHO_ZH.get(key) or COPY_ZH[key]
            text = f"<p>{zh}</p>"
            if misplace and key == "The company rebuilt":
                text = f"<p>{zh}{ECHO_ZH['It then fixed']}</p>"
            elif misplace and key == "It then fixed":
                text = html
            elif misplace and key in COPY_ZH:
                text = f"<p>{COPY_ZH[ORDER[min(ORDER.index(key) + 1, len(ORDER) - 1)]]}</p>"
            values.append({"id": identity, "source_hash": source_hash, "html": text})
        return ChatJsonResult(data={"t": values}, model="synthetic-model", usage=None, latency_ms=0)

    return model


def test_misplaced_segments_are_listed_and_only_their_stretch_is_retranslated(
    conn, settings, monkeypatch, capsys,
):
    """已译完、页面也对得上，只是有串段：盘点单列 misplaced；按这一类再译只补串段附近的段。

    一篇有段交回英文（中文写进了前一段），一篇译文整体错开一格（最后两段中文一样）。
    """
    import json

    from researchlib.translate import cli

    echo = _article(conn, settings, "echo", body=ECHO_BODY)
    shifted = _article(conn, settings, "shifted", body=COPY_BODY)
    # 以前的核对不拦这两种：这样存下来的译文页面照样逐段对得上。只撤这几处替换：monkeypatch.undo()
    # 会连 conftest 里「不在高峰价时段」那处一起撤掉，北京时间白天跑，后面按清单译就一直等高峰过去。
    with monkeypatch.context() as early:
        early.setattr(service, "untranslated_english", lambda *args: False)
        early.setattr(service, "copied_targets", lambda *args, **kwargs: [])
        early.setattr(service, "chat_json", _misplacing_model(misplace=True))
        for article in (echo, shifted):
            assert service.translate_article(conn, article, data_dir=settings.data_dir).status == "translated"
    conn.commit()

    report = inventory.collect(conn, settings)
    assert sorted(report["display"]["mapped"]) == sorted([echo, shifted])
    assert sorted(report["ids"]["misplaced"]) == sorted([echo, shifted])
    assert report["echoSegments"] == 1 and report["copiedSegments"] == 2
    assert (
        "  串段（一段的中文写进了别的段）2 篇：交回英文的段 1 段，相近两段英文不同、中文一字不差的 2 段。"
        "research-translate --from-inventory misplaced 只重译这几处附近的段"
        in inventory.summary(report)
    )
    # 别的类照旧算它们译完了；点 misplaced 才算没译完。
    assert inventory.unfinished(conn, [echo, shifted], settings.data_dir) == []
    assert inventory.unfinished(conn, [echo, shifted], settings.data_dir, misplaced=True) == [echo, shifted]
    path = inventory.inventory_path(settings.data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report), encoding="utf-8")

    asked: list[list[str]] = []
    honest = _misplacing_model(misplace=False)

    def model(**kwargs):
        if kwargs["user"]["segment_ids"] != ["title"]:
            asked.append([_key(html) for html in kwargs["user"]["segments"]])
        return honest(**kwargs)

    monkeypatch.setattr(service, "chat_json", model)
    monkeypatch.setattr(cli.Settings, "from_env", staticmethod(lambda: settings))
    monkeypatch.setattr(cli, "llm_provider", lambda: "deepseek")
    monkeypatch.setattr(cli, "deepseek_api_key", lambda: "sk-test")
    assert cli.main(["--from-inventory", "misplaced"]) == 0
    out = capsys.readouterr().out
    assert f"#{echo} translated" in out and f"#{shifted} translated" in out
    # 交回英文的：只问那一段和前后两段，第四段照旧复用。错开一格的：从哪段开始错看不出来，四段都在范围里。
    assert sorted(asked) == sorted([
        ["The company rebuilt", "It then fixed", "Only after that"],
        ORDER,
    ])
    stored = {
        row["article_id"]: row["body_html"] for row in conn.execute(
            "SELECT article_id, body_html FROM article_translations WHERE article_id = ANY(%s)",
            ([echo, shifted],),
        ).fetchall()
    }
    assert "It then fixed" not in stored[echo] and stored[echo].count("接着它理顺了运营数据") == 1
    assert all(stored[shifted].count(zh) == 1 for zh in COPY_ZH.values())
    assert inventory.unfinished(conn, [echo, shifted], settings.data_dir, misplaced=True) == []
    assert cli.main(["--from-inventory", "misplaced"]) == 0
    assert "misplaced 这一类都已经没有串段了。" in capsys.readouterr().out
    after = inventory.collect(conn, settings)
    assert after["ids"]["misplaced"] == [] and after["echoSegments"] == 0 and after["copiedSegments"] == 0
    assert "  串段（一段的中文写进了别的段）0 篇：交回英文的段 0 段，相近两段英文不同、中文一字不差的 0 段" in (
        inventory.summary(after)
    )
