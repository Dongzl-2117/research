"""回答页上的标注，以及按这些真实问题回放检索。

合成语料量出来的准确率，覆盖不到真实的问法和作者真实的用词。回答页上每段原文可以标「不相关」，
也可以补上「漏了」的文章；这些存进 answer_feedback，当成真值。以后改了检索、或者库变大了，跑

    research-eval-feedback

按当时的问题和范围重新检索一遍，看漏掉的文章现在找回来几篇、标过不相关的段落还剩几段。
默认复用当时模型给的同义词（存在 llm_answers.stats 里），不再调模型；加 --expand 才重新问模型。
最后附上近期整合的耗时、批数和同义词扩展有没有失败。
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys

from psycopg.types.json import Jsonb


def parse_article_ref(ref: str) -> tuple[int | None, int | None]:
    """「https://…/articles/123?chunk=4」「/articles/123」「123」都认。返回 (文章 id, 块号)。"""
    text = (ref or "").strip()
    if not text:
        return None, None
    match = re.search(r"/articles/(\d+)(?:/(?:zh|original))?(?:\?chunk=(\d+))?", text)
    if match is None:
        match = re.fullmatch(r"\s*(\d+)\s*", text)
        if match is None:
            return None, None
        return int(match.group(1)), None
    return int(match.group(1)), int(match.group(2)) if match.group(2) else None


def marks(conn, answer_id: int) -> dict:
    rows = conn.execute(
        """
        SELECT f.article_id, f.chunk_ord, f.label, f.note, a.title
        FROM answer_feedback f JOIN articles a ON a.id = f.article_id
        WHERE f.answer_id = %s
        ORDER BY f.created_at
        """,
        (answer_id,),
    ).fetchall()
    return {
        "irrelevant": {(row["article_id"], row["chunk_ord"]) for row in rows if row["label"] == "irrelevant"},
        "missing": [
            {"article_id": row["article_id"], "chunk": row["chunk_ord"], "title": row["title"], "note": row["note"]}
            for row in rows
            if row["label"] == "missing"
        ],
    }


def _answer(conn, answer_id: int):
    return conn.execute("SELECT id, question, scope FROM llm_answers WHERE id = %s", (answer_id,)).fetchone()


def toggle_irrelevant(conn, answer_id: int, article_id: int, chunk_ord: int | None) -> bool:
    """标或取消「不相关」。返回标完之后是不是不相关。"""
    answer = _answer(conn, answer_id)
    if answer is None:
        raise LookupError("没有这条回答")
    removed = conn.execute(
        """
        DELETE FROM answer_feedback
        WHERE answer_id = %s AND article_id = %s AND coalesce(chunk_ord, -1) = coalesce(%s, -1)
          AND label = 'irrelevant'
        RETURNING id
        """,
        (answer_id, article_id, chunk_ord),
    ).fetchall()
    if removed:
        conn.commit()
        return False
    conn.execute(
        """
        INSERT INTO answer_feedback (answer_id, question, scope, article_id, chunk_ord, label)
        VALUES (%s, %s, %s, %s, %s, 'irrelevant')
        ON CONFLICT DO NOTHING
        """,
        (answer_id, answer["question"], Jsonb(answer["scope"] or {}), article_id, chunk_ord),
    )
    conn.commit()
    return True


def add_missing(conn, answer_id: int, ref: str, note: str = "") -> int:
    """补一篇「漏了」的文章。返回文章 id；认不出或库里没有时抛 ValueError。"""
    answer = _answer(conn, answer_id)
    if answer is None:
        raise LookupError("没有这条回答")
    article_id, chunk = parse_article_ref(ref)
    if article_id is None:
        raise ValueError("贴文章链接或编号，例如 /articles/123")
    exists = conn.execute("SELECT 1 FROM articles WHERE id = %s", (article_id,)).fetchone()
    if exists is None:
        raise ValueError(f"库里没有文章 {article_id}")
    conn.execute(
        """
        INSERT INTO answer_feedback (answer_id, question, scope, article_id, chunk_ord, label, note)
        VALUES (%s, %s, %s, %s, %s, 'missing', %s)
        ON CONFLICT DO NOTHING
        """,
        (answer_id, answer["question"], Jsonb(answer["scope"] or {}), article_id, chunk, (note or "")[:300]),
    )
    conn.commit()
    return article_id


def labelled_questions(conn) -> list[dict]:
    """有标注的问题，按问题和范围合并。同一个问题问过几次，标注都算上。"""
    rows = conn.execute(
        """
        SELECT f.question, f.scope, f.article_id, f.chunk_ord, f.label, f.answer_id
        FROM answer_feedback f
        ORDER BY f.created_at
        """
    ).fetchall()
    grouped: dict[tuple[str, str], dict] = {}
    for row in rows:
        key = (row["question"], json.dumps(row["scope"] or {}, sort_keys=True, ensure_ascii=False))
        item = grouped.setdefault(
            key,
            {
                "question": row["question"],
                "scope": row["scope"] or {},
                "irrelevant": set(),
                "missing": set(),
                "answers": set(),
            },
        )
        if row["answer_id"] is not None:
            item["answers"].add(row["answer_id"])
        if row["label"] == "irrelevant":
            item["irrelevant"].add((row["article_id"], row["chunk_ord"]))
        else:
            item["missing"].add(row["article_id"])
    return list(grouped.values())


