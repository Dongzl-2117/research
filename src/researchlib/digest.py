"""用 LLM 给每篇文章写摘要、要点和标签，存进 article_digests。

只处理还没有摘要、正文改过（content_hash 对不上）、提示词版本旧了、当时只读了长文开头，
或正文很短而图片文字和上次抽的时候不一致的文章。正文加图片里的字不到 MIN_DIGEST_CHARS 个字的不写：
只有「这是好文章」一句的帖，模型只能写「正文只有一句，无法归纳」；带图的等识图写上字以后再写。
短正文的图片文字刚写上时，会先写进 article_followups，排在缺摘要的积压前面，避免被饿死。
知识星球的读者帖网站上哪儿都不展示，不排队写摘要。
卡片和 JSON 接口优先展示摘要；旧摘要照常显示，等下一次批量重写。失败的记下来，隔几天再试。

只有命令行这一条写入路径：抓取脚本抓完补这一来源的、夜间任务每晚补一批（scripts/nightly.sh，默认 80 篇）、
手动补一批。网页不触发：11-14 以前打开首页、作者、按天、检索、公司、文章页会把卡片里缺摘要的排进后台线程，
公开页面谁都能开、没有每天的上限、也不避开高峰价，提示词版本一改全库就会在没人同意时被补完，删了。
应用启动和部署不会跑。

    research-digest --limit 50
    research-digest --source fangwei-mp --limit 200
    research-digest --ocr-changed --limit 2000 --workers 16 --off-peak   # 一次补一大批，避开 DeepSeek 高峰价

``--workers N`` 几篇同时写，和 research-views 是同一套（``llm_batch.py``）：每篇先占一把 Postgres advisory lock，
同一进程的几个 worker、同时开着的几个 research-digest 都不会写同一篇；每篇印一行用量和用时，
最后印合计；余额不足（402）、鉴权被拒整批停，不把剩下的一篇篇记成失败。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time

from psycopg.types.json import Jsonb

from researchlib import llm_batch
from researchlib.author_voice import display_author, raw_meta_of, site_visible_sql
from researchlib.config import Settings
from researchlib.excerpt import DIGEST_LIMIT, digest_input, original_chunks
from researchlib.failures import clear_failure, cooling_sql
from researchlib.followups import KIND_DIGEST, clear_followup
from researchlib.llm import LLMError, LLMUnavailable, chat, ensure_oauth
from researchlib.ocr import SHORT_BODY_CHARS, append_ocr, article_ocr, ocr_speaker_note
from researchlib.sources import enabled_sources, load_sources
from researchlib.visibility import visible_sql
from researchlib.web.present import format_date
from researchlib.wechat_dialogue import relabel_host

# 一次交给模型的正文上限（字），和问库一次读的量一样。更长的取开头、结尾和各节开头，见 excerpt.py。
BODY_LIMIT = DIGEST_LIMIT
# 提示词改了就把版本加一。旧摘要照常显示，夜间任务或手动跑 research-digest 时按新版重写。
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
# 每篇的锁是 (hashtext(LOCK_NAME), 文章 id) 这把会话级 advisory lock：写完放掉，进程没了自动放掉。
LOCK_NAME = "research-digest"


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


# 短正文、图片文字和摘要对不上。
_OCR_DIGEST_DUE = (
    "(a.ocr_hash <> '' AND d.ocr_hash IS DISTINCT FROM a.ocr_hash"
    f" AND length(coalesce(a.body_text, '')) < {int(SHORT_BODY_CHARS)})"
)
# 其中写摘要时还没有图片里的字的（多半是「没有可读文字，无法提炼」这类空壳，挂在卡片上）：识了图先重写。
_OCR_FIRST_SEEN = (
    "(d.article_id IS NOT NULL AND coalesce(d.ocr_hash, '') = '' AND a.ocr_hash <> ''"
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
# 入队的先写，再是写摘要时还没见过图片里的字的短正文：否则缺摘要的积压（按 limit 截断）会把它们一直留在后面。
# 以前识过字、只是字变了的（全库按新格式重认一次改上千篇）和缺摘要、正文变了的放在一起按发布时间排，
# 不插到新文章前面：新文章的摘要不会因为重认旧图等上几个星期。
PENDING_ORDER = (
    f"{_FOLLOWUP_DIGEST} DESC, {_OCR_FIRST_SEEN} DESC, "
    f"(d.article_id IS NULL OR d.content_hash <> a.content_hash OR {_OCR_DIGEST_DUE}) DESC, "
    "a.published_at DESC NULLS LAST, a.id DESC"
)


def pending_articles(conn, source_ids: list[str], limit: int, *, ocr_changed: bool = False):
    """该写（或重写）摘要的文章，按 PENDING_ORDER 排。

    ocr_changed：只要已经有摘要、写摘要时的图片文字和现在的不一样的短正文（``_OCR_DIGEST_DUE``）。
    全库重认改了上千篇的图片文字，夜间按发布时间和缺摘要的老文章一起排，要几个月才轮到；识图重认以后点名一次补完。
    """
    only = f" AND d.article_id IS NOT NULL AND {_OCR_DIGEST_DUE}" if ocr_changed else ""
    return conn.execute(
        f"""
        SELECT a.id, a.source_id, a.title, a.published_at, a.body_text, a.content_hash
        FROM articles a
        LEFT JOIN article_digests d ON d.article_id = a.id
        WHERE a.source_id = ANY(%s) AND a.duplicate_of IS NULL
          AND {visible_sql("a")} AND {site_visible_sql("a")}
          AND {_DUE} AND {cooling_sql('digest', 'a.id')}{only}
        ORDER BY {PENDING_ORDER}
        LIMIT %s
        """,
        (source_ids, PROMPT_VERSION, limit),
    ).fetchall()


def digest_article(conn, settings: Settings, row, author: str, *, usage: dict | None = None) -> dict:
    """写这一篇的摘要。usage：传一个 dict 进来，这次调用实际用掉的 token 加在里面（见 ``llm.chat``）。"""
    meta = raw_meta_of(conn, row)
    shown_author, _owns = display_author(author, meta)
    when = format_date(row["published_at"], settings.display_timezone)
    header = f"标题：{row['title']}\n作者：{shown_author}\n日期：{when}"
    chunks = original_chunks(conn, row["id"])
    ocr_text, ocr_digest = article_ocr(conn, row["id"])
    # 截图里的绿气泡是发这篇的人：别的来源、星球读者帖都不写成方伟。
    ocr_text = relabel_host(ocr_text, shown_author)
    body = append_ocr(row["body_text"] or "", ocr_text)
    if ocr_text.strip():
        chunks = [*chunks, ("图片里的字", ocr_text)]
        note = ocr_speaker_note(conn, row["id"], ocr_text, shown_author)
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
        usage=usage,
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


def run_batch(
    settings: Settings,
    jobs: list[dict],
    authors: dict[str, str],
    *,
    workers: int = 1,
    deadline: float | None = None,
    off_peak: bool = False,
    now=None,
    sleep=time.sleep,
) -> dict:
    """把 jobs 里的文章写上摘要。jobs：[{"id", "title"}]，正文到写的时候再取。

    几篇同时写、每篇占锁、用量、避开高峰价、整批停都在 ``llm_batch.run``。占到锁以后再查一遍这篇还该不该写：
    别的进程、网页后台刚写完的跳过，不重复问模型。
    """

    def handle(conn, job: dict, usage: dict) -> str | None:
        rows = rows_needing_digest(conn, [int(job["id"])])
        if not rows:
            return None
        row = rows[0]
        data = digest_article(conn, settings, row, authors.get(row["source_id"], ""), usage=usage)
        return f"摘要 {len(data['summary'])} 字，要点 {len(data['points'])} 条"

    return llm_batch.run(
        settings,
        jobs,
        handle,
        kind="digest",
        lock_name=LOCK_NAME,
        verb="写",
        workers=workers,
        deadline=deadline,
        off_peak=off_peak,
        now=now,
        sleep=sleep,
    )


def main(argv: list[str] | None = None) -> int:
    """退出码：0 跑完了（个别文章失败也算，失败的记下来隔几天再试），2 整批停下了：没有可用的 LLM 凭证、
    接口拒绝鉴权或余额不足（402）；3 连着几篇限流、5xx、断网，暂停了，这几篇没记失败，过几分钟再跑同一条。"""
    parser = argparse.ArgumentParser(description="给还没有摘要的文章生成 LLM 摘要")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--source", default="")
    parser.add_argument("--max-minutes", type=float, default=0, help="跑到这么多分钟就停（夜间任务用），0 表示不限")
    parser.add_argument(
        "--ocr-changed",
        action="store_true",
        help="只重写图片里的字变了的短帖（已有摘要，写摘要时的图片文字和现在的不一样）。识图重认以后跑，夜间任务不要加",
    )
    llm_batch.add_arguments(parser, "写")
    args = parser.parse_args(argv)
    llm_batch.check_arguments(args)
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
    workers, off_peak = llm_batch.resolve(settings, args, "写")
    conn = prepare(settings)
    deadline = time.monotonic() + args.max_minutes * 60 if args.max_minutes > 0 else None
    try:
        rows = pending_articles(conn, list(authors), args.limit, ocr_changed=args.ocr_changed)
        # 正文到写的时候再取：几千篇的正文不一直占着内存，取到的也是那一刻的正文。
        jobs = [{"id": int(row["id"]), "title": row["title"]} for row in rows]
        del rows
    finally:
        conn.close()
    report = run_batch(settings, jobs, authors, workers=workers, deadline=deadline, off_peak=off_peak)
    if report["timed_out"]:
        print("到了时间上限，先停在这里", file=sys.stderr)
    print(
        f"待处理 {len(jobs)} 篇，写入 {report['done']} 篇，失败 {report['failed']} 篇"
        + llm_batch.tail(report, "写")
    )
    return llm_batch.exit_code(report, "写")


if __name__ == "__main__":
    sys.exit(main())
