"""按句切开译的长段：「只看中文」并回一段，中文句子之间不加空格；对照页照旧逐段配对。"""

import re
from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from researchlib.ingest.model import ParsedDoc
from researchlib.ingest.store import store_documents
from researchlib.sources import load_sources
from researchlib.translate import service
from researchlib.translate.llm import ChatJsonResult
from researchlib.web.app import create_app

SHANGHAI = ZoneInfo("Asia/Shanghai")
PAD = (
    " and the rest of this sentence keeps going with more detail about the quarter"
    " so that the paragraph stays well past the split threshold"
)
SENTENCES = [
    ("The airline rebuilt its app first because loyalty starts with booking",
     "这家航空公司先重做了 App，因为忠诚度从订票开始。"),
    ("Then it fixed the operating data so every delay could be traced",
     "接着它理顺了运营数据，每次延误都能追到原因。"),
    ("Only after that did it change how managers talked about blame",
     "在那之后，它才改了经理们在会上谈责任的方式。"),
    ("The result was a culture that cared about fixing problems",
     "结果形成了一种重在解决问题的文化。"),
    ("That is why the customer experience improved so quickly",
     "所以客户体验改善得很快。"),
]
TAIL = ("A short closing paragraph stays on its own.", "收尾的短段落单独成段。")


def _translate(**kwargs):
    """译制替身：英文句子换成对应的中文句子（标题给一个固定译名），回执要的 id 和摘要原样带回。"""
    user = kwargs["user"]
    values = []
    for identity, source_hash, html in zip(user["segment_ids"], user["source_hashes"], user["segments"], strict=True):
        text = re.sub(r"<[^>]+>", "", html).strip()
        chinese = next((zh for en, zh in [*SENTENCES, TAIL] if text.startswith(en)), "航空公司的技术转型")
        values.append({"id": identity, "source_hash": source_hash, "html": f"<p>{chinese}</p>"})
    return ChatJsonResult(data={"t": values}, model="synthetic-model", usage=None, latency_ms=0)


def test_reading_page_joins_sentences_of_a_long_paragraph(conn, settings, monkeypatch):
    body = " ".join(en + PAD + "." for en, _zh in SENTENCES) + "\n\n" + TAIL[0]
    source = next(s for s in load_sources(settings.sources_path) if s.id == "stratechery")
    store_documents(
        conn,
        source=replace(source, min_chars=0),
        docs=[ParsedDoc(
            external_id="strat:reading", title="Synthetic airline interview",
            published_at=datetime(2026, 1, 15, 19, 0, tzinfo=SHANGHAI), body_md=body,
            raw_meta={"format": "markdown", "language": "en"}, image_refs=[],
        )],
        data_dir=settings.data_dir, archive_files=1, images_in_archive=0, errors=[],
    )
    article_id = conn.execute("SELECT id FROM articles WHERE external_id = 'strat:reading'").fetchone()["id"]
    monkeypatch.setattr(service, "chat_json", _translate)
    assert service.translate_article(conn, article_id, data_dir=settings.data_dir).status == "translated"
    conn.commit()
    stored = conn.execute(
        "SELECT body_html FROM article_translations WHERE article_id = %s", (article_id,)
    ).fetchone()["body_html"]
    # 存下来的译文里，长段是一句一个 <p data-src-para>：这一篇确实按句切开译了。
    assert stored.count("data-src-para") == len(SENTENCES)
    joined = "".join(zh for _en, zh in SENTENCES)
    with TestClient(create_app(settings)) as client:
        reading = client.get(f"/articles/{article_id}/zh").text
        paired = client.get(f"/articles/{article_id}").text
    # 「只看中文」：五句在同一个 <p> 里，句号后面没有空格；收尾那段单独一段。
    assert re.search(r"<p\b[^>]*>" + re.escape(joined) + "</p>", reading)
    assert re.search(r"<p\b[^>]*>" + re.escape(TAIL[1]) + "</p>", reading)
    assert "data-src-para" not in reading
    # 对照页：长段并成一对，中文那一栏同样不多空格；收尾那段另成一对。
    assert paired.count('class="bilingual-pair"') == 2
    assert joined in paired
    assert ". Then it fixed the operating data" in paired
