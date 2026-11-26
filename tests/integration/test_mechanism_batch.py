"""research-mechanism：只打该打的块、存分数、块改了重打、英文原文有译文的不打。"""

import json
import os
from unittest.mock import Mock

from researchlib import mechanism
from researchlib.seed import load_sample
from researchlib.sources import load_sources


def _setup(conn, settings):
    os.environ["ALLOW_SAMPLE_SEED"] = "1"
    load_sample(conn, settings)
    conn.commit()
    return [source.id for source in load_sources(settings.sources_path) if source.enabled]


def _reply(_settings, messages, *, usage=None, **_kwargs):
    count = messages[1]["content"].count("【")
    if usage is not None:
        usage["prompt"] = usage.get("prompt", 0) + 1000
    return json.dumps({"scores": [{"id": i, "score": 2} for i in range(1, count + 1)]}), "deepseek-test"


def _labeled_chunk(conn, article_id):
    chunk = mechanism.stale_chunks(conn, article_id)[0]
    return {"article_id": article_id, "ord": chunk["ord"], "chunk_hash": chunk["chunk_hash"], "label": 2}


def test_failed_calibration_keeps_article_in_scoring_queue(conn, settings, monkeypatch, tmp_path, capsys):
    sources = _setup(conn, settings)
    article = mechanism.pending_articles(conn, sources, 1)[0]
    article_id = article["id"]
    sample = tmp_path / "calibration.json"
    sample.write_text(json.dumps([_labeled_chunk(conn, article_id)]))
    conn.commit()
    missing_score = Mock(return_value=('{"scores":[]}', "test-model"))
    monkeypatch.setattr(mechanism, "chat", missing_score)
    assert mechanism.calibrate(settings, str(sample), workers=1, off_peak=False) == 0
    assert missing_score.call_count == 2
    output = capsys.readouterr()
    assert "模型打了 0 块" in output.out
    assert "打分缺了第 1 段" in output.err
    assert article_id in {row["id"] for row in mechanism.pending_articles(conn, sources, None)}
    assert not conn.execute(
        "SELECT 1 FROM llm_failures WHERE kind = 'mechanism' AND item_id = %s", (article_id,)
    ).fetchone()

    # 正式打分也连续漏段时，仍应计入冷却。
    report = mechanism.run_batch(settings, [article])
    assert report["failed"] == 1
    assert mechanism.llm_batch.exit_code(report) == 0
    assert conn.execute(
        "SELECT failures FROM llm_failures WHERE kind = 'mechanism' AND item_id = %s", (article_id,)
    ).fetchone()["failures"] == 1
    assert article_id not in {row["id"] for row in mechanism.pending_articles(conn, sources, None)}


def test_calibration_deadline_finishes_current_article_and_stops(conn, settings, monkeypatch, tmp_path, capsys):
    sources = _setup(conn, settings)
    articles = mechanism.pending_articles(conn, sources, 3)
    sample = tmp_path / "calibration.json"
    sample.write_text(json.dumps([_labeled_chunk(conn, row["id"]) for row in articles]))
    clock = 100.0

    def slow_reply(*args, **kwargs):
        nonlocal clock
        clock += 120
        return _reply(*args, **kwargs)

    reply = Mock(side_effect=slow_reply)
    monkeypatch.setattr(mechanism.time, "monotonic", lambda: clock)
    monkeypatch.setattr(mechanism.Settings, "from_env", lambda: settings)
    monkeypatch.setattr(mechanism, "ensure_oauth", lambda _settings: None)
    monkeypatch.setattr(mechanism, "chat", reply)
    assert mechanism.main(["--calibrate", str(sample), "--max-minutes", "1"]) == 0
    assert reply.call_count == 1
    output = capsys.readouterr()
    assert "模型打了 1 块" in output.out
    assert "到了时间上限" in output.err


def test_scores_pending_chunks_once_and_rescore_when_chunk_changes(conn, settings, monkeypatch):
    sources = _setup(conn, settings)
    rows = mechanism.pending_articles(conn, sources, 5)
    conn.commit()
    assert rows
    jobs = [{"id": int(row["id"]), "title": row["title"]} for row in rows]
    monkeypatch.setattr(mechanism, "chat", _reply)
    report = mechanism.run_batch(settings, jobs, workers=2)
    assert report["done"] == len(jobs) and report["failed"] == 0
    done = {job["id"] for job in jobs}
    assert not done & {int(row["id"]) for row in mechanism.pending_articles(conn, sources, None)}
    stored = conn.execute(
        "SELECT count(*) AS n FROM chunk_mechanism WHERE article_id = ANY(%s) AND score = 2", (list(done),)
    ).fetchone()["n"]
    assert stored == sum(int(row["chunks"]) for row in rows)
    # 再跑一遍不问模型。
    again = mechanism.run_batch(settings, jobs)
    assert again["done"] == 0 and again["skipped"] == len(jobs)
    # 改一块的正文：只有这一篇回到待打分，且只有这一块要重打。
    target = jobs[0]["id"]
    ord_ = conn.execute("SELECT min(ord) AS o FROM article_chunks WHERE article_id = %s", (target,)).fetchone()["o"]
    conn.execute("UPDATE article_chunks SET body = body || '补一句' WHERE article_id = %s AND ord = %s", (target, ord_))
    conn.commit()
    assert [c["ord"] for c in mechanism.stale_chunks(conn, target)] == [ord_]


