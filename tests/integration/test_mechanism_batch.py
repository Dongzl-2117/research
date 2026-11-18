"""research-mechanism：只打该打的块、存分数、块改了重打、英文原文有译文的不打。"""

import json
import os

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


def test_limit_zero_counts_unscored_chunks_including_cooling(conn, settings, monkeypatch, capsys):
    from researchlib.failures import note_failure

    def article(title, bodies):
        article_id = conn.execute(
            "INSERT INTO articles (source_id, external_id, content_hash, title, body_md, body_text)"
            " VALUES ('fangwei-mp', %s, %s, %s, '', '') RETURNING id",
            (title, title, title),
        ).fetchone()["id"]
        for ord_, body in enumerate(bodies):
            conn.execute(
                "INSERT INTO article_chunks (article_id, ord, heading, body) VALUES (%s, %s, '', %s)",
                (article_id, ord_, body),
            )
        return article_id

    cooling = article("冷却", ["已经打完", "文" * (mechanism.CHUNK_LIMIT + 100), "四个汉字"])
    ready = article("可处理", ["两个"])
    scored = mechanism.stale_chunks(conn, cooling)[:1]
    mechanism.save_scores(conn, cooling, scored, [2], "test")
    note_failure(conn, mechanism.FAILURE_KIND, cooling, "打分缺段")
    monkeypatch.setattr(mechanism.Settings, "from_env", lambda: settings)

    def no_oauth(_settings):
        raise AssertionError("盘点不应触发模型鉴权")

    monkeypatch.setattr(mechanism, "ensure_oauth", no_oauth)
    assert mechanism.main(["--limit", "0", "--source", "fangwei-mp"]) == 0
    report = capsys.readouterr().out
    assert "还要打分 2 篇、3 块" in report
    assert f"{mechanism.CHUNK_LIMIT + 6:,} 字" in report
    assert "当前可打 1 篇、1 块" in report
    assert "冷却中 1 篇、2 块" in report
    assert [row["id"] for row in mechanism.pending_articles(conn, ["fangwei-mp"], 10)] == [ready]

    # 打完眼下可处理的文章后，盘点也能看见剩余的冷却项。
    mechanism.save_scores(conn, ready, mechanism.stale_chunks(conn, ready), [2], "test")
    conn.commit()
    assert mechanism.main(["--limit", "0", "--source", "fangwei-mp"]) == 0
    report = capsys.readouterr().out
    assert "还要打分 1 篇、2 块" in report
    assert "当前可打 0 篇、0 块，冷却中 1 篇、2 块" in report
