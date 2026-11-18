"""给检索块打「这段讲没讲机制和原因」的分（0、1、2），存进 chunk_mechanism，搜索时只查表、不调模型。

为什么要它（2026-11-17）：检索的字面分和意思分都量不出一段话讲没讲「为什么」。搜「Costco 商业模式」，
排在前面的有一段只说某家公司在学 Costco、不说学了什么为什么学，还有一段是泛泛的投资格言。
Zhilin 的标准是「不讲业务的都往后，只有真正讲机制的（少 SKU、低运营成本、仓储模式、大单品）才保留」。
不花钱的办法试过：拿一句「讲业务机制」的描述比向量相似度，几乎等于瞎猜；拿 7 道题 338 段人工判分训练
向量上的分类器，留出题准度（AUC）0.69，能用但不够。他定了一次性用模型给全库的块打分，搜索时边际成本为零，
固定成本高一点可以接受。

分数只看这一段本身，不知道读者会搜什么：「讲的是不是你问的那家公司」由搜索时的
``retrieval.focus_share_factor`` 管（问到的公司不是这段主角就往后排）。两样合起来才是「讲 Costco 的机制」。

用法（花模型钱，先报数、等 Zhilin 同意再跑；网页、启动、上线都不会跑它）：

    research-mechanism --limit 0                       # 只报还要打几篇、几块、多少字，不调模型
    docker compose exec -T app research-mechanism --calibrate - --effort low < tools/quality_eval/mechanism_owner.json
                                                       # 打人工判过的段、和人工比，不写库
    research-mechanism --limit 2000 --workers 16 --off-peak

一篇一个任务（每篇占一把锁），这篇里要打的块按 ``BATCH_CHARS`` 分几次问。块改过（标题加正文的 md5 变了）、
提示词版本加了一，就重打这一块。英文文章有中文译文块的，英文原文块不打（检索按中文问，译文块和原文块讲的是
同一段话，两样都打费用翻倍）；搜索时没有分数的块按不打折处理。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import replace

from researchlib import llm_batch
from researchlib.author_voice import site_visible_sql
from researchlib.config import Settings
from researchlib.failures import clear_failure, cooling_sql
from researchlib.llm import LLMError, LLMUnavailable, chat, ensure_oauth
from researchlib.sources import enabled_sources, load_sources
from researchlib.visibility import visible_sql

# 提示词或打分标准改了就加一：版本旧的块下次重打。
PROMPT_VERSION = 2
# 一次请求最多送这么多字的块（不算标题）。块平均五六百字，约 20 块一问。
BATCH_CHARS = 12000
# 一块送给模型的上限（字）。检索块一般不超过两千字，超长的取开头。
CHUNK_LIMIT = 3000
LOCK_NAME = "research-mechanism"
FAILURE_KIND = "mechanism"
EFFORTS = ("low", "high", "max")

PROMPT = """下面是一篇投资研究文章里的若干段落，每段前面有编号。逐段判断：读完这一段，能不能明白一门生意是怎么回事，
也就是它靠什么赚钱、为什么能赢或会输、护城河在哪。写得简短、写成列表或对比都可以，关键是讲清了道理。

2：主体在讲清一门生意的道理，读完能复述出它为什么赚钱、为什么赢或输。
1：讲到了这样的道理，但只有一两句，其余是别的。
0：没把道理讲出来。只是提到、谁学谁、语录、点评、答问、预测、新闻里只有结论、态度或数字的，都是 0。
拿不准时判低一档。

