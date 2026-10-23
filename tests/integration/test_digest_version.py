"""摘要提示词换了版本：旧摘要照常显示，只由显式 research-digest 按新版重写。"""

import os

from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from researchlib import digest as digest_mod
from researchlib.seed import load_sample
from researchlib.web.app import create_app


def test_old_digests_show_without_implicit_rewrite_and_explicit_processing_still_works(conn, settings, monkeypatch):
    os.environ["ALLOW_SAMPLE_SEED"] = "1"
    load_sample(conn, settings)
    row = conn.execute(
        "SELECT id, content_hash, source_id FROM articles WHERE external_id = 'sample:tencent-ads'"
    ).fetchone()
    conn.execute(
        """
        INSERT INTO article_digests (article_id, content_hash, model, summary, points, tags, prompt_version)
        VALUES (%s, %s, 'grok-old', '旧版导读：腾讯广告', %s, '[]', 1)
        """,
        (row["id"], row["content_hash"], Jsonb(["旧要点"])),
    )
    conn.commit()
    queued = []
    monkeypatch.setattr("researchlib.digest.schedule_digests", lambda _settings, jobs: queued.extend(jobs))
    with TestClient(create_app(settings)) as client:
        home = client.get("/").text
        assert "旧版导读：腾讯广告" in home
    assert queued == []  # GET 只展示；显式处理命令仍能选择这个旧摘要。
    assert [item["id"] for item in digest_mod.rows_needing_digest(conn, [row["id"]])] == [row["id"]]
    pending = digest_mod.pending_articles(conn, [row["source_id"]], 50)
    # 没有摘要的排在只是版本旧了的前面。
    assert pending[-1]["id"] == row["id"]

    monkeypatch.setattr(
        digest_mod,
        "chat",
        lambda _s, _m, **_k: (
            '{"summary": "新版导读", "points": ["判断：广告加载率还能升"], "tags": ["腾讯"]}',
            "grok-test",
        ),
    )
    digest_mod.digest_article(conn, settings, digest_mod.rows_needing_digest(conn, [row["id"]])[0], "方伟")
    saved = conn.execute(
        "SELECT summary, prompt_version FROM article_digests WHERE article_id = %s", (row["id"],)
    ).fetchone()
    assert saved["summary"] == "新版导读" and saved["prompt_version"] == digest_mod.PROMPT_VERSION
    assert digest_mod.rows_needing_digest(conn, [row["id"]]) == []


def test_prompt_keeps_mechanism_first_and_optional_risks():
    assert "先写判断" in digest_mod.PROMPT and "机制" in digest_mod.PROMPT
    assert "关键数字" not in digest_mod.PROMPT
    assert "没谈到就不写" in digest_mod.PROMPT
