"""按字数估算摘要和判断还要花多少 token、多少钱。不调用模型，不连网。

单价和 docs/llm.md 一致：DeepSeek 非高峰每百万 token 进 0.15 美元、出 0.60 美元，
高峰（北京时间工作日 9–12 点、14–18 点）翻倍，思考算在输出里。
输出按同页「一次补完积压」的规划区间，每次 4,000 到 10,000 token，不是这次实测。
输入按 1 个字 1 个 token 估，是上沿。Grok OAuth 走订阅，token 照样算，不换算成美元。

    research-llm-estimate --listing /data/inbox/latepost/raw/post-history.jsonl --inbox /data/backfill/latepost
    docker compose exec app research-llm-estimate --source latepost
    docker compose exec app research-llm-estimate --articles 1,2,3

导入本身不跑这个命令，也不会因此去调模型。
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

from researchlib.excerpt import DIGEST_LIMIT, VIEW_WINDOW

# docs/llm.md「一次补完积压」：非高峰每百万 token 进 0.15、出 0.60 美元，高峰翻倍。
DEEPSEEK_INPUT_PER_MILLION = 0.15
DEEPSEEK_OUTPUT_PER_MILLION = 0.60
PEAK_MULTIPLIER = 2
# 同页：每次思考加回答按 4,000 到 10,000 token 算。
OUTPUT_TOKENS_LOW = 4_000
OUTPUT_TOKENS_HIGH = 10_000
# 同页：方伟公众号抽样大约 6,700 字。列表还没有正文时用这个数，并标明是假设。
ASSUMED_BODY_CHARS = 6700
# 标题、作者、日期那几行，不逐篇去拼。
HEADER_CHARS = 80
# 和 digest.MIN_DIGEST_CHARS 一样：再短就不写摘要。
MIN_DIGEST_CHARS = 20
# scripts/nightly.sh 的默认上限。改了脚本要一起改。
NIGHTLY_DIGEST_LIMIT = 80
NIGHTLY_VIEWS_LIMIT = 60
# docs/ocr.md：一张图输出大约 10,764 token。图片本身的输入 token 这里不算。
OCR_OUTPUT_TOKENS = 10_764


@dataclass(frozen=True)
class ArticleSpec:
    chars: int
    assumed: bool = False
    need_digest: bool = True
    # None：还没导入，不知道有没有深入提及。0：没有。正数：深入提到的公司家数。
    deep_mentions: int | None = None
    images: int | None = None


@dataclass(frozen=True)
class JobEstimate:
    name: str
    calls: int
    articles: int
    input_tokens: int
    output_low: int
    output_high: int
    note: str


@dataclass(frozen=True)
class Estimate:
    articles: int
    measured: int
    assumed: int
    assumed_chars: int
    skipped: int
    short_skipped: int
    digest: JobEstimate
    views: JobEstimate | None
    views_are_upper: bool
    images: int | None


def dollars(input_tokens: int, output_tokens: int, *, peak: bool = False) -> float:
    multiplier = PEAK_MULTIPLIER if peak else 1
    return (
        input_tokens / 1_000_000 * DEEPSEEK_INPUT_PER_MILLION * multiplier
        + output_tokens / 1_000_000 * DEEPSEEK_OUTPUT_PER_MILLION * multiplier
    )


def _view_slices(chars: int) -> tuple[int, int]:
    """按整篇长度粗算判断要问几次、正文送多少字。实际只送提到公司的段落，通常更少。"""
    remaining = max(chars, 0)
    calls = 0
    body = 0
    while remaining > 0 and calls < 40:
        take = min(remaining, VIEW_WINDOW)
        body += take
        remaining -= take
        calls += 1
    return calls, body


def _digest_job(specs: list[ArticleSpec], prompt_chars: int) -> tuple[JobEstimate, int]:
    calls = 0
    input_tokens = 0
    short = 0
    for spec in specs:
        if not spec.need_digest:
            continue
        if spec.chars < MIN_DIGEST_CHARS and not spec.assumed:
            short += 1
            continue
        calls += 1
        input_tokens += min(spec.chars, DIGEST_LIMIT) + prompt_chars + HEADER_CHARS
    note = (
        f"每篇 1 次。提示词 {prompt_chars} 字，另加约 {HEADER_CHARS} 字的标题行；"
        f"正文超过 {DIGEST_LIMIT} 字只送节选。输出是规划区间，不是实测。"
    )
    return (
        JobEstimate("摘要", calls, calls, input_tokens, calls * OUTPUT_TOKENS_LOW, calls * OUTPUT_TOKENS_HIGH, note),
        short,
    )


def _views_job(specs: list[ArticleSpec], prompt_chars: int) -> tuple[JobEstimate | None, bool]:
    unknown = any(spec.deep_mentions is None for spec in specs)
    calls = 0
    articles = 0
    input_tokens = 0
    for spec in specs:
        if spec.deep_mentions is None:
            if not unknown:
                continue
            include = True
        else:
            include = spec.deep_mentions > 0
            if unknown and not include:
                continue
        if not include:
            continue
        n_calls, body = _view_slices(spec.chars)
        if n_calls == 0:
            continue
        articles += 1
        calls += n_calls
        input_tokens += body + n_calls * prompt_chars
    if unknown:
        note = (
            "上沿：导入并算出公司提及之前，不知道有多少篇会抽。"
            "这里按列出的每一篇都深入提到公司。实际只抽深入提及，一带而过不抽；"
            f"长文只送提到公司的段落（一块约 {VIEW_WINDOW} 字），通常少于此数。"
        )
    else:
        note = (
            f"只算库里已经有深入提及的篇。按整篇长度每 {VIEW_WINDOW} 字 1 次，是上沿；"
            "实际只送提到公司的段落。"
        )
    if not unknown and calls == 0:
        return JobEstimate("判断", 0, 0, 0, 0, 0, note), False
    return (
        JobEstimate(
            "判断",
            calls,
            articles,
            input_tokens,
            calls * OUTPUT_TOKENS_LOW,
            calls * OUTPUT_TOKENS_HIGH,
            note,
        ),
        unknown,
    )


def build_estimate(
    specs: list[ArticleSpec],
    *,
    digest_prompt_chars: int,
    views_prompt_chars: int,
    skipped: int = 0,
    assumed_chars: int = ASSUMED_BODY_CHARS,
) -> Estimate:
    digest, short = _digest_job(specs, digest_prompt_chars)
    views, upper = _views_job(specs, views_prompt_chars)
    images: int | None = 0
    for spec in specs:
        if spec.images is None:
            images = None
            break
        images += spec.images
    return Estimate(
        articles=len(specs),
        measured=sum(1 for spec in specs if not spec.assumed),
        assumed=sum(1 for spec in specs if spec.assumed),
        assumed_chars=assumed_chars,
        skipped=skipped,
        short_skipped=short,
        digest=digest,
        views=views,
        views_are_upper=upper,
        images=images,
    )


def _money_span(job: JobEstimate) -> str:
    if job.calls == 0:
        return "0 美元"
    low = dollars(job.input_tokens, job.output_low)
    high = dollars(job.input_tokens, job.output_high)
    peak_low = dollars(job.input_tokens, job.output_low, peak=True)
    peak_high = dollars(job.input_tokens, job.output_high, peak=True)
    return (
        f"非高峰 {low:.2f} 到 {high:.2f} 美元，高峰 {peak_low:.2f} 到 {peak_high:.2f} 美元"
    )


def _job_line(job: JobEstimate) -> str:
    return (
        f"{job.name}: {job.articles} 篇、{job.calls} 次调用，输入约 {job.input_tokens:,} token，"
        f"输出 {job.output_low:,} 到 {job.output_high:,} token（含思考）。"
        f"DeepSeek {_money_span(job)}。{job.note}"
    )


def _nights(calls: int, per_night: int) -> int:
    if calls <= 0 or per_night <= 0:
        return 0
    return (calls + per_night - 1) // per_night


def render_estimate(estimate: Estimate, *, provider_text: str) -> str:
    lines = [
        provider_text.rstrip(),
        "这是估算，不是报价。没有调用模型。",
        (
            f"文章: {estimate.articles} 篇（量过正文的 {estimate.measured} 篇，"
            f"按每篇 {estimate.assumed_chars} 字假设的 {estimate.assumed} 篇）"
        ),
    ]
    if estimate.skipped:
        lines.append(f"列表里另有 {estimate.skipped} 篇跳过（不是文章、已删除或还在审核），不进估算")
    if estimate.short_skipped:
        lines.append(f"另有 {estimate.short_skipped} 篇不足 {MIN_DIGEST_CHARS} 字，不写摘要")
    lines.append(_job_line(estimate.digest))
    if estimate.views is not None:
        label = "判断（上沿）" if estimate.views_are_upper else "判断"
        lines.append(_job_line(estimate.views).replace("判断:", f"{label}:", 1))
    if estimate.views is not None and not estimate.views_are_upper:
        both_low = dollars(
            estimate.digest.input_tokens + estimate.views.input_tokens,
            estimate.digest.output_low + estimate.views.output_low,
        )
        both_high = dollars(
            estimate.digest.input_tokens + estimate.views.input_tokens,
            estimate.digest.output_high + estimate.views.output_high,
        )
        lines.append(
            f"摘要加判断（DeepSeek 非高峰）: {both_low:.2f} 到 {both_high:.2f} 美元。"
            "判断按整篇切块，实际通常更少。"
        )
    else:
        lines.append("合计只加了摘要。判断还是上沿，不要和摘要加在一起当成已经要付的钱。")
    if estimate.images is None:
        lines.append("识图: 张数还不知道（要等正文抓下来并导入）。夜间默认关（NIGHTLY_OCR=0），打开才按张花钱。")
    else:
        output = estimate.images * OCR_OUTPUT_TOKENS
        lines.append(
            f"识图: {estimate.images} 张。夜间默认关（NIGHTLY_OCR=0）。"
            f"若打开，输出大约 {output:,} token（约每张 {OCR_OUTPUT_TOKENS}，docs/ocr.md 的实测），"
            "图片本身的输入 token 另算，不在上面的金额里。"
        )
    lines.append("译制: 0 篇算进这次。只译英文（Stratechery）。中文公众号不进待译队列。")
    lines.append("论点: 按公司，不按篇。夜间默认关（THESES_ENABLED=0），导入不会跑 research-theses。")
    lines.append("段落机制分: 不在夜间任务里，这次不算。要跑得另报费用。")
    lines.append("问库: 只有读者提问才调，导入不会触发。")
    digest_nights = _nights(estimate.digest.calls, NIGHTLY_DIGEST_LIMIT)
    view_calls = estimate.views.calls if estimate.views is not None else 0
    view_nights = _nights(view_calls, NIGHTLY_VIEWS_LIMIT)
    lines.append(
        "导入之后谁会接着花钱（导入这一步本身不调模型，网页打开也不会排队）："
    )
    lines.append(
        f"- 摘要：夜间 research-digest 每晚最多 {NIGHTLY_DIGEST_LIMIT} 篇，"
        f"上面这些摘要大约 {digest_nights} 晚。定时抓取只当场处理它自己那次抓进来的新文章"
        "（research-process-new），回填导入的不碰。同一篇有锁，不会写两遍。"
    )
    lines.append(
        f"- 判断：夜间 research-views 每晚最多 {NIGHTLY_VIEWS_LIMIT} 篇，只抽深入提到公司的。"
        f"按上面的次数大约 {view_nights} 晚（上沿会更久，因为实际篇数通常更少）。"
    )
    lines.append(
        "- 夜间任务 00:37 开始，不在 DeepSeek 高峰价时段（北京时间工作日 9–12 点、14–18 点）。"
        "回填这种大面积的手动跑要加 --off-peak，在高峰跑金额按上面的高峰一档。"
    )
    lines.append(
        "- 外链图片由定时抓取每次最多 200 张、夜间最多 300 张接着下，不调用上面的模型。"
        "配了 EMBEDDING_BASE_URL 时，导入会让本机向量服务算新段落的向量，那不是 DeepSeek / Grok。"
    )
    return "\n".join(lines)


def provider_text(environ: dict[str, str] | None = None) -> str:
    from researchlib.deepseek import deepseek_model, deepseek_reasoning_effort
    from researchlib.grok import GROK_MODEL_ID, GROK_REASONING_EFFORT
    from researchlib.llm_provider import PROVIDER_DEEPSEEK, llm_provider

    provider = llm_provider(environ)
    if provider == PROVIDER_DEEPSEEK:
        current = (
            f"当前提供方: DeepSeek（{deepseek_model(environ)}，"
            f"reasoning_effort={deepseek_reasoning_effort(environ)}）"
        )
    else:
        current = (
            f"当前提供方: Grok OAuth（{GROK_MODEL_ID}，reasoning_effort={GROK_REASONING_EFFORT}，"
            "订阅，不按 token 另收）"
        )
    prices = (
        "DeepSeek 非高峰价：每百万 token 进 0.15 美元、出 0.60 美元；"
        "高峰（北京时间工作日 9–12 点、14–18 点；节假日没单独认）翻倍。思考算在输出里。"
        "金额按 DeepSeek 报。当前若是 Grok，只看 token，不另收这些美元。"
    )
    return current + "\n" + prices


def digest_prompt_chars() -> int:
    from researchlib.digest import PROMPT

    return len(PROMPT)


def views_prompt_chars() -> int:
    from researchlib.views import PROMPT

    return len(PROMPT)


def specs_from_listing(
    path: Path,
    *,
    chars_by_sn: dict[str, int] | None = None,
    chars_each: int | None = None,
) -> tuple[list[ArticleSpec], int]:
    from researchlib.wechat_crawl import _load_listing

    rows = _load_listing(path)
    chars_by_sn = chars_by_sn or {}
    specs: list[ArticleSpec] = []
    skipped = 0
    for row in rows:
        if row.get("skip"):
            skipped += 1
            continue
        sn = str(row.get("sn") or "")
        if sn and sn in chars_by_sn:
            specs.append(ArticleSpec(chars=chars_by_sn[sn], assumed=False))
        elif chars_each:
            specs.append(ArticleSpec(chars=chars_each, assumed=True))
        else:
            specs.append(ArticleSpec(chars=ASSUMED_BODY_CHARS, assumed=True))
    return specs, skipped


def archive_chars(inbox: Path) -> dict[str, int]:
    """归档 Markdown 的全文长度（含归档头，略多于正文）。没有 md 目录就返回空。"""
    found: dict[str, int] = {}
    root = inbox / "md"
    if not root.is_dir():
        return found
    for path in root.rglob("*.md"):
        sn = path.stem.rsplit("-", 1)[-1].lower()
        if len(sn) == 32 and all(char in "0123456789abcdef" for char in sn):
            found[sn] = len(path.read_text(encoding="utf-8", errors="replace"))
    return found


def specs_from_rows(rows) -> list[ArticleSpec]:
    specs: list[ArticleSpec] = []
    for row in rows:
        specs.append(
            ArticleSpec(
                chars=int(row["chars"] or 0),
                assumed=False,
                need_digest=bool(row["need_digest"]),
                deep_mentions=int(row["deep_n"] or 0),
                images=int(row["images"] or 0),
            )
        )
    return specs


def load_db_rows(conn, *, source_id: str = "", article_ids: list[int] | None = None):
    from researchlib.author_voice import site_visible_sql
    from researchlib.digest import PROMPT_VERSION
    from researchlib.visibility import visible_sql

    where = ["a.duplicate_of IS NULL", visible_sql("a"), site_visible_sql("a")]
    params: list = [PROMPT_VERSION]
    if source_id:
        where.append("a.source_id = %s")
        params.append(source_id)
    if article_ids:
        where.append("a.id = ANY(%s)")
        params.append(article_ids)
    return conn.execute(
        f"""
        SELECT length(coalesce(a.body_text, '')) AS chars,
               (d.article_id IS NULL OR d.content_hash IS DISTINCT FROM a.content_hash
                    OR d.prompt_version < %s) AS need_digest,
               coalesce(v.deep_n, 0) AS deep_n,
               coalesce(img.n, 0) AS images
        FROM articles a
        LEFT JOIN article_digests d ON d.article_id = a.id
        LEFT JOIN (
            SELECT article_id, count(*) AS deep_n
            FROM article_mentions
            WHERE depth <> 'passing'
            GROUP BY article_id
        ) v ON v.article_id = a.id
        LEFT JOIN (
            SELECT article_id, count(*) AS n
            FROM article_images
            GROUP BY article_id
        ) img ON img.article_id = a.id
        WHERE {" AND ".join(where)}
        """,
        tuple(params),
    ).fetchall()


def _parse_ids(text: str) -> list[int]:
    found: list[int] = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if not part.isdigit():
            raise ValueError(f"文章 id 要是数字，收到 {part!r}")
        found.append(int(part))
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="估算回填之后摘要和判断大约要多少 token、多少钱。不调用模型。")
    parser.add_argument("--listing", default="", help="--list-only 写下的 post-history.jsonl")
    parser.add_argument("--inbox", default="", help="归档目录。有 md 就按文件字数，不再用假设篇幅")
    parser.add_argument("--chars-each", type=int, default=0, help="列表没有正文时，每篇按这么多字估。0 用默认 6700")
    parser.add_argument("--source", default="", help="按这个来源库里的文章估算（导入之后）")
    parser.add_argument("--articles", default="", help="逗号分隔的文章 id")
    args = parser.parse_args(argv)
    if args.listing and (args.source or args.articles):
        print("拒绝：--listing 不要和 --source、--articles 一起用", file=sys.stderr)
        return 2
    if not args.listing and not args.source and not args.articles:
        print("拒绝：要给 --listing，或 --source，或 --articles", file=sys.stderr)
        return 2
    if args.chars_each < 0:
        print("拒绝：--chars-each 不能是负数", file=sys.stderr)
        return 2

    skipped = 0
    assumed_chars = args.chars_each or ASSUMED_BODY_CHARS
    if args.listing:
        path = Path(args.listing)
        if not path.is_file():
            print(f"拒绝：找不到列表文件 {path}", file=sys.stderr)
            return 2
        chars_by_sn = archive_chars(Path(args.inbox)) if args.inbox else {}
        specs, skipped = specs_from_listing(
            path,
            chars_by_sn=chars_by_sn,
            chars_each=args.chars_each or None,
        )
    else:
        from researchlib.config import Settings
        from researchlib.db import prepare

        try:
            article_ids = _parse_ids(args.articles)
        except ValueError as exc:
            print(f"拒绝：{exc}", file=sys.stderr)
            return 2
        settings = Settings.from_env()
        conn = prepare(settings)
        try:
            rows = load_db_rows(conn, source_id=args.source, article_ids=article_ids or None)
        finally:
            conn.close()
        specs = specs_from_rows(rows)
    estimate = build_estimate(
        specs,
        digest_prompt_chars=digest_prompt_chars(),
        views_prompt_chars=views_prompt_chars(),
        skipped=skipped,
        assumed_chars=assumed_chars,
    )
    print(render_estimate(estimate, provider_text=provider_text()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
