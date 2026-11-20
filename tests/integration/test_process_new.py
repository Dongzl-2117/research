"""research-process-new：抓取导入以后只处理这次新进来的几篇，按识图、译制、判断、摘要、打分的顺序，不等高峰过去。"""

import os

from test_bilingual_provenance_db import article as english_article

from researchlib import digest, fresh, mechanism, views
from researchlib.seed import load_sample
from researchlib.translate import cli
from researchlib.translate.service import list_pending_english


def _report(done=0):
    return {"done": done, "failed": 0, "skipped": 0, "unmetered": 0, "usage": {}, "fatal": "", "paused": "",
            "timed_out": False, "seconds": 0.0}


def _seed(conn, settings):
    os.environ["ALLOW_SAMPLE_SEED"] = "1"
    load_sample(conn, settings)
    conn.commit()


def _ids(conn, source_id):
    return [int(row["id"]) for row in conn.execute(
        "SELECT id FROM articles WHERE source_id = %s ORDER BY id", (source_id,)
    ).fetchall()]


def test_pending_queries_stay_inside_the_named_articles(conn, settings):
    _seed(conn, settings)
    english = english_article(conn, settings)
    conn.commit()
    mp = _ids(conn, "fangwei-mp")
    newest = mp[-2:]
    # 摘要、打分、判断的待办条件照旧，只是限在这几篇里；参数顺序错了会查不出来或报错。
    everything = {int(r["id"]) for r in digest.pending_articles(conn, ["fangwei-mp"], 50)}
    picked = {int(r["id"]) for r in digest.pending_articles(conn, ["fangwei-mp"], 50, article_ids=mp[-3:])}
    assert picked and picked == everything & set(mp[-3:]) and picked != everything
    scored = {int(r["id"]) for r in mechanism.pending_articles(conn, ["fangwei-mp"], None, article_ids=newest)}
    assert scored and scored <= set(newest)
    wanted = [int(row["id"]) for row in views.view_companies(conn, ["fangwei-mp"])]
    everything = {int(r["id"]) for r in views.pending_articles(conn, wanted, ["fangwei-mp"], 50)}
    only = {int(r["id"]) for r in views.pending_articles(conn, wanted, ["fangwei-mp"], 50, article_ids=mp[:1])}
    assert only == everything & set(mp[:1])
    # 译制：带来源和不带来源都按点名的几篇挑。
    assert list_pending_english(conn, limit=10, article_ids=[english]) == [english]
    assert list_pending_english(conn, limit=10, source_id="stratechery", article_ids=[english]) == [english]
    assert list_pending_english(conn, limit=10, source_id="stratechery", article_ids=newest) == []
    assert list_pending_english(conn, limit=10, article_ids=[]) == []


def test_only_articles_after_the_mark_are_processed_in_order(conn, settings, monkeypatch, capsys):
    _seed(conn, settings)
    mp = _ids(conn, "fangwei-mp")
    mark = mp[-4]
    fresh_ids = set(mp[-3:])
    calls: list[tuple[str, set[int]]] = []

    monkeypatch.setattr(fresh.Settings, "from_env", classmethod(lambda cls, *a, **k: settings))
    monkeypatch.setattr("researchlib.llm.ensure_oauth", lambda _settings: None)

    def fake_ocr(argv):
        calls.append(("识图", {int(argv[i + 1]) for i, item in enumerate(argv) if item == "--article"}))
        return 0

    def batch(name):
        def run(_settings, jobs, *args, **kwargs):
            calls.append((name, {int(job["id"]) for job in jobs}))
            return _report(len(jobs))
        return run

    from researchlib import ocr

    monkeypatch.setattr(ocr, "main", fake_ocr)
    monkeypatch.setattr(cli, "main", lambda argv: calls.append(("译制", set())) or 0)
    monkeypatch.setattr(views, "run_batch", batch("判断"))
    monkeypatch.setattr(digest, "run_batch", batch("摘要"))
    monkeypatch.setattr(mechanism, "run_batch", batch("段落打分"))

    assert fresh.main(["--last-id"]) == 0
    assert capsys.readouterr().out.strip() == str(max(_ids(conn, "fangwei-mp") + _ids(conn, "fangwei-zsxq")
                                                      + _ids(conn, "chengfengpolang")))
    assert fresh.main(["--source", "fangwei-mp", "--after-id", str(mark), "--ocr"]) == 0
    names = [name for name, _ in calls]
    # 中文文章不译；识图在判断、摘要前面，打分在最后。
    assert names[0] == "识图" and "译制" not in names
    assert names.index("摘要") < names.index("段落打分")
    for name, ids in calls:
        assert ids <= fresh_ids, name
    assert calls[0][1] == fresh_ids
    assert any(name == "摘要" and ids for name, ids in calls)

    # 后面没有新文章：什么都不做。
    calls.clear()
    assert fresh.main(["--source", "fangwei-mp", "--after-id", str(mp[-1])]) == 0
    assert calls == [] and "没有新文章" in capsys.readouterr().out


