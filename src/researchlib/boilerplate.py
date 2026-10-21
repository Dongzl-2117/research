"""正文清洗：归档头、公众号和星球的引流尾巴、广告图。

库里的 body_md 保持导入时的原样（改稿、删稿核对都靠它），清洗只发生在两处：

- 文章页显示正文之前；
- 算 body_text 和段落块之前（检索、摘要、观点读的都是清洗后的文字）。

`file:///H.HTM` 这类 PDF 页内链接从显示和检索文字里去掉，不改这里的三条统计规则。
段永平博客要连 body_md 一起洗掉时，用 `research-ingest-duanyongping --reclassify`。

三条规则，都按同一来源多篇文章的统计来判，不写死某个作者的话术：

1. 归档头：正文开头那几行「- 作者：」「- 时间：」「- sn：」「- 原链接：」「- 原始数据：」
   「- 类型：」「- 点赞 · 评论 · 阅读人数」「- topic_id：」，整块去掉。
2. 模板句：同一来源里在很多篇（至少 max(5, 2%) 篇）都出现的整行。文末从第一行模板句起，
   后面几乎全是模板句、引流关键词或几个字的碎行时，整段切掉（招募被拆成一行几个字的也切得到）。
   正文中间的模板句只在带引流关键词时去掉；飞书那类重复的小标题不带关键词，留着。
3. 广告图：同一来源里至少 5 篇都出现的同一张图（按内容哈希），而且不是成批一起出现
   （飞书父文档会带上子文档的整批插图，那是正文）。二维码、优惠券海报、「长按关注」都是这种。
"""

from __future__ import annotations

import math
import re
import threading
from collections import Counter
from dataclasses import dataclass, field

from researchlib.textutil import scrub_file_page_junk

_KEEP = re.compile(r"[0-9A-Za-z一-鿿]+")
_IMAGE = re.compile(
    r"!\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)|<img\b[^>]*\bsrc\s*=\s*[\"']([^\"']+)[\"'][^>]*>", re.I
)
_HEADING = re.compile(r"^\s*#{1,6}\s+\S")
_META_KEYS = (
    r"作者|时间|发布时间|类型|点赞|sn|topic_id|原链接|原文链接|原始数据|来源|公众号|阅读人数|阅读|"
    r"ghid|appmsgid|position|author|url|source"
)
_META_LINE = re.compile(r"^\s*[-*]\s*(?:" + _META_KEYS + r")\s*(?:[:：]|\s+\d|\s*·)", re.I)
# 带这些词的行在文末算引流。
PROMO = re.compile(
    r"加入(?:会员群|知识星球|星球|微信群|交流群|社群|我们)|知识星球|会员群|交流群|扫码|二维码|长按(?:识别|扫码|关注)|"
    r"白嫖|非诚勿扰|加我|加微信|微信号|私信我|优惠券|立减|免费赠送|直接购买|备注[:：]|点个?在看|点赞.{0,3}在看|"
    r"星标|关注(?:公众号|我们|本号)|转发.{0,4}朋友圈|欢迎(?:关注|转发)|赞赏|打赏|推荐阅读|往期(?:回顾|推荐|文章)"
)
COMMENTS = re.compile(r"^##\s*(?:评论|评论里的回复)")
MIN_LINE = 4  # 归一化后短于这个的行算碎行，不参与统计
MIN_DF = 5
DF_SHARE = 0.02
IMAGE_MIN_DF = 5
IMAGE_BATCH = 3  # 同一批文章里一起出现的图达到这么多张，算正文插图（飞书父子文档）
TAIL_RATIO = 0.25  # 切掉的尾巴里，正文字数不超过模板字数的这个比例
TAIL_GOOD_MAX = 160
TAIL_START = 0.35
IMAGE_WEIGHT = 400
TAIL_STRONG = 2  # 尾巴里至少两行模板句或模板图，单独一行话题标签不算
# 文档库（飞书）是作者自己整理的笔记，没有引流尾巴；重复的截图是父子文档共用的正文插图。只去归档头。
LIBRARY_KINDS = frozenset({"feishu"})  # 尾巴只能从全文这个位置之后开始


def normalize(text: str) -> str:
    return "".join(_KEEP.findall(text or "")).lower()


@dataclass
class Profile:
    lines: set[str] = field(default_factory=set)
    images: set[str] = field(default_factory=set)

    def common(self, norm: str) -> bool:
        return norm in self.lines


EMPTY = Profile()


def image_refs(line: str) -> list[str]:
    return [m.group(1) or m.group(2) for m in _IMAGE.finditer(line)]


def _only_images(line: str) -> bool:
    return bool(_IMAGE.search(line)) and not _IMAGE.sub("", line).strip()


def strip_meta(body: str) -> str:
    """去掉开头的归档头列表。只看标题之后、正文第一句之前那一段。"""
    lines = body.split("\n")
    out: list[str] = []
    started = False
    for index, line in enumerate(lines):
        if started:
            out.extend(lines[index:])
            break
        stripped = line.strip()
        if not stripped or _HEADING.match(line):
            out.append(line)
            continue
        if _META_LINE.match(line):
            continue
        started = True
        out.append(line)
    text = "\n".join(out)
    return re.sub(r"\n{3,}", "\n\n", text)


