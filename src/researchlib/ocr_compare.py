"""新旧识图对比：挑一批按旧说明认过的图，用现在的说明重认，不写库，原图和新旧结果打成一个包。

11-08 Zhilin 同意按新格式重认约 1.1 万张旧图，随即问「新办法真的更好吗」。线上只有 6 张新格式的样本，
只说明格式能交、读得出来，没和旧结果逐张比过。这个包给人逐张看图比：说话人、引用、截断、漏字错字。

    docker compose exec -T app research-ocr --compare 30

样本分三份：约三分之一是同一篇里连着截的几张聊天（整篇一起认，看拼接和截断），约三分之一是单张对话或帖子截图，
其余从全部有字的旧图里随机抽。「像对话」只看旧结果里有没有两行以上「某某：」开头，只用来挑样本，不改任何结果。
按内容哈希的 md5 排序，同一个库每次抽到同一批。每张图调一次模型（读不出来再要一次），按现在的价目约 0.007 美元。
"""

from __future__ import annotations

import hashlib
import json
import tarfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from researchlib.ocr import (
    MAX_VISION_BYTES,
    VISION_PROMPT_VERSION,
    Recognition,
    _fatal_ocr,
    _pending_where,
    article_image_texts,
    article_owners,
    compose_ocr_text,
    image_mime,
    prepare_vision_image,
)
from researchlib.ocr_units import ImageText

COMPARE_DIR = "ocr-compare"
# 旧结果里「某某：」开头的行。只用来挑像对话的样本。
DIALOGUE_LINE = r"(^|\n)[^：:\n]{1,24}[：:]"
# 连着截的聊天：一篇最多取这么多张，免得一篇长群聊占满样本。
SHOTS_PER_ARTICLE = 4
_EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/bmp": ".bmp", "image/gif": ".gif"}


def _md5(value: object) -> str:
    return hashlib.md5(str(value).encode()).hexdigest()


def old_images(conn) -> list[dict]:
    """全库重认要认、以前认出过字的图（和 ``pending_images(redo=True)`` 同一个筛选），带旧结果。"""
    where, params = _pending_where(redo=True, article_ids=None)
    return conn.execute(
        f"""
        WITH candidates AS (
            SELECT i.content_hash, min(i.storage_path) AS storage_path,
                   array_agg(DISTINCT i.article_id ORDER BY i.article_id) AS article_ids
            FROM article_images i
            JOIN articles a ON a.id = i.article_id
            WHERE {" AND ".join(where)}
            GROUP BY i.content_hash
        )
        SELECT c.content_hash, c.storage_path, c.article_ids, t.text, t.engine, t.created_at,
               (SELECT count(*) FROM regexp_matches(t.text, %s, 'g')) AS dialogue_lines
        FROM candidates c
        JOIN image_texts t ON t.content_hash = c.content_hash
        WHERE btrim(t.text) <> ''
        """,
        (*params, DIALOGUE_LINE),
    ).fetchall()


def pick_sample(conn, size: int) -> list[dict]:
    """挑 size 张左右。每张带上 stratum：shots（连着截的聊天）、dialogue（单张对话或帖子）、random。"""
    rows = old_images(conn)
    by_hash = {row["content_hash"]: row for row in rows}
    dialogue = [row for row in rows if int(row["dialogue_lines"] or 0) >= 2]
    picked: list[dict] = []
    seen: set[str] = set()

    def take(row: dict, stratum: str, article_id: int | None = None) -> None:
        if row["content_hash"] not in seen:
            seen.add(row["content_hash"])
            picked.append({**row, "stratum": stratum, "article_id": article_id or row["article_ids"][0]})

    # 同一篇里两张以上像对话的图：整篇按正文里的先后一起认。
    per_article: dict[int, int] = {}
    for row in dialogue:
        for article_id in row["article_ids"]:
            per_article[article_id] = per_article.get(article_id, 0) + 1
    shot_quota = size // 3
    for article_id in sorted((a for a, n in per_article.items() if n >= 2), key=_md5):
        if len(picked) >= shot_quota:
            break
        ordered = conn.execute(
            "SELECT content_hash FROM article_images WHERE article_id = %s ORDER BY id", (article_id,)
        ).fetchall()
        shots = [by_hash[r["content_hash"]] for r in ordered if r["content_hash"] in by_hash]
        for row in shots[:SHOTS_PER_ARTICLE]:
            take(row, "shots", article_id)
    for row in sorted(dialogue, key=lambda r: _md5(r["content_hash"])):
        if len(picked) >= 2 * size // 3:
            break
        take(row, "dialogue")
    for row in sorted(rows, key=lambda r: _md5("random" + r["content_hash"])):
        if len(picked) >= size:
            break
        take(row, "random")
    return picked


