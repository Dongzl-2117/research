"""用 LLM 给每篇文章写摘要、要点和标签，存进 article_digests。

只处理还没有摘要、正文改过（content_hash 对不上）、提示词版本旧了、当时只读了长文开头，
或正文很短而图片文字和上次抽的时候不一致的文章。正文加图片里的字不到 MIN_DIGEST_CHARS 个字的不写：
只有「这是好文章」一句的帖，模型只能写「正文只有一句，无法归纳」；带图的等识图写上字以后再写。
短正文的图片文字刚写上时，会先写进 article_followups，排在缺摘要的积压前面，避免被饿死。
知识星球的读者帖网站上哪儿都不展示，不排队写摘要。
卡片和 JSON 接口优先展示摘要；旧摘要照常显示，同时排进后台重写。失败的记下来，隔几天再试。

两条写入路径，同一张表、同一套提示、同一份 Grok 登录：

- 打开按天、作者、公司或检索时，当前卡片里缺摘要的文章在后台排队，不挡住页面，也不扫全库。
- 手动 CLI 仍可补一批。应用启动和部署不会跑全库。

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

from researchlib.author_voice import display_author, raw_meta_of, site_visible_sql
from researchlib.config import Settings
from researchlib.excerpt import DIGEST_LIMIT, digest_input, original_chunks
from researchlib.failures import clear_failure, cooling_sql, note_failure
from researchlib.followups import KIND_DIGEST, clear_followup
from researchlib.llm import LLMError, LLMUnavailable, chat, ensure_oauth, llm_configured
from researchlib.ocr import SHORT_BODY_CHARS, append_ocr, article_ocr
from researchlib.sources import enabled_sources, load_sources
from researchlib.visibility import visible_sql
from researchlib.web.present import format_date
from researchlib.wechat_dialogue import speaker_note

logger = logging.getLogger("researchlib.digest")

# 一次交给模型的正文上限（字），和问库一次读的量一样。更长的取开头、结尾和各节开头，见 excerpt.py。
BODY_LIMIT = DIGEST_LIMIT
# 提示词改了就把版本加一。旧摘要照常显示，被看到或跑 research-digest 时按新版重写。
PROMPT_VERSION = 2
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


# 正文加图片里的字，汉字、字母、数字不到这么多个就不写摘要。星球上只有「这是好文章」「下图」一句、
# 字都在图里的帖，抓下来时还没识图，模型只能写「正文只有一句，无法归纳」，挂在卡片上还不如原文。
# 识图写上字以后够数了再写（识图会把短正文排进摘要的队）。
MIN_DIGEST_CHARS = 20


def enough_text_sql(alias: str = "a") -> str:
    """这篇的正文加图片里的字够不够写摘要。各取前 400 字来数，长文不用整篇过正则。"""
    return (
        f"length(regexp_replace(left(coalesce({alias}.body_text, ''), 400)"
        f" || left(coalesce({alias}.ocr_text, ''), 400), '[^0-9A-Za-z一-鿿]', '', 'g'))"
        f" >= {MIN_DIGEST_CHARS}"
    )


# 短正文、图片文字和摘要对不上。单独拿出来，是为了排在「还没有摘要」前面。
_OCR_DIGEST_DUE = (
    "(a.ocr_hash <> '' AND d.ocr_hash IS DISTINCT FROM a.ocr_hash"
    f" AND length(coalesce(a.body_text, '')) < {int(SHORT_BODY_CHARS)})"
)
_FOLLOWUP_DIGEST = (
    "EXISTS (SELECT 1 FROM article_followups f"
    f" WHERE f.article_id = a.id AND f.kind = '{KIND_DIGEST}')"
)
_STALE = (
    "(d.article_id IS NULL OR d.content_hash <> a.content_hash OR d.prompt_version < %s OR d.head_only"
    f" OR {_OCR_DIGEST_DUE})"
)
# 入队的也算到期：哈希已经对齐、但标题曾被漏判时，仍要重写这一次。字太少的都不算到期。
_DUE = f"(({_STALE} OR {_FOLLOWUP_DIGEST}) AND {enough_text_sql('a')})"
# 刚识完图的短正文先写。否则缺摘要的积压（按 limit 截断）会把它们一直留在后面。
PENDING_ORDER = (
    f"{_FOLLOWUP_DIGEST} DESC, {_OCR_DIGEST_DUE} DESC, "
    "(d.article_id IS NULL OR d.content_hash <> a.content_hash) DESC, "
    "a.published_at DESC NULLS LAST, a.id DESC"
)


def pending_articles(conn, source_ids: list[str], limit: int):
    return conn.execute(
        f"""
        SELECT a.id, a.source_id, a.title, a.published_at, a.body_text, a.content_hash
        FROM articles a
        LEFT JOIN article_digests d ON d.article_id = a.id
        WHERE a.source_id = ANY(%s) AND a.duplicate_of IS NULL
          AND {visible_sql("a")} AND {site_visible_sql("a")}
          AND {_DUE} AND {cooling_sql('digest', 'a.id')}
        ORDER BY {PENDING_ORDER}
        LIMIT %s
        """,
        (source_ids, PROMPT_VERSION, limit),
    ).fetchall()


def digest_article(conn, settings: Settings, row, author: str) -> dict:
    meta = raw_meta_of(conn, row)
    shown_author, _owns = display_author(author, meta)
    when = format_date(row["published_at"], settings.display_timezone)
    header = f"标题：{row['title']}\n作者：{shown_author}\n日期：{when}"
    chunks = original_chunks(conn, row["id"])
    ocr_text, ocr_digest = article_ocr(conn, row["id"])
    body = append_ocr(row["body_text"] or "", ocr_text)
    if ocr_text.strip():
        chunks = [*chunks, ("图片里的字", ocr_text)]
        note = speaker_note(ocr_text)
        header += (
            "\n（「图片里的字」是截图里识别出来的，和正文一样要读，不要只概括正文里那一句。"
            + (note if note else "")
            + "）"
        )
    reduced = False
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
    clear_followup(conn, int(row["id"]), KIND_DIGEST)
    conn.commit()
    return data


def rows_needing_digest(conn, article_ids: list[int]):
    if not article_ids:
        return []
    return conn.execute(
        f"""
        SELECT a.id, a.source_id, a.title, a.published_at, a.body_text, a.content_hash
        FROM articles a
        LEFT JOIN article_digests d ON d.article_id = a.id
        WHERE a.id = ANY(%s) AND {_DUE} AND {cooling_sql('digest', 'a.id')}
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