def _split_comments(lines: list[str]) -> int:
    for index, line in enumerate(lines):
        if COMMENTS.match(line):
            return index
    return len(lines)


def _classify(line: str, profile: Profile, hashes: dict[str, str]) -> tuple[str, int]:
    """('bad' | 'good' | 'neutral', 字数)。"""
    stripped = line.strip()
    if not stripped:
        return "neutral", 0
    if _only_images(stripped):
        refs = image_refs(stripped)
        if refs and all(hashes.get(ref) in profile.images for ref in refs):
            return "bad", 10
        # 不是模板图的插图算正文，而且分量很重：尾巴不能越过它往前切。
        return "good", IMAGE_WEIGHT
    norm = normalize(stripped)
    if len(norm) < MIN_LINE:
        return ("bad", len(norm)) if PROMO.search(stripped) else ("neutral", 0)
    if profile.common(norm) or PROMO.search(stripped):
        return "bad", len(norm)
    return "good", len(norm)


def _tail_has_hook(lines: list[str], profile: Profile, hashes: dict[str, str], start: int) -> bool:
    """尾巴里必须有引流词或广告图，光是多篇重复的正文套话不够（评测语料会共享细节句）。"""
    for line in lines[start:]:
        if PROMO.search(line):
            return True
        refs = image_refs(line)
        if refs and any(hashes.get(ref) in profile.images for ref in refs):
            return True
    return False


def _tail_cut(lines: list[str], profile: Profile, hashes: dict[str, str]) -> int:
    kinds = [_classify(line, profile, hashes) for line in lines]
    total = sum(size for kind, size in kinds if kind != "neutral") or 1
    prefix = [0]
    for kind, size in kinds:
        prefix.append(prefix[-1] + (size if kind != "neutral" else 0))
    good = bad = 0
    strong = 0
    cut = len(lines)
    for index in range(len(lines) - 1, -1, -1):
        kind, size = kinds[index]
        if kind == "good":
            good += size
        elif kind == "bad":
            bad += size
            norm = normalize(lines[index])
            # 只有统计出来的模板句、模板图才算铁证；光有「扫码」「二维码」这类词可能是正文在谈支付。
            if profile.common(norm) or image_refs(lines[index]):
                strong += 1
        if good > TAIL_GOOD_MAX * 3:
            break
        if (
            kind == "bad"
            and strong >= TAIL_STRONG
            and good <= max(bad * TAIL_RATIO, 0)
            and good <= TAIL_GOOD_MAX
            and prefix[index] >= total * TAIL_START
            and _tail_has_hook(lines, profile, hashes, index)
        ):
            cut = index
    return cut


def clean_body(body: str, profile: Profile = EMPTY, hashes: dict[str, str] | None = None) -> str:
    """body 是去掉 frontmatter 之后的 Markdown。hashes：图片原始引用 → 内容哈希。"""
    hashes = hashes or {}
    body = scrub_file_page_junk(strip_meta(body or ""))
    lines = body.split("\n")
    split = _split_comments(lines)
    main, rest = lines[:split], lines[split:]
    cut = _tail_cut(main, profile, hashes)
    main = main[:cut]
    kept: list[str] = []
    for line in main:
        stripped = line.strip()
        refs = image_refs(stripped)
        if refs and profile.images:
            if any(hashes.get(ref) in profile.images for ref in refs):
                remainder = _IMAGE.sub(
                    lambda m: "" if hashes.get(m.group(1) or m.group(2)) in profile.images else m.group(0), line
                )
                if not remainder.strip():
                    continue
                line = remainder
        norm = normalize(stripped)
        if len(norm) >= MIN_LINE and profile.common(norm) and PROMO.search(stripped):
            continue
        kept.append(line)
    while kept and not kept[-1].strip():
        kept.pop()
    # 尾巴前面常留一行「—」「*」之类的分隔符。
    while cut < len(main) and kept and re.fullmatch(r"[\s—\-–*·•_=~─]+", kept[-1]):
        kept.pop()
        while kept and not kept[-1].strip():
            kept.pop()
    text = "\n".join(kept)
    if rest:
        text = text.rstrip() + "\n\n" + "\n".join(rest)
    return re.sub(r"\n{3,}", "\n\n", text).strip("\n") + ("\n" if text else "")


# —— 按来源统计 ——

_cache: dict[str, tuple[tuple, Profile]] = {}
_lock = threading.Lock()


def _signature(conn, source_id: str) -> tuple:
    row = conn.execute(
        "SELECT count(*) AS n, max(updated_at) AS t FROM articles WHERE source_id = %s AND duplicate_of IS NULL",
        (source_id,),
    ).fetchone()
    images = conn.execute("SELECT count(*) AS n FROM article_images WHERE source_id = %s", (source_id,)).fetchone()
    return (int(row["n"]), str(row["t"]), int(images["n"]))


