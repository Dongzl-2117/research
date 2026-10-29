"""只检查三个无答案查询的片段降级标识；不改冻结文本、标签、检索结果或排序。

它们由独立验收方补充，冻结 original 语料没有所问证据。不是通用语义质量评测。
可用 PYTHONPATH 切换版本，报告实际前五、标识缺失计数及后端顺序，供改前/改后比较。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import tempfile
import urllib.request
from pathlib import Path
from unittest.mock import patch

from evaluate import (
    SOURCE_ID,
    checked_corpus,
    checked_url,
    no_network,
    seed,
    source_manifest,
    windows_from_dom,
)
from fastapi.testclient import TestClient

from researchlib import embeddings
from researchlib.config import Settings
from researchlib.db import prepare
from researchlib.retrieval import reset_stats_cache, search_articles
from researchlib.sources import load_sources
from researchlib.web.app import create_app
from researchlib.web.queries import Scope

PROBES = ("Costco冷链温控记录", "Netflix推荐算法偏差审计", "ASML维护合同违约赔偿")
WARNINGS = ("部分词匹配，主题未核实", "同公司参考", "同公司原文，主题待核对")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    checked_url(args.url)
    corpus, digest = checked_corpus("original")
    manifest = source_manifest()
    cases = []
    with tempfile.TemporaryDirectory(prefix="search-labels-") as tmp:
        sources_path = Path(tmp) / "sources.yaml"
        sources_path.write_text(
            "sources:\n- {id: synthetic-eval, name: 合成验收库, author: 测试作者甲, kind: markdown, enabled: true}\n"
        )
        settings = Settings.from_env({
            "DATABASE_URL": args.url, "DATA_DIR": tmp, "SOURCES_FILE": str(sources_path),
            "EMBEDDING_BASE_URL": "", "RESEARCH_THESES_AUTO": "0",
        })
        conn = prepare(settings)
        try:
            ids, _chunks = seed(conn, corpus)
            sources = load_sources(sources_path)
            embeddings.use_embedder(None)
            with patch.dict(os.environ, {"PYTEST_CURRENT_TEST": "search-label-checks"}), \
                 patch.object(urllib.request, "urlopen", side_effect=no_network), \
                 patch.object(socket.socket, "connect", side_effect=no_network), \
                 TestClient(create_app(settings)) as client:
                for query in PROBES:
                    reset_stats_cache()
                    found = search_articles(conn, query, Scope(source_ids=[SOURCE_ID]), sources)
                    response = client.get("/search", params={"q": query})
                    assert response.status_code == 200
                    windows = windows_from_dom(response.text, ids, found)[:5]
                    unmarked = sum(not any(label in w["visible_text"] for label in WARNINGS) for w in windows)
                    cases.append({
                        "query": query, "returned_at_5": len(windows), "unmarked_off_topic_at_5": unmarked,
                        "top_5": windows,
                        "backend_order": [
                            {"article": ids[h.article_id], "score": h.score,
                             "chunks": [p.ord for p in h.passages]} for h in found.hits
                        ],
                    })
        finally:
            conn.close()
    assert manifest == source_manifest(), "测试期间产品源码变化"
    report = {
        "label": args.label, "synthetic_only": True, "mode": "lexical_only",
        "corpus_sha256": digest, "notice": __doc__,
        "implementation_manifest_sha256": hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest(),
        "cases": cases,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    for case in cases:
        print(f"{case['query']}: 前五实际返回={case['returned_at_5']}, 离题未标={case['unmarked_off_topic_at_5']}")
    if args.check and any(not c["returned_at_5"] or c["unmarked_off_topic_at_5"] for c in cases):
        raise SystemExit("无答案查询的片段降级标识未通过")


if __name__ == "__main__":
    main()