def test_english_original_chunks_skipped_when_translation_exists(conn, settings):
    _setup(conn, settings)
    row = conn.execute("SELECT article_id, max(ord) AS o FROM article_chunks GROUP BY article_id LIMIT 1").fetchone()
    article_id = row["article_id"]
    conn.execute(
        "INSERT INTO article_chunks (article_id, ord, heading, body, lang) VALUES (%s, %s, '', '译文', 'zh')",
        (article_id, row["o"] + 1000),
    )
    conn.commit()
    langs = {
        c["ord"]: c for c in mechanism.stale_chunks(conn, article_id)
    }
    assert list(langs) == [row["o"] + 1000]


def test_retrieval_reads_fresh_scores_only(conn, settings):
    from researchlib import retrieval

    _setup(conn, settings)
    rows = conn.execute(
        "SELECT article_id, ord, heading, body FROM article_chunks WHERE lang <> 'zh' ORDER BY article_id, ord LIMIT 2"
    ).fetchall()
    first, second = rows
    for row, score in ((first, 2), (second, 0)):
        conn.execute(
            "INSERT INTO chunk_mechanism (article_id, ord, chunk_hash, score, prompt_version)"
            " SELECT c.article_id, c.ord, " + mechanism.chunk_hash_sql("c") + ", %s, %s"
            " FROM article_chunks c WHERE c.article_id = %s AND c.ord = %s",
            (score, mechanism.PROMPT_VERSION, row["article_id"], row["ord"]),
        )
    conn.commit()
    found = retrieval.mechanism_scores(conn, [dict(r) for r in rows])
    assert found == {(first["article_id"], first["ord"]): 2, (second["article_id"], second["ord"]): 0}
    # 正文改过（指纹对不上）的当没打过。
    changed = dict(second, body=(second["body"] or "") + "补一句")
    assert retrieval.mechanism_scores(conn, [dict(first), changed]) == {(first["article_id"], first["ord"]): 2}


def test_failed_article_keeps_scored_batches_and_is_reported_while_cooling(conn, settings, monkeypatch):
    sources = _setup(conn, settings)
    rows = mechanism.pending_articles(conn, sources, None)
    conn.commit()
    row = max(rows, key=lambda item: int(item["chunks"]))
    assert int(row["chunks"]) >= 2
    calls = {"n": 0}

    def flaky(_settings, messages, *, usage=None, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return _reply(_settings, messages, usage=usage)
        return json.dumps({"scores": []}), "deepseek-test"  # 第二批起一段都不给

    monkeypatch.setattr(mechanism, "chat", flaky)
    monkeypatch.setattr(mechanism, "BATCH_CHARS", 1)  # 一块一批
    report = mechanism.run_batch(settings, [{"id": int(row["id"]), "title": row["title"]}])
    assert report["failed"] == 1
    kept = conn.execute("SELECT count(*) AS n FROM chunk_mechanism WHERE article_id = %s", (row["id"],)).fetchone()["n"]
    assert kept == 1
    assert int(row["id"]) not in {int(r["id"]) for r in mechanism.pending_articles(conn, sources, None)}
    cooled = mechanism.pending_articles(conn, sources, None, cooling=True)
    assert [int(r["id"]) for r in cooled] == [int(row["id"])]
    assert int(cooled[0]["chunks"]) == int(row["chunks"]) - 1


def test_nightly_failure_keeps_cooling_work_in_cost_estimate(conn, settings, monkeypatch, capsys):
    article_id = conn.execute(
        "INSERT INTO articles (source_id, external_id, content_hash, title, body_md, body_text)"
        " VALUES ('fangwei-mp', 'test', 'test', '长文', '正文', '正文') RETURNING id"
    ).fetchone()["id"]
    for ord_, body in enumerate(["已打完", "文" * (mechanism.CHUNK_LIMIT + 100), "两个"]):
        conn.execute(
            "INSERT INTO article_chunks (article_id, ord, heading, body) VALUES (%s, %s, '', %s)",
            (article_id, ord_, body),
        )
    mechanism.save_scores(conn, article_id, mechanism.stale_chunks(conn, article_id)[:1], [2], "test")
    conn.commit()
    monkeypatch.setattr(mechanism.Settings, "from_env", lambda: settings)
    monkeypatch.setattr(mechanism, "ensure_oauth", lambda _settings: None)
    monkeypatch.setattr(mechanism, "chat", lambda *args, **kwargs: ('{"scores":[]}', "test"))
    args = ["--source", "fangwei-mp"]
    inventory = [*args, "--limit", "0"]
    total = f"合计待打 1 篇、2 块，送模型的正文约 {mechanism.CHUNK_LIMIT + 2:,} 字"
    assert mechanism.main(inventory) == 0
    assert total in capsys.readouterr().out

    assert mechanism.main([*args, "--limit", "1", "--fail-empty"]) == 4
    assert "夜间统计 写入 0 篇，失败 1 篇，跳过 0 篇" in capsys.readouterr().err
    assert mechanism.main(inventory) == 0
    report = capsys.readouterr().out
    assert "还要打分 0 篇、0 块" in report
    assert "另有 1 篇、2 块失败后在冷却里" in report
    assert total in report
    assert f"当前可打 0 字，冷却中 {mechanism.CHUNK_LIMIT + 2:,} 字" in report

    # 没有能开工的文章，与全部尝试失败是两种不同的夜间结果。
    assert mechanism.main([*args, "--limit", "1", "--fail-empty"]) == 0
    assert "夜间统计 写入 0 篇，失败 0 篇，跳过 0 篇" in capsys.readouterr().err
