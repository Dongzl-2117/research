"""用 LLM 给每篇文章写摘要、要点和标签，存进 article_digests。

只处理还没有摘要、正文改过（content_hash 对不上）、提示词版本旧了、当时只读了长文开头，
或正文很短而图片文字和上次抽的时候不一致的文章。
卡片和 JSON 接口只读兼容的已存摘要。缺少摘要时不会在读取页面时写库或排队。
摘要写入由显式 CLI 或处理任务执行，失败后按冷却期重试。

    research-digest --limit 50
    research-digest --source fangwei-mp --limit 200
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import threading
import time

from psycopg.types.json import Jsonb

from researchlib.author_voice import DUAN_SOURCE, VOICE_NOTE, display_author, for_model, is_guest, raw_meta_of
from researchlib.config import Settings
from researchlib.eligibility import is_zsxq, model_cache_safe_article, model_cache_safe_sql, project_article
from researchlib.empty_answer import is_suppressed, visible_sql
from researchlib.excerpt import DIGEST_LIMIT, digest_input, original_chunks
from researchlib.failures import clear_failure, cooling_sql, note_failure
from researchlib.llm import LLMError, LLMUnavailable, chat, ensure_oauth, llm_configured
from researchlib.model_inputs import OCR_MODEL_INPUT_VERSION
from researchlib.ocr import SHORT_BODY_CHARS, append_ocr, article_ocr
from researchlib.sources import enabled_sources, load_sources
from researchlib.web.present import format_date
from researchlib.wechat_dialogue import speaker_note

logger = logging.getLogger("researchlib.digest")

# 一次交给模型的正文上限（字），和问库一次读的量一样。更长的取开头、结尾和各节开头，见 excerpt.py。
BODY_LIMIT = DIGEST_LIMIT
# 提示词改了就把版本加一。旧摘要照常显示，被看到或跑 research-digest 时按新版重写。
PROMPT_VERSION = OCR_MODEL_INPUT_VERSION
PROMPT = """读下面这篇投资研究文章，只输出一个 JSON 对象，不要输出别的文字：
{"summary": "不超过 120 字的导读：作者在这篇里的核心判断是什么、凭什么这么判断，不写空话",
 "points": ["3 到 5 条要点，每条一句"],
 "tags": ["3 到 6 个主题标签，例如行业、公司、宏观变量"]}
