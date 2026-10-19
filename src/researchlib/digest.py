"""用 LLM 给每篇文章写摘要、要点和标签，存进 article_digests。

只处理还没有摘要、或正文改过（content_hash 对不上）的文章。卡片和 JSON 接口优先展示摘要。

这是手动 CLI，应用启动和部署都不会调用它，也没有「全库自动跑」的开关。

    research-digest --limit 50
    research-digest --source fangwei-mp --limit 200
"""

from __future__ import annotations

import argparse
import json
import re
import sys

from psycopg.types.json import Jsonb

from researchlib.config import Settings
from researchlib.llm import LLMError, LLMUnavailable, chat
from researchlib.sources import enabled_sources, load_sources
from researchlib.web.present import format_date

BODY_LIMIT = 12000
PROMPT = """读下面这篇投资研究文章，只输出一个 JSON 对象，不要输出别的文字：
{"summary": "不超过 120 字的摘要，写作者的核心判断，不写空话",
 "points": ["3 到 5 条要点，每条一句，带关键数字"],
 "tags": ["3 到 6 个主题标签，例如 行业、公司、宏观变量"]}
只依据原文，原文没有的不要写。"""
_JSON = re.compile(r"\{.*\}", re.S)


def parse_digest(text: str) -> dict:
    match = _JSON.search(text or "")
    if not match:
        raise LLMError("摘要返回里没有 JSON")
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise LLMError(f"摘要 JSON 解析失败：{exc}") from exc
    summary = str(data.get("summary") or "").strip()
    if not summary:
        raise LLMError("摘要为空")
    points = [str(item).strip() for item in (data.get("points") or []) if str(item).strip()][:6]
    tags = [str(item).strip() for item in (data.get("tags") or []) if str(item).strip()][:8]
    return {"summary": summary[:400], "points": points, "tags": tags}


def pending_articles(conn, source_ids: list[str], limit: int):
    return conn.execute(
        """
        SELECT a.id, a.source_id, a.title, a.published_at, a.body_text, a.content_hash
        FROM articles a
        LEFT JOIN article_digests d ON d.article_id = a.id
        WHERE a.source_id = ANY(%s)
          AND (d.article_id IS NULL OR d.content_hash <> a.content_hash)
        ORDER BY a.published_at DESC NULLS LAST, a.id DESC
        LIMIT %s
        """,
        (source_ids, limit),
    ).fetchall()


def digest_article(conn, settings: Settings, row, author: str) -> dict:
    when = format_date(row["published_at"], settings.display_timezone)
    header = f"标题：{row['title']}\n作者：{author}\n日期：{when}"
    text, model = chat(
        settings,
        [
            {"role": "system", "content": PROMPT},
            {"role": "user", "content": header + "\n\n正文：\n" + (row["body_text"] or "")[:BODY_LIMIT]},
        ],
        max_tokens=800,
        temperature=0.1,
    )
    data = parse_digest(text)
    conn.execute(
        """
        INSERT INTO article_digests (article_id, content_hash, model, summary, points, tags)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (article_id) DO UPDATE SET
          content_hash = EXCLUDED.content_hash,
          model = EXCLUDED.model,
          summary = EXCLUDED.summary,
          points = EXCLUDED.points,
          tags = EXCLUDED.tags,
          created_at = now()
        """,
        (row["id"], row["content_hash"], model, data["summary"], Jsonb(data["points"]), Jsonb(data["tags"])),
    )
    conn.commit()
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="给还没有摘要的文章生成 LLM 摘要")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--source", default="")
    args = parser.parse_args(argv)
    from researchlib.db import prepare

    settings = Settings.from_env()
    sources = enabled_sources(load_sources(settings.sources_path))
    if args.source:
        sources = [source for source in sources if source.id == args.source]
        if not sources:
            raise SystemExit(f"{args.source} 不是已启用来源")
    authors = {source.id: source.author for source in sources}
    conn = prepare(settings)
    done = failed = 0
    try:
        rows = pending_articles(conn, list(authors), args.limit)
        for row in rows:
            try:
                digest_article(conn, settings, row, authors[row["source_id"]])
                done += 1
            except LLMUnavailable as exc:
                print(exc, file=sys.stderr)
                return 2
            except LLMError as exc:
                conn.rollback()
                failed += 1
                print(f"{row['id']} {row['title']}: {exc}", file=sys.stderr)
    finally:
        conn.close()
    print(f"待处理 {len(rows)} 篇，写入 {done} 篇，失败 {failed} 篇")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