def _article(conn, article_id: int) -> dict:
    row = conn.execute(
        """
        SELECT a.id, a.source_id, a.title, a.published_at, a.ocr_text, s.author
        FROM articles a JOIN sources s ON s.id = a.source_id WHERE a.id = %s
        """,
        (article_id,),
    ).fetchone()
    return dict(row) if row else {"id": article_id}


def _recognize(recognize, path: Path) -> tuple[Recognition | None, Exception | None, float]:
    started = time.monotonic()
    try:
        found = recognize(path)
    except Exception as exc:  # 单张失败照记；鉴权失败由调用方整批停
        return None, exc, time.monotonic() - started
    result = found if isinstance(found, Recognition) else Recognition(found or "")
    return result, None, time.monotonic() - started


def run_compare(conn, data_dir: Path, recognize, size: int, *, workers: int = 3) -> dict:
    """认样本、写包。返回报告（含包的路径）。不写 image_texts，也不动文章。"""
    data_dir = Path(data_dir)
    sample = pick_sample(conn, size)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    out = data_dir / COMPARE_DIR / stamp
    (out / "images").mkdir(parents=True, exist_ok=True)

    jobs = []
    for row in sample:
        path = data_dir / "images" / row["storage_path"]
        if not path.is_file() or path.stat().st_size > MAX_VISION_BYTES:
            continue
        jobs.append((row, path))
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(_recognize, recognize, path) for _row, path in jobs]
        outcomes = []
        for future in futures:
            outcomes.append(future.result())
            problem = outcomes[-1][1]
            if problem is not None and _fatal_ocr(problem):
                for rest in futures:
                    rest.cancel()
                raise problem

    articles: dict[int, dict] = {}
    owners: dict[int, tuple[str, ...]] = {}
    entries = []
    new_units: dict[str, ImageText] = {}
    tokens: dict[str, int] = {}
    for index, ((row, path), (result, problem, seconds)) in enumerate(zip(jobs, outcomes, strict=True), 1):
        article_id = int(row["article_id"])
        if problem is not None:
            error = str(problem)[:300] or type(problem).__name__
        else:
            error = result.error if result else ""
        if article_id not in articles:
            articles[article_id] = _article(conn, article_id)
            owners[article_id] = article_owners(conn, article_id)
        payload, mime = prepare_vision_image(path.read_bytes(), image_mime(path))
        name = f"{row['content_hash'][:12]}{_EXT.get(mime, path.suffix.lower())}"
        (out / "images" / name).write_bytes(payload)
        old = ImageText(row["content_hash"], row["text"] or "", None)
        entry = {
            "n": index,
            "stratum": row["stratum"],
            "content_hash": row["content_hash"],
            "image": f"images/{name}",
            "article_id": article_id,
            "article_ids": [int(value) for value in row["article_ids"]],
            "old": {
                "engine": row["engine"],
                "created_at": row["created_at"].isoformat() if row["created_at"] else "",
                "raw": row["text"] or "",
                "presented": compose_ocr_text([old]),
            },
            "new": {"error": error, "seconds": round(seconds, 1)},
        }
        if result is not None:
            for key, value in (result.usage or {}).items():
                tokens[key] = tokens.get(key, 0) + int(value)
            entry["new"].update(
                {
                    "kind": (result.units or {}).get("kind", ""),
                    "units": result.units,
                    "text": result.text,
                    "usage": result.usage,
                    "presented": compose_ocr_text(
                        [ImageText(row["content_hash"], result.text, result.units)], owners[article_id]
                    )
                    if result.text
                    else "",
                }
            )
            if not error and result.text:
                new_units[row["content_hash"]] = ImageText(row["content_hash"], result.text, result.units)
        entries.append(entry)

    # 连着截的那几篇：整篇的旁路正文（摘要、观点、检索读的）新旧各一份，看拼接和截断。
    whole = []
    for article_id in sorted({e["article_id"] for e in entries if e["stratum"] == "shots"}):
        current = article_image_texts(conn, article_id)
        replaced = [new_units.get(image.digest, image) for image in current]
        whole.append(
            {
                "article_id": article_id,
                "old": articles[article_id].get("ocr_text") or "",
                "new": compose_ocr_text(replaced, owners[article_id]),
            }
        )

    report = {
        "created_at": stamp,
        "prompt_version": VISION_PROMPT_VERSION,
        "images": len(entries),
        "structured": sum(1 for e in entries if e["new"].get("units") is not None and not e["new"]["error"]),
        "plain": sum(1 for e in entries if e["new"].get("text") and e["new"].get("units") is None),
        "failed": sum(1 for e in entries if e["new"]["error"]),
        "tokens": tokens,
        "seconds": round(sum(e["new"]["seconds"] for e in entries), 1),
        "articles": {
            str(key): {
                "source_id": value.get("source_id", ""),
                "author": value.get("author", ""),
                "title": value.get("title", ""),
                "published_at": value["published_at"].isoformat() if value.get("published_at") else "",
            }
            for key, value in articles.items()
        },
        "entries": entries,
        "whole_articles": whole,
    }
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "report.md").write_text(render_markdown(report), encoding="utf-8")
    bundle = out.parent / f"{stamp}.tar.gz"
    with tarfile.open(bundle, "w:gz") as tar:
        tar.add(out, arcname=f"ocr-compare-{stamp}")
    report["bundle"] = str(bundle)
    return report