要点的写法：
- 先写判断，再写作者给出的机制，也就是为什么会这样。机制只写原文给出的理由，不要替作者补。
- 数字只在支撑判断时引用，照抄原文，不要罗列数据。
- 原文谈到了风险、推翻判断的条件或要盯的验证指标，就各写成一条；没谈到就不写，不要硬凑。
只依据原文，原文没有的不要写。开头的 sn、点赞、评论、时间戳不要写进摘要。
不要给出投资建议，不要写该不该买、目标价或仓位。这是导读，不是建议。"""
_JSON = re.compile(r"\{.*\}", re.S)
_MAX_QUEUED = 40
_COOLDOWN_S = 120.0
_lock = threading.Lock()
_order: list[int] = []
_authors: dict[int, str] = {}
_inflight: set[int] = set()
_settings_holder: list[Settings] = []
_running = False
_cooldown_until = 0.0


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


def needs_rewrite(digest: dict | None) -> bool:
    """页面上已有的摘要要不要在后台重写：提示词版本旧了，或者当时只读了长文的开头。"""
    if digest is None:
        return True
    return (digest.get("prompt_version") or 1) < PROMPT_VERSION or bool(digest.get("head_only"))


def digest_due(article, digest: dict | None) -> bool:
    """打开这一篇时要不要重写摘要。不扫全库。

    正文很短、图片文字和上次抽的时候不一致，也重写这一篇：截图里的字要进导读。
    """
    if needs_rewrite(digest):
        return True
    if article is None:
        return False
    ocr_hash = article["ocr_hash"] or ""
    if not ocr_hash or len(article["body_text"] or "") >= SHORT_BODY_CHARS:
        return False
    return ((digest or {}).get("ocr_hash") or "") != ocr_hash


_STALE = (
    "(d.article_id IS NULL OR d.content_hash <> a.content_hash OR d.prompt_version < %s OR d.head_only"
    " OR (a.ocr_hash <> '' AND d.ocr_hash IS DISTINCT FROM a.ocr_hash"
    f" AND length(coalesce(a.body_text, '')) < {int(SHORT_BODY_CHARS)}))"
)


def pending_articles(conn, source_ids: list[str], limit: int):
    return conn.execute(
        f"""
        SELECT a.id, a.source_id, a.title, a.published_at, a.body_md, a.body_text, a.content_hash, a.raw_meta
        FROM articles a
        LEFT JOIN article_digests d ON d.article_id = a.id
        WHERE a.source_id = ANY(%s) AND a.duplicate_of IS NULL
          AND {visible_sql("a")}
          AND {model_cache_safe_sql("a")}
          AND {_STALE} AND {cooling_sql('digest', 'a.id')}
        ORDER BY (d.article_id IS NULL OR d.content_hash <> a.content_hash) DESC,
                 a.published_at DESC NULLS LAST, a.id DESC
        LIMIT %s
        """,
        (source_ids, PROMPT_VERSION, limit),
    ).fetchall()


def digest_article(conn, settings: Settings, row, author: str) -> dict:
    if not row.get("source_id"):
        return {}
    meta = raw_meta_of(conn, row)
    if is_suppressed(meta):
        return {}
    if not model_cache_safe_article({**dict(row), "raw_meta": meta}):
        return {}
    row = project_article({**dict(row), "raw_meta": meta})
    if row is None:
        return {}
    meta = row["raw_meta"]
    shown_author, _owns = display_author(author, meta)
    when = format_date(row["published_at"], settings.display_timezone)
    header = f"标题：{row['title']}\n作者：{shown_author}\n日期：{when}"
    if is_zsxq(row):
        from researchlib.chunks import make_chunks

        chunks = make_chunks(row["title"], row["body_md"])
    else:
        chunks = original_chunks(conn, row["id"])
    source_id = row["source_id"] if "source_id" in row.keys() else ""
    # 星球读者帖是发帖人自己的正文。for_model 会把它收成空的「作者本人」，摘要不能跟着写成段永平没说话。
    if is_zsxq(row):
        body = row["body_text"] or ""
        restricted = False
        if row.get("owner_reply_context"):
            header += "\n（观点只归属于已验证星主回复；标注的网友问题仅作上下文，不归入星主观点。）"
    elif is_guest(meta):
        body = row["body_text"] or ""
        restricted = False
    else:
        body, voice_chunks, restricted = for_model(
            row["body_text"] or "", chunks, meta, source_id=source_id
        )
        if restricted:
            header += VOICE_NOTE
            chunks = voice_chunks
    ocr_text, ocr_digest = ("", "") if row.get("owner_reply_context") else article_ocr(conn, row["id"])
    voice_body = body
    duan_silent = source_id == DUAN_SOURCE and restricted and not voice_body.strip()
    if not duan_silent:
        body = append_ocr(voice_body, ocr_text)
        if ocr_text.strip():
            chunks = [*chunks, ("图片里的字", ocr_text)]
            note = speaker_note(ocr_text)
            header += (
                "\n（「图片里的字」是截图里识别出来的，和正文一样要读，不要只概括正文里那一句。"
                + (note if note else "")
                + "）"
            )
    reduced = False
    if duan_silent:
        # 只有段永平这篇里他本人没有字。图片里的字也不写成他的导读。
        data = {"summary": "（没有段永平本人的话）", "points": [], "tags": []}
        model = ""
    else:
        if len(body) > BODY_LIMIT:
            body, reduced = digest_input(body, chunks, BODY_LIMIT)
        if reduced:
            header += "\n（长文：下面是开头、结尾和各节开头，中间略去的部分用省略号标出）"
        text, model = chat(
            settings,
            [
                {"role": "system", "content": PROMPT},
                {"role": "user", "content": header + "\n\n正文：\n" + body},
            ],
            temperature=0.1,
        )
        data = parse_digest(text)
    conn.execute(
        """
        INSERT INTO article_digests (
          article_id, content_hash, model, summary, points, tags, prompt_version, head_only, ocr_hash
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, false, %s)
        ON CONFLICT (article_id) DO UPDATE SET
          content_hash = EXCLUDED.content_hash,
          model = EXCLUDED.model,
          summary = EXCLUDED.summary,
          points = EXCLUDED.points,
          tags = EXCLUDED.tags,
          prompt_version = EXCLUDED.prompt_version,
          head_only = false,
          ocr_hash = EXCLUDED.ocr_hash,
          created_at = now()
        """,
        (
            row["id"],
            row["content_hash"],
            model,
            data["summary"],
            Jsonb(data["points"]),
            Jsonb(data["tags"]),
            PROMPT_VERSION,
            ocr_digest,
        ),
    )
    clear_failure(conn, "digest", row["id"])
    conn.commit()
    return data


def rows_needing_digest(conn, article_ids: list[int]):
    if not article_ids:
        return []
    return conn.execute(
        f"""
        SELECT a.id, a.source_id, a.title, a.published_at, a.body_md, a.body_text, a.content_hash, a.raw_meta
        FROM articles a
        LEFT JOIN article_digests d ON d.article_id = a.id
        WHERE a.id = ANY(%s) AND {model_cache_safe_sql("a")} AND {visible_sql("a")}
          AND {_STALE} AND {cooling_sql('digest', 'a.id')}
        ORDER BY a.published_at DESC NULLS LAST, a.id DESC
        """,
        (article_ids, PROMPT_VERSION),
    ).fetchall()


def ensure_digests(conn, settings: Settings, article_ids: list[int], authors: dict[int, str]) -> int:
    """给这几篇里缺摘要的文章现写。失败时把异常抛回调用方。"""
    done = 0
    for row in rows_needing_digest(conn, article_ids):
        digest_article(conn, settings, row, authors.get(row["id"], ""))
        done += 1
    return done


def schedule_digests(settings: Settings, jobs: list[tuple[int, str]], *, spawn: bool | None = None) -> int:
    """把当前卡片里缺摘要的文章排进后台。不扫全库，也不挡住这次页面。

    返回新入队的篇数。测试进程里默认直接返回，避免连上 Grok 或另开数据库连接。
    """
    global _running
    if not jobs or not llm_configured(settings):
        return 0
    if spawn is None:
        spawn = "PYTEST_CURRENT_TEST" not in os.environ
    if not spawn:
        return 0
    start = False
    added = 0
    with _lock:
        if time.monotonic() < _cooldown_until:
            return 0
        _settings_holder[:] = [settings]
        for article_id, author in jobs:
            if article_id in _authors or article_id in _inflight or len(_order) >= _MAX_QUEUED:
                continue
            _authors[article_id] = author or ""
            _order.append(article_id)
            added += 1
        if added and not _running:
            _running = True
            start = True
    if start:
        threading.Thread(target=_digest_worker, name="article-digests", daemon=True).start()
    return added


def _digest_worker() -> None:
    global _running, _cooldown_until
    from researchlib.db import connect, ensure_search_session

    conn = None
    try:
        while True:
            with _lock:
                if not _order or not _settings_holder or time.monotonic() < _cooldown_until:
                    _running = False
                    return
                settings = _settings_holder[0]
                article_id = _order.pop(0)
                author = _authors.pop(article_id, "")
                _inflight.add(article_id)
            try:
                if conn is None:
                    conn = connect(settings)
                    ensure_search_session(conn)
                ensure_digests(conn, settings, [article_id], {article_id: author})
            except LLMUnavailable as exc:
                logger.warning("摘要暂停：%s", exc)
                with _lock:
                    _cooldown_until = time.monotonic() + _COOLDOWN_S
                    _order.clear()
                    _authors.clear()
                    _inflight.discard(article_id)
                    _running = False
                return
            except LLMError as exc:
                logger.warning("摘要失败 %s：%s", article_id, exc)
                if conn is not None:
                    conn.rollback()
                    note_failure(conn, "digest", article_id, exc)
            except Exception:
                logger.exception("摘要失败 %s", article_id)
                if conn is not None:
                    conn.rollback()
                else:
                    with _lock:
                        _inflight.discard(article_id)
                        _running = False
                    return
            finally:
                with _lock:
                    _inflight.discard(article_id)
    finally:
        if conn is not None:
            conn.close()


def main(argv: list[str] | None = None) -> int:
    """退出码：0 跑完了（个别文章失败也算，失败的记下来隔几天再试），2 没有可用的 LLM 凭证。"""
    parser = argparse.ArgumentParser(description="给还没有摘要的文章生成 LLM 摘要")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--source", default="")
    parser.add_argument("--max-minutes", type=float, default=0, help="跑到这么多分钟就停（夜间任务用），0 表示不限")
    args = parser.parse_args(argv)
    from researchlib.db import prepare

    settings = Settings.from_env()
    try:
        ensure_oauth(settings)
    except LLMUnavailable as exc:
        print(exc, file=sys.stderr)
        return 2
    sources = enabled_sources(load_sources(settings.sources_path))
    if args.source:
        sources = [source for source in sources if source.id == args.source]
        if not sources:
            raise SystemExit(f"{args.source} 不是已启用来源")
    authors = {source.id: source.author for source in sources}
    conn = prepare(settings)
    done = failed = 0
    deadline = time.monotonic() + args.max_minutes * 60 if args.max_minutes > 0 else None
    try:
        rows = pending_articles(conn, list(authors), args.limit)
        for row in rows:
            if deadline is not None and time.monotonic() > deadline:
                print("到了时间上限，先停在这里", file=sys.stderr)
                break
            try:
                digest_article(conn, settings, row, authors[row["source_id"]])
                done += 1
            except LLMUnavailable as exc:
                print(exc, file=sys.stderr)
                return 2
            except LLMError as exc:
                conn.rollback()
                note_failure(conn, "digest", row["id"], exc)
                failed += 1
                print(f"{row['id']} {row['title']}: {exc}", file=sys.stderr)
    finally:
        conn.close()
    print(f"待处理 {len(rows)} 篇，写入 {done} 篇，失败 {failed} 篇")
    return 0


if __name__ == "__main__":
    sys.exit(main())
