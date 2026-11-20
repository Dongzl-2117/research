"""问库标为不相关的原文在资料更新后仍按当时的出处核对。"""

import re

from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from researchlib import ask as ask_module
from researchlib.chunks import replace_chunks
from researchlib.feedback import citation_hash, replay
from researchlib.migrate import MIGRATIONS, split_sql
from researchlib.seed import load_sample
from researchlib.sources import enabled_sources, load_sources
from researchlib.web.app import create_app


def test_replay_follows_marked_passage_across_rechunk(conn, settings, monkeypatch):
    monkeypatch.setenv("ALLOW_SAMPLE_SEED", "1")
    load_sample(conn, settings)
    monkeypatch.setattr(ask_module, "chat", lambda *a, **kw: ("库存见原文[1]。", "fixture"))
    monkeypatch.setattr(ask_module, "llm_configured", lambda _: True)
    article = conn.execute("SELECT id, title FROM articles WHERE external_id='sample:battery-note'").fetchone()
    sources = enabled_sources(load_sources(settings.sources_path))
    title = article["title"]
    article_id = article["id"]
    evidence = "宁德时代的库存正在下降。"
    replacement = "宁德时代的库存正在上升。"

    def update(text):
        conn.execute("UPDATE articles SET body_md = %s WHERE id = %s", (text, article_id))
        replace_chunks(conn, article_id, title, text)
        conn.commit()

    update(f"## 库存\n\n{evidence}")
    with TestClient(create_app(settings)) as client:
        posted = client.post("/ask", data={"q": "宁德时代的库存", "article": article_id}, follow_redirects=False)
        page = posted.headers["location"]
        answer_page = client.get(page).text
        assert page.startswith("/ask/") and "正在下降。" in answer_page
        digest = re.search(r'name="passage_hash" value="([0-9a-f]+)"', answer_page).group(1)
        form = {"label": "irrelevant", "article": article_id, "chunk": 0, "n": 1, "passage_hash": digest}
        assert "已标不相关" in client.post(f"{page}/feedback", data=form).text
        stale = client.post(f"{page}/feedback", data={**form, "passage_hash": "0" * 32})
        assert "回答的出处已更新" in stale.text and "已标不相关" in stale.text
    assert replay(conn, settings, sources)[0]["irrelevant_still"] == 1

    # 原块号还是 0，库存结论已经反过来。
    update(f"## 库存\n\n{replacement}")
    changed = replay(conn, settings, sources)[0]
    assert changed["irrelevant_still"] == 0
    assert changed["irrelevant_unavailable"] == 1

    # 删掉回答仍能回放。原文换到了块号 1，前面的块仍然是另一份证据。
    conn.execute("DELETE FROM llm_answers")
    conn.commit()
    update(f"## 扩产\n\n下一份备忘记录产能。\n\n## 库存\n\n{evidence}")
    moved = replay(conn, settings, sources)[0]
    assert moved["irrelevant_still"] == 1 and moved["irrelevant_unavailable"] == 0


def test_old_feedback_only_recovers_from_a_contemporaneous_answer(conn, settings, monkeypatch):
    monkeypatch.setenv("ALLOW_SAMPLE_SEED", "1")
    load_sample(conn, settings)
    chunk = conn.execute("SELECT article_id, ord, heading, body FROM article_chunks LIMIT 1").fetchone()
    citation = {
        "article_id": chunk["article_id"], "ord": chunk["ord"], "text": chunk["body"], "heading": chunk["heading"]
    }
    for later in (False, True):
        answer = conn.execute(
            "INSERT INTO llm_answers (question, answer, citations) VALUES ('库存', '', %s) RETURNING id",
            (Jsonb([citation]),),
        ).fetchone()
        conn.execute(
            """INSERT INTO answer_feedback (answer_id, question, article_id, chunk_ord, label)
               VALUES (%s, '库存', %s, %s, 'irrelevant')""",
            (answer["id"], chunk["article_id"], chunk["ord"]),
        )
        if later:
            conn.execute("UPDATE llm_answers SET updated_at = now() + interval '1 day' WHERE id = %s", (answer["id"],))
    for statement in split_sql((MIGRATIONS / "041_answer_feedback_passage.sql").read_text()):
        conn.execute(statement)
    labels = conn.execute("SELECT passage_hash FROM answer_feedback ORDER BY id").fetchall()
    assert [label["passage_hash"] for label in labels] == [citation_hash(citation), None]