def _block(text: str) -> str:
    return "```text\n" + (text.strip() or "（空）") + "\n```\n"


def render_markdown(report: dict) -> str:
    """给人看的那一份：每张图一节，原图、旧结果（存的原文和整理后的）、新结果（排好的和结构）。"""
    lines = [
        f"# 新旧识图对比 {report['created_at']}",
        "",
        f"{report['images']} 张图：按结构交回 {report['structured']} 张，自由文字 {report['plain']} 张，"
        f"失败或读不出来 {report['failed']} 张。用量 {json.dumps(report['tokens'], ensure_ascii=False)}，"
        f"合计 {report['seconds']} 秒（几张一起认）。",
        "",
    ]
    for entry in report["entries"]:
        article = report["articles"].get(str(entry["article_id"]), {})
        lines += [
            f"## {entry['n']}. {entry['stratum']} · 文章 {entry['article_id']}「{article.get('title', '')}」"
            f"（{article.get('source_id', '')}，{article.get('published_at', '')[:10]}）",
            "",
            f"![]({entry['image']})",
            "",
            f"旧（{entry['old']['engine']}，{entry['old']['created_at'][:10]} 认的），整理后：",
            _block(entry["old"]["presented"]),
            "旧，存的原文：",
            _block(entry["old"]["raw"]),
        ]
        new = entry["new"]
        if new.get("error"):
            lines += [f"新：失败（{new['error']}）", ""]
            continue
        lines += [
            f"新（kind={new.get('kind', '')}，{new.get('seconds')} 秒，用量 "
            f"{json.dumps(new.get('usage') or {}, ensure_ascii=False)}），排好的：",
            _block(new.get("presented", "")),
            "新，结构（一块一行）：",
            "```json\n"
            + "\n".join(json.dumps(block, ensure_ascii=False) for block in (new.get("units") or {}).get("blocks", []))
            + "\n```\n",
        ]
    for item in report["whole_articles"]:
        lines += [
            f"## 整篇 · 文章 {item['article_id']}（连着截的几张拼起来）",
            "",
            "旧：",
            _block(item["old"]),
            "新：",
            _block(item["new"]),
        ]
    return "\n".join(lines) + "\n"
