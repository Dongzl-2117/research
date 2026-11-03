"""历史译文的只读分类与定向修复计划；没有 apply、模型或 db.prepare 路径。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from researchlib.config import Settings
from researchlib.db import connect
from researchlib.translate.display import article_pairs
from researchlib.translate.provenance import AlignmentError, digest, reconstruction, source_manifest, verify
from researchlib.translate.service import _render_original


def classify(source: str, translated: str, provenance: dict | None) -> dict:
    state = verify(source, translated, provenance)
    display = article_pairs(source, translated, provenance=provenance)
    candidate = reconstruction(source, provenance) if provenance else None
    if state.status == "mapped":
        category, action = "mapped", "none"
    elif candidate is not None:
        category, action = "lossless-candidate", "restore-from-receipt"
    elif state.status == "legacy" and not display.notice:
        category, action = "legacy-structural", "review-correspondence"
    else:
        category, action = "unmapped", "review-or-retranslate-after-approval"
    try:
        fingerprint = source_manifest(source)["source_fingerprint"]
    except AlignmentError:
        fingerprint = None
        category, action = "unmapped", "review-or-retranslate-after-approval"
    return {
        "category": category, "reason": state.reason, "action": action,
        "sourceFingerprint": fingerprint,
        "translationHash": digest(translated),
        "candidateHash": digest(candidate) if candidate is not None else None,
        "displayPairs": len(display.pairs), "displayNotice": display.notice,
        "semantics": "not-reviewed", "writes": False, "modelCalls": 0,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--article-id", action="append", type=int, default=[])
    parser.add_argument("--source", default="")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--dry-run", action="store_true", help="兼容显式干跑写法；本命令始终只读")
    parser.add_argument("--plan", type=Path, help="另存不含正文的定向计划，不执行修复")
    args = parser.parse_args(argv)
    settings = Settings.from_env()
    conn = connect(settings)
    try:
        conn.execute("SET TRANSACTION READ ONLY")
        params = []
        where = []
        if args.article_id:
            where.append("a.id = ANY(%s)")
            params.append(list(dict.fromkeys(args.article_id)))
        if args.source:
            where.append("a.source_id = %s")
            params.append(args.source)
        params.append(max(1, args.limit))
        rows = conn.execute(
            "SELECT a.*, t.body_html AS translated_html, t.content_hash AS translation_content_hash,"
            " coalesce(to_jsonb(t)->'provenance', '{}'::jsonb) AS provenance"
            " FROM articles a JOIN article_translations t ON t.article_id = a.id"
            + (" WHERE " + " AND ".join(where) if where else "")
            + " ORDER BY a.id LIMIT %s", params,
        ).fetchall()
        plan = []
        for row in rows:
            source, _raw_hash = _render_original(conn, row, settings.data_dir)
            item = classify(source, row["translated_html"], row["provenance"])
            item.update(articleId=int(row["id"]), source=row["source_id"],
                        expectedArticleHash=row["content_hash"],
                        expectedTranslationContentHash=row["translation_content_hash"])
            plan.append(item)
    finally:
        conn.rollback()
        conn.close()
    payload = {"readOnly": True, "articles": plan, "count": len(plan), "modelCalls": 0}
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if args.plan:
        # 计划只有摘要和状态，不包含私有正文、订阅地址或模型响应。
        args.plan.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