def _stored_expansions(conn, answer_ids: set[int]) -> dict:
    if not answer_ids:
        return {}
    row = conn.execute(
        "SELECT stats FROM llm_answers WHERE id = ANY(%s) AND stats ? 'expansions' ORDER BY id DESC LIMIT 1",
        (sorted(answer_ids),),
    ).fetchone()
    return (row["stats"] or {}).get("expansions") or {} if row else {}


def replay(conn, settings, sources, *, expand: bool = False) -> list[dict]:
    from researchlib.ask import expand_terms, scope_from_json
    from researchlib.retrieval import analyze, retrieve

    results = []
    for item in labelled_questions(conn):
        scope = scope_from_json(item["scope"])
        preset = bool(item["scope"].get("preset"))
        expansions = {} if preset else _stored_expansions(conn, item["answers"])
        if expand and not preset:
            originals = [t.text for t in analyze(conn, item["question"], sources).terms if t.kind == "word"]
            expansions = expand_terms(settings, item["question"], originals)
        found = retrieve(conn, item["question"], scope, sources, purpose="deep", preset=preset, expansions=expansions)
        articles = {p.article_id for p in found.passages}
        keys = {(p.article_id, p.ord) for p in found.passages}
        results.append(
            {
                "question": item["question"],
                "passages": len(found.passages),
                "missing": len(item["missing"]),
                "missing_found": len([a for a in item["missing"] if a in articles]),
                "missing_still": sorted(a for a in item["missing"] if a not in articles),
                "irrelevant": len(item["irrelevant"]),
                "irrelevant_still": len([k for k in item["irrelevant"] if k in keys]),
            }
        )
    return results


def timing(conn, limit: int = 50) -> dict:
    rows = conn.execute(
        "SELECT stats FROM llm_answers WHERE status = 'done' AND stats <> '{}'::jsonb ORDER BY id DESC LIMIT %s",
        (limit,),
    ).fetchall()
    stats = [row["stats"] for row in rows]
    if not stats:
        return {}

    def median(key: str) -> float:
        values = [float(item.get(key) or 0) for item in stats]
        return round(statistics.median(values), 1)

    return {
        "answers": len(stats),
        "total_s": median("total_s"),
        "expand_s": median("expand_s"),
        "synthesize_s": median("synthesize_s"),
        "passages": median("passages"),
        "batched": len([item for item in stats if int(item.get("batches") or 1) > 1]),
        "no_expansion": len([item for item in stats if not item.get("expansions")]),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="按回答页上标过的真实问题回放检索，看漏召回和噪音")
    parser.add_argument("--expand", action="store_true", help="重新让模型补同义词（默认复用当时存下的）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args(argv)
    from researchlib.config import Settings
    from researchlib.db import ensure_search_session, prepare
    from researchlib.sources import enabled_sources, load_sources

    settings = Settings.from_env()
    conn = prepare(settings)
    try:
        ensure_search_session(conn)
        sources = enabled_sources(load_sources(settings.sources_path))
        results = replay(conn, settings, sources, expand=args.expand)
        times = timing(conn)
    finally:
        conn.close()
    if args.json:
        print(json.dumps({"questions": results, "timing": times}, ensure_ascii=False, indent=2))
        return 0
    if not results:
        print("还没有标注。回答页上标「不相关」或补「漏了」的文章之后再跑。")
    for item in results:
        line = f"{item['question'][:40]}：{item['passages']} 段"
        if item["missing"]:
            line += f"；漏的 {item['missing']} 篇现在找回 {item['missing_found']} 篇"
            if item["missing_still"]:
                line += "（还漏 " + "、".join(str(a) for a in item["missing_still"][:8]) + "）"
        if item["irrelevant"]:
            line += f"；标过不相关的 {item['irrelevant']} 段还剩 {item['irrelevant_still']} 段"
        print(line)
    if results:
        missing = sum(item["missing"] for item in results)
        found = sum(item["missing_found"] for item in results)
        noise = sum(item["irrelevant"] for item in results)
        still = sum(item["irrelevant_still"] for item in results)
        print(f"合计：漏召回 {found}/{missing} 找回，不相关 {still}/{noise} 还在")
    if times:
        print(
            f"近 {times['answers']} 次整合：中位耗时 {times['total_s']} 秒（同义词 {times['expand_s']}、"
            f"作答 {times['synthesize_s']}），中位 {times['passages']} 段，分批 {times['batched']} 次，"
            f"同义词为空 {times['no_expansion']} 次"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