只看段落本身写了什么，不判断对错，不替作者补理由。
只输出 JSON：{"scores": [{"id": 段落编号, "score": 0 或 1 或 2}]}，每个编号都要有分数。"""

_JSON = re.compile(r"\{.*\}", re.S)


def chunk_hash_sql(alias: str = "c") -> str:
    """块的指纹：标题加正文的 md5。块重切以后指纹变了，分数就算过期。"""
    return f"md5(coalesce({alias}.heading, '') || E'\\n' || coalesce({alias}.body, ''))"


# 要打分的块：图片文字块、中文块都打；英文文章的原文块在这篇有中文译文块时不打。
_SCORED_CHUNK = (
    "(c.lang <> '' OR NOT EXISTS (SELECT 1 FROM article_chunks t"
    " WHERE t.article_id = c.article_id AND t.lang = 'zh'))"
)
_STALE_CHUNK = (
    "(m.article_id IS NULL OR m.prompt_version < %(version)s OR m.chunk_hash <> " + chunk_hash_sql("c") + ")"
)


def pending_articles(conn, source_ids: list[str], limit: int | None):
    """还有块没打分（或分数过期）的文章，新的在前。limit 为 None 时全要（报数用）。

    交回每篇的 id、标题、要打的块数和字数。
    """
    limit_sql = "LIMIT %(limit)s" if limit is not None else ""
    rows = conn.execute(
        f"""
        SELECT a.id, a.title, count(*) AS chunks, sum(length(c.body)) AS chars
        FROM articles a
        JOIN article_chunks c ON c.article_id = a.id
        LEFT JOIN chunk_mechanism m ON m.article_id = c.article_id AND m.ord = c.ord
        WHERE a.source_id = ANY(%(sources)s) AND a.duplicate_of IS NULL
          AND {visible_sql("a")} AND {site_visible_sql("a")}
          AND {cooling_sql(FAILURE_KIND, 'a.id')}
          AND {_SCORED_CHUNK} AND {_STALE_CHUNK}
        GROUP BY a.id, a.title, a.published_at
        ORDER BY a.published_at DESC NULLS LAST, a.id DESC
        {limit_sql}
        """,
        {"sources": source_ids, "version": PROMPT_VERSION, "limit": limit},
    ).fetchall()
    return rows


def stale_chunks(conn, article_id: int) -> list[dict]:
    """这一篇里要打分的块：(ord, 标题, 正文, 指纹)。"""
    return conn.execute(
        f"""
        SELECT c.ord, c.heading, c.body, {chunk_hash_sql("c")} AS chunk_hash
        FROM article_chunks c
        LEFT JOIN chunk_mechanism m ON m.article_id = c.article_id AND m.ord = c.ord
        WHERE c.article_id = %(id)s AND {_SCORED_CHUNK} AND {_STALE_CHUNK}
        ORDER BY c.ord
        """,
        {"id": article_id, "version": PROMPT_VERSION},
    ).fetchall()


def chunks_by_ord(conn, article_id: int, ords: list[int]) -> list[dict]:
    """点名的几块（校准用），不管有没有分数。"""
    return conn.execute(
        f"""
        SELECT c.ord, c.heading, c.body, {chunk_hash_sql("c")} AS chunk_hash
        FROM article_chunks c WHERE c.article_id = %s AND c.ord = ANY(%s) ORDER BY c.ord
        """,
        (article_id, ords),
    ).fetchall()


def batches(chunks: list[dict], limit: int | None = None) -> list[list[dict]]:
    """按字数把块分成几次问（默认 ``BATCH_CHARS``）。一块超过上限也单独成一批。"""
    limit = BATCH_CHARS if limit is None else limit
    out: list[list[dict]] = []
    current: list[dict] = []
    size = 0
    for chunk in chunks:
        length = min(len(chunk["body"] or ""), CHUNK_LIMIT)
        if current and size + length > limit:
            out.append(current)
            current, size = [], 0
        current.append(chunk)
        size += length
    if current:
        out.append(current)
    return out


def render(title: str, chunks: list[dict]) -> str:
    """一批块写成给模型的正文。编号用块在这一批里的序号（从 1 开始），不用 ord：ord 有上万的，模型容易抄错。"""
    parts = [f"文章标题：{title}"]
    for index, chunk in enumerate(chunks, 1):
        heading = (chunk["heading"] or "").strip()
        body = (chunk["body"] or "").strip()[:CHUNK_LIMIT]
        parts.append(f"【{index}】" + (f"（小标题：{heading}）" if heading else "") + "\n" + body)
    return "\n\n".join(parts)


def parse_scores(text: str, count: int) -> list[int]:
    """读出每段的分。缺了哪一段、分数不是 0 到 2 就整批算失败（抛 LLMError），不猜。"""
    match = _JSON.search(text or "")
    if not match:
        raise LLMError("打分的回答里没有 JSON")
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise LLMError(f"打分的 JSON 读不出来：{exc}") from exc
    items = data.get("scores") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise LLMError("打分的 JSON 里没有 scores")
    scores: dict[int, int] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            index, score = int(item.get("id")), int(item.get("score"))
        except (TypeError, ValueError):
            continue
        if 1 <= index <= count and score in (0, 1, 2):
            scores[index] = score
    missing = [index for index in range(1, count + 1) if index not in scores]
    if missing:
        raise LLMError(f"打分缺了第 {', '.join(map(str, missing[:5]))} 段")
    return [scores[index] for index in range(1, count + 1)]


def score_chunks(settings: Settings, title: str, chunks: list[dict], usage: dict) -> tuple[list[int], str]:
    """给这些块打分（分批问），交回和 chunks 一一对应的分数和模型名。"""
    scores: list[int] = []
    model = ""
    for batch in batches(chunks):
        text, model = chat(
            settings,
            [{"role": "system", "content": PROMPT}, {"role": "user", "content": render(title, batch)}],
            temperature=0.0,
            usage=usage,
        )
        scores.extend(parse_scores(text, len(batch)))
    return scores, model


def save_scores(conn, article_id: int, chunks: list[dict], scores: list[int], model: str) -> None:
    for chunk, score in zip(chunks, scores, strict=True):
        conn.execute(
            """
            INSERT INTO chunk_mechanism (article_id, ord, chunk_hash, score, prompt_version, model)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (article_id, ord) DO UPDATE SET
              chunk_hash = EXCLUDED.chunk_hash, score = EXCLUDED.score,
              prompt_version = EXCLUDED.prompt_version, model = EXCLUDED.model, scored_at = now()
            """,
            (article_id, chunk["ord"], chunk["chunk_hash"], score, PROMPT_VERSION, model),
        )


def run_batch(settings: Settings, jobs: list[dict], **kwargs) -> dict:
    """把 jobs（[{"id", "title"}]）里每篇过期的块打上分。占到锁以后再查一遍要打哪几块。"""

    def handle(conn, job: dict, usage: dict) -> str | None:
        article_id = int(job["id"])
        chunks = stale_chunks(conn, article_id)
        conn.commit()
        if not chunks:
            return None
        scores, model = score_chunks(settings, str(job.get("title") or ""), chunks, usage)
        save_scores(conn, article_id, chunks, scores, model)
        clear_failure(conn, FAILURE_KIND, article_id)
        conn.commit()
        counts = [scores.count(value) for value in (2, 1, 0)]
        return f"{len(chunks)} 块：讲机制 {counts[0]}、讲到一些 {counts[1]}、没讲 {counts[2]}"

    return llm_batch.run(settings, jobs, handle, kind=FAILURE_KIND, lock_name=LOCK_NAME, verb="打分", **kwargs)


def load_sample(path: str) -> list[dict]:
    """人工判分：[{"query", "article_id", "ord", "label"}]，见 tools/quality_eval/mechanism.json。"""
    if path == "-":
        # 容器镜像里没有 tools/，从宿主机用 < 喂进来：--calibrate - < tools/quality_eval/…json
        data = json.load(sys.stdin)
    else:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    items = data.get("items") if isinstance(data, dict) else data
    if any(type(item.get("label")) is not int or item["label"] not in (0, 1, 2) for item in items):
        raise ValueError("先为每条待校准的正文填写人工 label（0、1 或 2）")
    return [
        {"query": str(item.get("query") or ""), "article_id": int(item["article_id"]), "ord": int(item["ord"]),
         "label": int(item["label"]), **({"chunk_hash": item["chunk_hash"]} if item.get("chunk_hash") else {})}
        for item in items
    ]


def review_sample(conn, path: str) -> dict:
    """导出当前实际文本供重新盲评。不把旧分自动签给当前块；人工填入 label 后才可校准。"""
    rows = []
    for old in load_sample(path):
        chunks = chunks_by_ord(conn, old["article_id"], [old["ord"]])
        if not chunks:
            print(f"块已不存在：{old['article_id']} / {old['ord']}", file=sys.stderr)
            continue
        chunk = chunks[0]
        rows.append({"query": old["query"], "article_id": old["article_id"], "ord": old["ord"],
                     "heading": chunk["heading"], "body": chunk["body"], "chunk_hash": chunk["chunk_hash"],
                     "label": None})
    return {"items": rows}


def agreement(pairs: list[tuple[int, int]]) -> dict:
    """人工分和模型分对得上多少：一字不差的比例、两边都判「讲机制」(2) 的查准查全、二次加权 kappa。"""
    total = len(pairs)
    if not total:
        return {"n": 0}
    exact = sum(1 for human, model in pairs if human == model) / total
    hits = sum(1 for human, model in pairs if human == 2 and model == 2)
    model_two = sum(1 for _human, model in pairs if model == 2)
    human_two = sum(1 for human, _model in pairs if human == 2)
    # 二次加权 kappa：0 是瞎猜的水平，1 是完全一致。
    weights = {(i, j): (i - j) ** 2 / 4 for i in range(3) for j in range(3)}
    observed = sum(weights[(human, model)] for human, model in pairs) / total
    human_share = [sum(1 for h, _ in pairs if h == k) / total for k in range(3)]
    model_share = [sum(1 for _, m in pairs if m == k) / total for k in range(3)]
    expected = sum(weights[(i, j)] * human_share[i] * model_share[j] for i in range(3) for j in range(3))
    kappa = 1 - observed / expected if expected else 0.0
    matrix = [[sum(1 for h, m in pairs if h == i and m == j) for j in range(3)] for i in range(3)]
    return {
        "n": total,
        "exact": round(exact, 3),
        "precision2": round(hits / model_two, 3) if model_two else None,
        "recall2": round(hits / human_two, 3) if human_two else None,
        "kappa": round(kappa, 3),
        "matrix": matrix,
    }


def calibrate(settings: Settings, path: str, workers: int, off_peak: bool) -> int:
    """打人工判过的块、和人工比。不写库。打印对得上多少、用了多少 token（按它推全库费用）。"""
    from researchlib.db import connect

    sample = load_sample(path)
    by_article: dict[int, list[dict]] = {}
    unsigned = sum(1 for item in sample if not item.get("chunk_hash"))
    if unsigned:
        print(f"{unsigned} 条未绑定正文，不计入一致率；用 --review-sample 导出后重新盲评。", file=sys.stderr)
    for item in sample:
        if not item.get("chunk_hash"):
            continue
        by_article.setdefault(item["article_id"], []).append(item)
    if not by_article:
        return 1
    conn = connect(settings)
    try:
        titles = {
            int(row["id"]): row["title"]
            for row in conn.execute("SELECT id, title FROM articles WHERE id = ANY(%s)", (list(by_article),))
        }
    finally:
        conn.close()
    results: dict[tuple[int, int, str], int] = {}
    chars = {"n": 0}

    def handle(conn, job: dict, usage: dict) -> str | None:
        article_id = int(job["id"])
        expected = {(item["ord"], item["chunk_hash"]) for item in by_article[article_id]}
        chunks = [chunk for chunk in chunks_by_ord(conn, article_id, [item["ord"] for item in by_article[article_id]])
                  if (chunk["ord"], chunk["chunk_hash"]) in expected]
        conn.commit()
        if not chunks:
            return None
        scores, _model = score_chunks(settings, str(job.get("title") or ""), chunks, usage)
        for chunk, score in zip(chunks, scores, strict=True):
            results[(article_id, int(chunk["ord"]), chunk["chunk_hash"])] = score
        chars["n"] += sum(min(len(chunk["body"] or ""), CHUNK_LIMIT) for chunk in chunks)
        return " ".join(str(score) for score in scores)

    jobs = [{"id": article_id, "title": titles[article_id]} for article_id in by_article if article_id in titles]
    report = llm_batch.run(
        settings, jobs, handle, kind=FAILURE_KIND, lock_name=LOCK_NAME + "-calibrate", verb="打分",
        workers=workers, off_peak=off_peak,
    )
    pairs = [
        (item["label"], results[key])
        for item in sample
        if (key := (item["article_id"], item["ord"], item.get("chunk_hash"))) in results
    ]
    stats = agreement(pairs)
    print(f"人工判过 {len(sample)} 块，模型打了 {len(pairs)} 块（缺块、未绑定、正文变化或失败的均未计分）")
    print(json.dumps(stats, ensure_ascii=False))
    usage = report["usage"]
    if chars["n"] and (usage.get("prompt") or usage.get("completion")):
        per = 10000 / chars["n"]
        print(
            f"每万字正文：进 {usage.get('prompt', 0) * per:,.0f} 出 {usage.get('completion', 0) * per:,.0f}"
            f"（其中思考 {usage.get('reasoning', 0) * per:,.0f}）token，拿它乘 --limit 0 报的字数推全库用量"
        )
    print("合计" + llm_batch.tail(report, "打分"))
    return llm_batch.exit_code(report, "打分")


def main(argv: list[str] | None = None) -> int:
    """退出码同 research-digest：0 跑完，2 整批停（没凭证、鉴权、余额），3 接口连着不通、暂停。"""
    parser = argparse.ArgumentParser(description="给检索块打「讲没讲机制和原因」的分（花模型钱）")
    parser.add_argument("--limit", type=int, default=50, help="最多打几篇；0 只报还要打多少，不调模型")
    parser.add_argument("--source", default="")
    parser.add_argument("--max-minutes", type=float, default=0, help="跑到这么多分钟就停，0 表示不限")
    parser.add_argument(
        "--effort",
        choices=EFFORTS,
        default="",
        help="思考强度（DeepSeek：low、high、max）。不设按全站默认；打分用不着最深的思考，先用 --calibrate 比",
    )
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--review-sample", help="把旧样本当前的原文、哈希、待填标签打印为 JSON，不调模型")
    modes.add_argument("--calibrate", default="", help="人工判分文件，- 从标准输入读：只打这些块、和人工比，不写库")
    llm_batch.add_arguments(parser, "打分")
    args = parser.parse_args(argv)
    llm_batch.check_arguments(args)
    from researchlib.db import prepare

    settings = Settings.from_env()
    if args.effort:
        settings = replace(settings, llm_effort=args.effort)
    sources = enabled_sources(load_sources(settings.sources_path))
    if args.source:
        sources = [source for source in sources if source.id == args.source]
        if not sources:
            raise SystemExit(f"{args.source} 不是已启用来源")
    source_ids = [source.id for source in sources]
    conn = prepare(settings)
    try:
        if args.review_sample:
            print(json.dumps(review_sample(conn, args.review_sample), ensure_ascii=False, indent=2))
            return 0
        if args.limit == 0 and not args.calibrate:
            rows = pending_articles(conn, source_ids, None)
            chunks = sum(int(row["chunks"]) for row in rows)
            chars = sum(int(row["chars"] or 0) for row in rows)
            print(f"还要打分 {len(rows)} 篇、{chunks} 块、正文约 {chars:,} 字（不算标题）")
            return 0
        if not args.calibrate:
            rows = pending_articles(conn, source_ids, args.limit)
            jobs = [{"id": int(row["id"]), "title": row["title"]} for row in rows]
    finally:
        conn.close()
    try:
        ensure_oauth(settings)
    except LLMUnavailable as exc:
        print(exc, file=sys.stderr)
        return 2
    workers, off_peak = llm_batch.resolve(settings, args, "打分")
    if args.calibrate:
        return calibrate(settings, args.calibrate, workers, off_peak)
    deadline = time.monotonic() + args.max_minutes * 60 if args.max_minutes > 0 else None
    report = run_batch(settings, jobs, workers=workers, deadline=deadline, off_peak=off_peak)
    if report["timed_out"]:
        print("到了时间上限，先停在这里", file=sys.stderr)
    print(
        f"待打分 {len(jobs)} 篇，打完 {report['done']} 篇，失败 {report['failed']} 篇" + llm_batch.tail(report, "打分")
    )
    return llm_batch.exit_code(report, "打分")


if __name__ == "__main__":
    sys.exit(main())
