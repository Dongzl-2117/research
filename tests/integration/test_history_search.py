import json
from datetime import timedelta

import pytest
from psycopg.types.json import Jsonb

from researchlib import history_search
from researchlib.article_history import confirm_commits


def _insert(conn, title, body, *, meta=None, source="fangwei-mp"):
    return conn.execute(
        """INSERT INTO articles (source_id,external_id,title,published_at,body_md,body_text,content_hash,raw_meta)
           VALUES (%s,%s,%s,'2020-01-01',%s,%s,'synthetic',%s) RETURNING id""",
        (source, title, title, body, body, Jsonb(meta or {})),
    ).fetchone()["id"]


def test_readonly_cross_article_evidence_uses_verified_versions_and_reports_coverage(
    conn, settings, monkeypatch, capsys
):
    first = _insert(conn, "当时的判断", "旧版独有 BuyerMargin，当前版将删除")
    second = _insert(conn, "普通标题", "当时没有新增观点")
    replies = [
        {"text": "作者回复提及 BUYERMARGIN", "published_at": "2025-01-02T00:00:00+08:00"},
        {"text": "作者回复晚出的未来结论", "published_at": "2100-01-02T00:00:00+08:00"},
        {"text": "无日期的未来结论"},
    ]
    third = _insert(
        conn,
        "现场答问",
        "原帖\n\n## 评论里的回复\n旧格式的未来结论",
        meta={"author_replies": replies},
        source="fangwei-zsxq",
    )
    conn.commit()
    confirm_commits(conn)
    cutoff = conn.execute("SELECT clock_timestamp() AS t").fetchone()["t"]
    conn.commit()
    conn.execute("UPDATE articles SET title='当前标题', body_md='现版删掉了旧词' WHERE id=%s", (first,))
    conn.execute("UPDATE articles SET title='全新 BUYERMARGIN',body_md='后来的未来结论' WHERE id=%s", (second,))
    fourth = _insert(conn, "晚入库的文章", "BUYERMARGIN")
    conn.commit()

    monkeypatch.setattr(history_search.Settings, "from_env", lambda: settings)
    read_version = history_search.read_version

    def check_transaction(reader, *args):
        assert reader.execute("SHOW transaction_read_only").fetchone()["transaction_read_only"] == "on"
        assert reader.execute("SHOW transaction_isolation").fetchone()["transaction_isolation"] == "repeatable read"
        return read_version(reader, *args)

    monkeypatch.setattr(history_search, "read_version", check_transaction)

    def run(q, *more, at=cutoff):
        history_search.main(["--as-of", at.isoformat(), "--query", q, *more])
        return json.loads(capsys.readouterr().out)

    p1 = run("buyermargin", "--scan-limit", "2")
    assert p1["scanned"] == 2 and p1["next_after_id"] == second and p1["through_id"] == fourth
    assert [(m["title"], m["match"]) for m in p1["matches"]] == [("当时的判断", "BuyerMargin")]
    assert p1["matches"][0]["url"].startswith(f"https://investment.zldong.com/articles/{first}/history?as_of=")
    _insert(conn, "翻页时才刚入库", "BUYERMARGIN")
    conn.commit()
    p2 = run("buyermargin", "--scan-limit", "2", "--after-id", str(second), "--through-id", str(fourth))
    assert p2["next_after_id"] is None and p2["scanned"] == 2 and p2["coverage"]["not_yet_archived"] == 1
    assert [(m["article_id"], m["field"]) for m in p2["matches"]] == [(third, "author_reply")]
    assert "2025-01-02" in p2["matches"][0]["author_reply_published_at"]
    missing = run("未来结论")
    assert missing["status"] == "no_match_in_verified" and not missing["matches"]
    assert run("未来结论", at=cutoff - timedelta(days=60))["status"] == "insufficient_evidence"
    conn.execute("UPDATE articles SET raw_meta=%s WHERE id=%s", (Jsonb({"poster_role": "guest"}), first))
    conn.commit()
    assert [m["article_id"] for m in run("buyermargin")["matches"]] == [third]
    for args in [("--scan-limit", "251"), ("--through-id", "1", "--after-id", "3")]:
        with pytest.raises(SystemExit) as exc:
            run("buyermargin", *args)
        assert exc.value.code == 2