def test_translation_cooldown_defers_scoring_until_the_chinese_chunks_are_ready(conn, settings, monkeypatch):
    from test_bilingual_provenance_db import model as translation_model

    from researchlib.translate import service
    from researchlib.translate.provenance import AlignmentError

    english = english_article(conn, settings)
    conn.commit()
    translations = []
    scores = []

    def reject(**kwargs):
        translations.append("reject")
        raise AlignmentError("slot-0-numbers")

    def score(_settings, messages, **kwargs):
        scores.append(messages[-1]["content"])
        return '{"scores":[{"id":1,"score":2}]}', "local-test"

    monkeypatch.setattr(fresh.Settings, "from_env", classmethod(lambda cls: settings))
    monkeypatch.setattr("researchlib.llm.ensure_oauth", lambda _settings: None)
    monkeypatch.setenv("RESEARCH_LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "local-test")
    monkeypatch.setattr(service, "chat_json", reject)
    monkeypatch.setattr(digest, "run_batch", lambda *args, **kwargs: _report())
    monkeypatch.setattr(mechanism, "chat", score)
    args = ["--source", "stratechery", "--after-id", str(english - 1)]

    assert fresh.main(args) == 1
    assert translations
    assert conn.execute("SELECT status FROM translation_attempts WHERE article_id=%s", (english,)).fetchone() == {
        "status": "rejected"
    }
    assert list_pending_english(conn, article_ids=[english]) == []
    assert scores == []

    def translate(**kwargs):
        translations.append("translated")
        return translation_model(**kwargs)

    monkeypatch.setattr(service, "chat_json", translate)
    assert fresh.main(args) == 0
    assert "translated" not in translations
    assert scores == []

    conn.execute(
        "UPDATE translation_attempts SET created_at = now() - interval '13 hours' WHERE article_id=%s", (english,)
    )
    conn.commit()
    assert fresh.main(args) == 0
    assert "translated" in translations
    assert len(scores) == 1 and "第一段10美元" in scores[0]
    rows = conn.execute(
        "SELECT c.lang FROM article_chunks c JOIN chunk_mechanism m USING (article_id, ord) WHERE c.article_id = %s",
        (english,),
    )
    assert {row["lang"] for row in rows} == {"zh"}
    attempts = len(translations)
    assert fresh.main(args) == 0
    assert len(translations) == attempts and len(scores) == 1


def test_untranslated_english_waits_for_its_translation_before_scoring(conn, settings, monkeypatch):
    _seed(conn, settings)
    mark = max(_ids(conn, "fangwei-mp") + _ids(conn, "fangwei-zsxq") + _ids(conn, "chengfengpolang"))
    english = english_article(conn, settings)
    conn.commit()
    asked: list[list[str]] = []
    scored: list[set[int]] = []
    monkeypatch.setattr(fresh.Settings, "from_env", classmethod(lambda cls, *a, **k: settings))
    monkeypatch.setattr("researchlib.llm.ensure_oauth", lambda _settings: None)
    monkeypatch.setattr(cli, "main", lambda argv: asked.append(argv) or 1)

    def score(_settings, jobs, **kwargs):
        scored.append({int(job["id"]) for job in jobs})
        return _report()

    monkeypatch.setattr(mechanism, "run_batch", score)
    monkeypatch.setattr(views, "run_batch", lambda *a, **k: _report())
    monkeypatch.setattr(digest, "run_batch", lambda *a, **k: _report())
    # 译制失败（退出码 1）：记成出错的步骤，后面几步照做；没译完的英文先不打分。
    assert fresh.main(["--source", "stratechery", "--after-id", str(mark)]) == 1
    assert asked and asked[0][-2:] == ["--article-id", str(english)]
    assert "--anytime" not in asked[0] and "--source" not in asked[0]
    assert scored == []
