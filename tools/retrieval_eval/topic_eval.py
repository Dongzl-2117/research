"""人工标注片段评测。默认只测词法，不用 stub 冒充真实向量效果。

同一份 topic_cases.json 可配合不同 PYTHONPATH 测改前/改后。只重建显式指定的 research_eval_* 合成库。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import statistics
import tempfile
import time
from datetime import UTC, datetime
from html import unescape
from pathlib import Path

from researchlib import embeddings
from researchlib.chunks import OCR_ORD_BASE, replace_translation_chunks
from researchlib.config import Settings
from researchlib.db import prepare
from researchlib.mentions import CompanyMatcher, load_company_rows, mentions_for_document, replace_mentions
from researchlib.retrieval import reset_stats_cache, search_articles
from researchlib.snippets import highlight_snippet
from researchlib.web.queries import Scope

CASES = Path(__file__).with_name("topic_cases.json")


def seed(conn, corpus):
    assert conn.info.dbname.startswith("research_eval_")
    conn.execute("TRUNCATE articles RESTART IDENTITY CASCADE")
    for c in corpus["companies"]:
        conn.execute(
            "INSERT INTO companies(ticker,name_zh,name_en,aliases,seeded) VALUES(%s,%s,%s,%s,false)"
            " ON CONFLICT(name_zh) DO NOTHING", (c["ticker"], c["name_zh"], c["name_en"], c["aliases"]),
        )
    matcher = CompanyMatcher(load_company_rows(conn))
    truth, names, passages = {}, {}, {}
    for doc in corpus["articles"]:
        body = "\n\n".join(f"## {block[0]}\n\n{block[1]}" for block in doc["blocks"])
        digest = hashlib.sha256((doc["title"] + body).encode()).hexdigest()
        aid = conn.execute(
            "INSERT INTO articles(source_id,external_id,content_hash,title,body_md,body_text,published_at)"
            " VALUES('topic-audit',%s,%s,%s,%s,%s,%s) RETURNING id",
            (doc["id"], digest, doc["title"], body, body, datetime(2026, 1, 1, tzinfo=UTC)),
        ).fetchone()["id"]
        names[aid] = doc["id"]
        for i, block in enumerate(doc["blocks"]):
            heading, text, label = block[:3]
            conn.execute("INSERT INTO article_chunks(article_id,ord,heading,body) VALUES(%s,%s,%s,%s)",
                         (aid, i, heading, text))
            truth[(aid, i)] = label
            passages[(aid, i)] = text
        if doc.get("ocr"):
            conn.execute(
                "INSERT INTO article_chunks(article_id,ord,heading,body,lang,anchor)"
                " VALUES(%s,%s,'图片里的字',%s,'ocr','img-synthetic')", (aid, OCR_ORD_BASE, doc["ocr"]),
            )
            truth[(aid, OCR_ORD_BASE)] = doc["ocr_label"]
            passages[(aid, OCR_ORD_BASE)] = doc["ocr"]
        replace_mentions(conn, aid, mentions_for_document(matcher, doc["title"], body + doc.get("ocr", "")))
        if doc.get("translation"):
            conn.execute(
                "INSERT INTO article_translations(article_id,content_hash,title_zh,body_html,body_text,complete)"
                " VALUES(%s,%s,%s,%s,%s,true)",
                (aid, digest, "开市客的经营机制", doc["translation"], doc["translation"]),
            )
            replace_translation_chunks(conn, aid)
            for r in conn.execute("SELECT ord,body FROM article_chunks WHERE article_id=%s AND lang='zh'", (aid,)):
                truth[(aid, r["ord"])] = "costco"
                passages[(aid, r["ord"])] = r["body"]
    conn.commit()
    reset_stats_cache()
    return truth, names, passages


def evaluate(conn, sources, corpus, truth, names, originals, *, legacy=False):
    rows = []
    for case in corpus["queries"]:
        elapsed = []
        for _ in range(3):
            start = time.perf_counter()
            result = search_articles(conn, case["text"], Scope(source_ids=["topic-audit"]), sources, limit=20)
            elapsed.append((time.perf_counter() - start) * 1000)
        ranked = [p for h in result.hits for p in h.passages][:10]
        covered, hits, examples = set(), [], []
        matcher = CompanyMatcher(load_company_rows(conn))
        focus_id = conn.execute("SELECT id FROM companies WHERE name_zh=%s", (case["company"],)).fetchone()["id"]
        focus_terms = [f for t in result.query.content_terms for f in t.forms] if result.query else []
        wrong_entity = 0
        duplicates = 0
        for i, p in enumerate(ranked):
            text = "\n".join([p.before, p.body, p.after])
            fragment = highlight_snippet(
                p.body, case["text"], radius=88 if legacy else 260, terms=result.terms,
                **({} if legacy else {"focus_terms": focus_terms}),
            )
            visible = unescape(re.sub(r"<[^>]+>", "", str(fragment)))
            if not legacy:
                visible = "\n".join([p.before[:160], visible, p.after[:160]])
            evidence = {key for key, label in truth.items()
                        if key[0] == p.article_id and label == case["id"] and originals[key] in text}
            covered |= evidence
            relevant = bool(evidence)
            hits.append(relevant)
            subjects = set(matcher.count(p.body))
            wrong_entity += i < 5 and bool(subjects - {focus_id}) and focus_id not in subjects
            duplicates += any(prev.article_id == p.article_id and prev.lang == p.lang
                              and prev.heading == p.heading and abs(prev.ord - p.ord) <= 1 for prev in ranked[:i])
            examples.append({"article": names[p.article_id], "chunk": p.ord, "lang": p.lang,
                             "relevant": relevant, "text": p.body, "visible_text": visible, "url": p.url})
        cid = conn.execute("SELECT id FROM companies WHERE name_zh=%s", (case["company"],)).fetchone()["id"]
        scoped = search_articles(conn, case["text"], Scope(source_ids=["topic-audit"], company_id=cid), sources)
        allowed = {r["article_id"] for r in conn.execute(
            "SELECT article_id FROM article_mentions WHERE company_id=%s", (cid,)
        )}
        violations = sum(h.article_id not in allowed for h in scoped.hits)
        positives = sum(label == case["id"] for label in truth.values())
        rows.append({"query": case["text"], "split": case["split"], "p_at_5": sum(hits[:5]) / 5,
                     "p_at_10": sum(hits[:10]) / 10, "coverage_at_10": len(covered) / max(positives, 1),
                     "relevant_returned": sum(hits), "returned": len(ranked), "gold_passages": positives,
                     "scope_violations": violations, "adjacent_duplicates": duplicates,
                     "wrong_entity_at_5": wrong_entity,
                     "latency_median_ms": round(statistics.median(elapsed), 2), "top": examples})
    return {"mode": "lexical_only", "real_embedding_model": None,
            "notes": "P@k 分母固定 k，缺位计不相关。覆盖按标注原文块；上下文完整出现也计覆盖。延迟为3次中位数。",
            "cases": rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--check", type=Path, help="核对片段回归门槛；不替代真实模型效果评估")
    parser.add_argument("--legacy-presentation", action="store_true", help="旧页面88字预览，仅供改前对比")
    args = parser.parse_args()
    if not args.url.rsplit("/", 1)[-1].startswith("research_eval_"):
        raise SystemExit("只允许显式指定 research_eval_* 合成评测库")
    corpus = json.loads(CASES.read_text())
    with tempfile.TemporaryDirectory(prefix="topic-eval-") as tmp:
        source_path = Path(tmp) / "sources.yaml"
        source_path.write_text(
            "sources:\n- {id: topic-audit, name: 合成评测来源, author: 王小明, kind: markdown, enabled: true}\n"
        )
        settings = Settings.from_env({"DATABASE_URL": args.url, "DATA_DIR": tmp, "SOURCES_FILE": str(source_path)})
        conn = prepare(settings)
        assert conn.info.dbname.startswith("research_eval_")
        embeddings.use_embedder(None)
        from researchlib.sources import load_sources
        truth, names, passages = seed(conn, corpus)
        report = evaluate(
            conn, load_sources(source_path), corpus, truth, names, passages, legacy=args.legacy_presentation
        )
        conn.close()
    if args.check:
        minimum = json.loads(args.check.read_text())
        for row in report["cases"]:
            expected = minimum[row["query"]]
            for metric in ("p_at_5", "p_at_10", "coverage_at_10"):
                if row[metric] + 1e-9 < expected[metric]:
                    raise SystemExit(f"片段评测退步：{row['query']} {metric}={row[metric]} < {expected[metric]}")
            if row["scope_violations"] or row["adjacent_duplicates"] or row["wrong_entity_at_5"]:
                raise SystemExit(f"片段范围或相邻去重失败：{row['query']}")
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        args.out.write_text(text + "\n")
    for row in report["cases"]:
        print(f"{row['query']}: P@5={row['p_at_5']:.2f}, P@10={row['p_at_10']:.2f}, "
              f"覆盖={row['coverage_at_10']:.2f}, 范围违反={row['scope_violations']}, "
              f"相邻重复={row['adjacent_duplicates']}, 中位={row['latency_median_ms']}ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