def threshold(articles: int) -> int:
    return max(MIN_DF, math.ceil(articles * DF_SHARE))


def build_profile(conn, source_id: str) -> Profile:
    from researchlib.textutil import split_frontmatter

    rows = conn.execute(
        "SELECT id, body_md FROM articles WHERE source_id = %s AND duplicate_of IS NULL", (source_id,)
    ).fetchall()
    df: Counter[str] = Counter()
    for row in rows:
        _front, body = split_frontmatter(row["body_md"] or "")
        lines = body.split("\n")
        seen = {normalize(line) for line in lines[: _split_comments(lines)]}
        df.update(norm for norm in seen if len(norm) >= MIN_LINE)
    need = threshold(len(rows))
    common = {norm for norm, count in df.items() if count >= need}
    image_rows = conn.execute(
        """
        SELECT i.content_hash, array_agg(DISTINCT i.article_id ORDER BY i.article_id) AS ids
        FROM article_images i JOIN articles a ON a.id = i.article_id
        WHERE i.source_id = %s AND a.duplicate_of IS NULL
        GROUP BY i.content_hash
        HAVING count(DISTINCT i.article_id) >= %s
        """,
        (source_id, IMAGE_MIN_DF),
    ).fetchall()
    batches = Counter(tuple(row["ids"]) for row in image_rows)
    images = {row["content_hash"] for row in image_rows if batches[tuple(row["ids"])] < IMAGE_BATCH}
    return Profile(common, images)


def profile_for(conn, source_id: str) -> Profile:
    kind = conn.execute("SELECT kind FROM sources WHERE id = %s", (source_id,)).fetchone()
    if kind is not None and kind["kind"] in LIBRARY_KINDS:
        return EMPTY
    signature = _signature(conn, source_id)
    with _lock:
        hit = _cache.get(source_id)
        if hit and hit[0] == signature:
            return hit[1]
    found = build_profile(conn, source_id)
    with _lock:
        _cache[source_id] = (signature, found)
    return found


def image_hashes(conn, article_id: int) -> dict[str, str]:
    rows = conn.execute(
        "SELECT original_ref, content_hash FROM article_images WHERE article_id = %s", (article_id,)
    ).fetchall()
    return {row["original_ref"]: row["content_hash"] for row in rows}


def clean_markdown(body_md: str, profile: Profile = EMPTY, hashes: dict[str, str] | None = None) -> str:
    """整篇 body_md（可能带 frontmatter）→ 清洗后的正文，不带 frontmatter。"""
    from researchlib.textutil import split_frontmatter

    _front, body = split_frontmatter(body_md or "")
    return clean_body(body, profile, hashes)


def clean_article(conn, article_id: int, source_id: str, body_md: str) -> str:
    return clean_markdown(body_md, profile_for(conn, source_id), image_hashes(conn, article_id))


# —— 全库重洗 ——


def rewash(conn, source_ids: list[str] | None = None, dry_run: bool = False) -> dict:
    """按当前规则重算 body_text 和原文段落块。body_md 不动。返回各来源改动篇数。"""
    from researchlib.chunks import replace_chunks
    from researchlib.textutil import markdown_to_text, strip_duplicate_title

    where = "WHERE duplicate_of IS NULL"
    params: list = []
    if source_ids:
        where += " AND source_id = ANY(%s)"
        params.append(source_ids)
    rows = conn.execute(
        f"SELECT id, source_id, title, body_md, body_text FROM articles {where} ORDER BY id", params
    ).fetchall()
    changed: Counter[str] = Counter()
    seen: Counter[str] = Counter()
    for index, row in enumerate(rows, 1):
        seen[row["source_id"]] += 1
        cleaned = clean_article(conn, row["id"], row["source_id"], row["body_md"])
        text = markdown_to_text(strip_duplicate_title(cleaned, row["title"]))
        if text == (row["body_text"] or ""):
            continue
        changed[row["source_id"]] += 1
        if dry_run:
            continue
        conn.execute("UPDATE articles SET body_text = %s WHERE id = %s", (text, row["id"]))
        replace_chunks(conn, row["id"], row["title"], row["body_md"])
        if index % 200 == 0:
            conn.commit()
    if not dry_run:
        conn.commit()
    return {"articles": dict(seen), "changed": dict(changed)}


def main(argv: list[str] | None = None) -> int:
    import argparse
    import json

    from researchlib.config import Settings
    from researchlib.db import prepare

    parser = argparse.ArgumentParser(description="按清洗规则重算全库文章的检索文字和段落块（body_md 不动）")
    parser.add_argument("--source", action="append", help="只洗这个来源，可重复")
    parser.add_argument("--dry-run", action="store_true", help="只数会改多少篇，不写库")
    args = parser.parse_args(argv)
    conn = prepare(Settings.from_env())
    try:
        report = rewash(conn, args.source, args.dry_run)
    finally:
        conn.close()
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
