"""冻结合成库的实际 /search DOM 验收；不调用向量、翻译或付费模型。

语料、标签、heading 和计分口径由独立验收方冻结。检索代码只收到普通文章和原始 query，
evidence 等真值仅在拿到页面以后使用。切换 PYTHONPATH 可在同一环境公平比较不同提交。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import socket
import statistics
import tempfile
import time
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import psycopg
from fastapi.testclient import TestClient
from psycopg.conninfo import conninfo_to_dict

from researchlib import embeddings
from researchlib.chunks import replace_chunks
from researchlib.config import Settings
from researchlib.db import prepare
from researchlib.mentions import rebuild_all
from researchlib.retrieval import reset_stats_cache, search_articles
from researchlib.sources import load_sources
from researchlib.web.app import create_app
from researchlib.web.queries import Scope

FROZEN = {
    "original": ("frozen_corpus.json", 34, 42, "e230d58faa4c8282965f6054b39a5ff73bbbc0f8b8aca3fd0efd5498e1c2cc9b"),
    "heldout": ("heldout_corpus.json", 19, 22, "5ab7ca21d9d310d944ad96c2d83bc11e390b16c08d4e1acc1bef5a11a7eb6ac2"),
}
SOURCE_ID = "synthetic-eval"
VOID = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "wbr"})


def normalized(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


@dataclass
class Node:
    tag: str
    attrs: dict[str, str]
    children: list[Node | str] = field(default_factory=list)

    def has_class(self, name: str) -> bool:
        return name in self.attrs.get("class", "").split()

    def walk(self):
        for child in self.children:
            if isinstance(child, Node):
                yield child
                yield from child.walk()

    def text(self, *, omit_links: bool = False) -> str:
        if omit_links and self.tag == "a":
            return ""
        parts = [child if isinstance(child, str) else child.text(omit_links=omit_links) for child in self.children]
        # inline <mark> 不插空格，避免把原文的一个词拆开；块级节点按浏览器可见的段落边界分隔。
        content = "".join(parts)
        return f" {content} " if self.tag in {"p", "blockquote", "div", "li", "br"} else content


class Page(HTMLParser):
    def __init__(self, html: str):
        super().__init__(convert_charrefs=True)
        self.root = Node("root", {})
        self.stack = [self.root]
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        node = Node(tag, {k: v or "" for k, v in attrs})
        self.stack[-1].children.append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.stack[-1].children.append(Node(tag, {k: v or "" for k, v in attrs}))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def checked_corpus(which: str) -> tuple[dict, str]:
    filename, articles, chunks, expected = FROZEN[which]
    path = Path(__file__).with_name(filename)
    content = path.read_bytes()
    actual = hashlib.sha256(content).hexdigest()
    if actual != expected:
        raise SystemExit(f"冻结文件哈希不符：{filename} {actual} != {expected}")
    corpus = json.loads(content)
    assert corpus["synthetic_only"] is True
    assert len(corpus["documents"]) == articles
    assert sum(len(doc["chunks"]) for doc in corpus["documents"]) == chunks
    return corpus, actual


def checked_url(url: str) -> str:
    target = conninfo_to_dict(url)
    database = target.get("dbname", "")
    if target.get("host") not in {"127.0.0.1", "localhost", "::1"} or not re.fullmatch(
        r"research_eval_[a-z0-9_]+", database
    ):
        raise SystemExit("只允许显式指定本机 research_eval_* 隔离合成库；拒绝其他目标")
    # 在 prepare（含迁移）前先检查实际数据库目标，不能只信 URL 字符串。
    with psycopg.connect(url) as conn:
        actual = conn.execute("SELECT current_database()").fetchone()[0]
        if actual != database:
            raise SystemExit(f"实际数据库目标不符：{actual} != {database}")
    return database


def seed(conn, corpus: dict) -> tuple[dict[int, str], dict[str, dict]]:
    assert re.fullmatch(r"research_eval_[a-z0-9_]+", conn.info.dbname)
    actual = conn.execute("SELECT current_database() AS name").fetchone()["name"]
    assert actual == conn.info.dbname
    conn.execute("TRUNCATE articles RESTART IDENTITY CASCADE")
    for company in corpus.get("companies", []):
        conn.execute(
            "INSERT INTO companies(name_zh,name_en,ticker,aliases,seeded) VALUES(%s,%s,%s,%s,false)"
            " ON CONFLICT(name_zh) DO NOTHING",
            (company["name_zh"], company["name_en"], company["ticker"], company["aliases"]),
        )
    ids, chunks = {}, {}
    for doc in corpus["documents"]:
        body = "\n\n".join(f"## 合成段 {i + 1}\n\n{chunk['text']}" for i, chunk in enumerate(doc["chunks"]))
        aid = conn.execute(
            "INSERT INTO articles(source_id,external_id,title,body_md,body_text,content_hash,published_at,raw_meta)"
            " VALUES(%s,%s,%s,%s,%s,%s,%s,%s::jsonb) RETURNING id",
            (
                SOURCE_ID, "synthetic-search:" + doc["id"], doc["title"], body, body,
                hashlib.sha256(body.encode("utf-8")).hexdigest(), datetime(2026, 1, 1, tzinfo=UTC),
                json.dumps({"synthetic": True}),
            ),
        ).fetchone()["id"]
        ids[aid] = doc["id"]
        count = replace_chunks(conn, aid, doc["title"], body)
        assert count == len(doc["chunks"]), (doc["id"], count)
        stored = conn.execute(
            "SELECT ord,heading,body FROM article_chunks WHERE article_id=%s ORDER BY ord", (aid,)
        ).fetchall()
        for i, (chunk, row) in enumerate(zip(doc["chunks"], stored, strict=True)):
            assert row["ord"] == i and row["heading"] == f"合成段 {i + 1}"
            assert normalized(row["body"]) == normalized(chunk["text"]), doc["id"]
            chunks[f"{doc['id']}:{i}"] = chunk
    conn.commit()
    rebuild_all(conn)
    reset_stats_cache()
    return ids, chunks


def windows_from_dom(html: str, ids: dict[int, str], found) -> list[dict]:
    page = Page(html)
    cards = [node for node in page.root.walk() if node.tag == "article" and node.has_class("item-card")]
    # 旧模板的第二个 p.item-hit 没有单独 href；只用 found 的 ord 对齐它的来源，不用 found 正文计分。
    centers = {hit.article_id: [p.ord for p in hit.passages[:2]] for hit in found.hits}
    windows = []
    for card_index, card in enumerate(cards):
        title = next(node for node in card.walk() if node.tag == "h3" and node.has_class("item-title"))
        title_link = next(node for node in title.walk() if node.tag == "a")
        aid = int(urlsplit(title_link.attrs["href"]).path.rstrip("/").rsplit("/", 1)[-1])
        assert aid in ids, f"隔离语料之外的文章 {aid}"
        extracts = [node for node in card.walk() if node.has_class("item-hit") and node.tag in {"p", "blockquote"}]
        for index, node in enumerate(extracts):
            links = [n.attrs["href"] for n in node.walk() if n.tag == "a" and "/articles/" in n.attrs.get("href", "")]
            href = links[0] if links else title_link.attrs["href"]
            linked_ord = parse_qs(urlsplit(href).query).get("chunk")
            ord_ = int(linked_ord[0]) if links and linked_ord else centers[aid][index]
            windows.append({
                "card": card_index, "article": ids[aid], "center": f"{ids[aid]}:{ord_}",
                "title": normalized(title.text()), "url": href,
                "visible_text": normalized(node.text(omit_links=True)),
            })
    return windows


def score_at_k(windows: list[dict], query: dict, chunks: dict[str, dict], k: int) -> dict:
    selected = windows[:k]
    card_text = defaultdict(list)
    for window in selected:
        card_text[window["card"]].append(window["visible_text"])
    card_text = {key: normalized(" ".join(text)) for key, text in card_text.items()}
    ordinary = deduplicated = repeated = wrong = 0
    covered, seen_credit, seen_groups = set(), set(), set()
    evaluated = []
    for window in selected:
        visible = window["visible_text"]
        article = window["article"]
        eligible, groups, topics = [], set(), set()
        visible_chunks = {window["center"]}
        for stable, chunk in chunks.items():
            if not stable.startswith(article + ":"):
                continue
            if normalized(chunk["text"]) in visible:
                visible_chunks.add(stable)
            label = chunk.get("labels", {}).get(query["id"], {})
            if label.get("grade", 0) != 2:
                continue
            if not any(normalized(text) in visible for text in label.get("evidence", [])):
                continue
            if any(normalized(chunks[context]["text"]) not in card_text[window["card"]]
                   for context in label.get("required_context", [])):
                continue
            eligible.append(stable)
            groups.add(chunk.get("duplicate_group") or stable)
            topics.update(topic for topic, text in label.get("topic_evidence", {}).items()
                          if normalized(text) in visible)
        duplicate_groups = {chunks[stable].get("duplicate_group") or stable for stable in visible_chunks}
        is_duplicate = bool(duplicate_groups & seen_groups)
        seen_groups |= duplicate_groups
        is_wrong = any(query["id"] in chunks[stable].get("wrong_entity_for", []) for stable in visible_chunks)
        relevant = bool(eligible)
        new_credit = bool(groups - seen_credit)
        ordinary += relevant
        deduplicated += new_credit
        repeated += is_duplicate
        wrong += is_wrong
        seen_credit |= groups
        covered |= topics
        evaluated.append({
            **window, "relevant": relevant, "deduplicated_relevant": new_credit,
            "eligible_blocks": eligible, "visible_blocks": sorted(visible_chunks),
            "topics": sorted(topics), "duplicate": is_duplicate, "wrong_entity": is_wrong,
        })
    return {
        "returned": len(selected), "visible_relevant": ordinary, "visible_p": ordinary / k,
        "deduplicated_relevant": deduplicated, "visible_deduplicated_p": deduplicated / k,
        "topic_count": len(covered), "topic_coverage": len(covered) / max(1, len(query["topics"])),
        "topics": sorted(covered), "wrong_entity_count": wrong, "wrong_entity_rate": wrong / max(1, len(selected)),
        "duplicate_count": repeated, "duplicate_rate": repeated / max(1, len(selected)), "windows": evaluated,
    }


def no_network(*args, **kwargs):
    raise AssertionError("冻结评测禁止网络或模型请求")


def source_manifest() -> dict[str, str]:
    import researchlib.retrieval as implementation
    root = Path(implementation.__file__).resolve().parent
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.suffix in {".py", ".html", ".sql"}
    }


def run(settings, corpus: dict, *, html_dir: Path | None) -> dict:
    conn = prepare(settings)
    try:
        embeddings.use_embedder(None)
        ids, chunks = seed(conn, corpus)
        sources = load_sources(settings.sources_path)
        cases = []
        # TestClient 走真实路由/模板，在进程内发送 ASGI 请求；不监听端口。
        # PYTEST_CURRENT_TEST 关闭产品已有的后台保养/摘要工作线程，避免整篇认重改变冻结装载。
        with patch.dict(os.environ, {"PYTEST_CURRENT_TEST": "frozen-search-acceptance"}), \
             patch.object(urllib.request, "urlopen", side_effect=no_network), \
             patch.object(socket.socket, "connect", side_effect=no_network), \
             TestClient(create_app(settings)) as client:
            for query in corpus["queries"]:
                reset_stats_cache()
                found = search_articles(conn, query["text"], Scope(source_ids=[SOURCE_ID]), sources, limit=20)
                response = client.get("/search", params={"q": query["text"]})
                assert response.status_code == 200, response.text
                html = response.text
                windows = windows_from_dom(html, ids, found)
                elapsed = []
                for _ in range(20):
                    start = time.perf_counter()
                    measured = client.get("/search", params={"q": query["text"]})
                    elapsed.append((time.perf_counter() - start) * 1000)
                    assert measured.status_code == 200
                    assert windows_from_dom(measured.text, ids, found) == windows, "热路由期间结果不稳定"
                if html_dir:
                    html_dir.mkdir(parents=True, exist_ok=True)
                    (html_dir / f"{query['id']}.html").write_text(html, encoding="utf-8")
                cases.append({
                    "id": query["id"], "query": query["text"], "split": query["split"],
                    "currently_blind": False, "visible_total": len(windows),
                    "at_5": score_at_k(windows, query, chunks, 5),
                    "at_10": score_at_k(windows, query, chunks, 10),
                    "warm_route_latency_ms": {
                        "samples": 20, "p50": round(statistics.median(elapsed), 3),
                        "p95": round(sorted(elapsed)[math.ceil(len(elapsed) * 0.95) - 1], 3),
                        "values": [round(value, 3) for value in elapsed],
                    },
                })
        return {"article_id_mapping": {str(aid): stable for aid, stable in ids.items()}, "cases": cases}
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", choices=FROZEN, required=True)
    parser.add_argument("--url", required=True, help="显式本机 research_eval_* 隔离库；必须提前建库")
    parser.add_argument("--label", required=True, help="版本标记；建议完整提交 SHA")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--html-dir", type=Path)
    parser.add_argument("--check", action="store_true", help="按原三题门槛失败退出；结果仍先保存")
    args = parser.parse_args()
    corpus, digest = checked_corpus(args.corpus)
    implementation_manifest = source_manifest()
    database = checked_url(args.url)
    with tempfile.TemporaryDirectory(prefix="search-acceptance-") as tmp:
        source_path = Path(tmp) / "sources.yaml"
        source_path.write_text(
            "sources:\n- {id: synthetic-eval, name: 合成验收库, author: 测试作者甲, kind: markdown, enabled: true}\n",
            encoding="utf-8",
        )
        settings = Settings.from_env({
            "DATABASE_URL": args.url, "DATA_DIR": str(Path(tmp) / "data"), "SOURCES_FILE": str(source_path),
            "RESEARCH_THESES_AUTO": "0", "EMBEDDING_BASE_URL": "", "RESEARCH_STALE_BANNER": "0",
        })
        report = run(settings, corpus, html_dir=args.html_dir)
    if source_manifest() != implementation_manifest:
        raise SystemExit("评测期间产品源码/模板发生变化；拒绝把混合版本写为一个结果")
    import researchlib.retrieval as implementation
    report = {
        "label": args.label, "corpus": args.corpus, "corpus_sha256": digest,
        "implementation_file": str(Path(implementation.__file__).resolve()),
        "implementation_manifest_sha256": hashlib.sha256(
            json.dumps(implementation_manifest, sort_keys=True).encode("utf-8")
        ).hexdigest(),
        "implementation_manifest": implementation_manifest,
        "mode": "lexical_only", "real_embedding_model_tested": False,
        "database": database, "notice": corpus["notice"],
        "scoring": "真实 DOM；P 分母固定 k；机制和必要同卡上下文须实际可见；标签仅用于计分；热路由20次。",
        **report,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    failures = []
    for case in report["cases"]:
        row = case["at_5"]
        print(f"{case['query']}: 可见P@5={row['visible_p']:.2f}, 去重P@5={row['visible_deduplicated_p']:.2f}, "
              f"覆盖={row['topic_count']}, 误归={row['wrong_entity_count']}, 重复={row['duplicate_count']}, "
              f"p50={case['warm_route_latency_ms']['p50']}ms p95={case['warm_route_latency_ms']['p95']}ms")
        if case["id"] in {"costco", "asml", "netflix"} and (
            row["visible_deduplicated_p"] < 0.8
            or row["topic_count"] < (3 if case["id"] == "costco" else 2)
            or row["wrong_entity_count"] or row["duplicate_count"]
        ):
            failures.append(case["query"])
    if args.check and failures:
        raise SystemExit("未达冻结验收门槛：" + "、".join(failures))


if __name__ == "__main__":
    main()
