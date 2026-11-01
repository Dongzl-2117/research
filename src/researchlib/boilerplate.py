"""正文清洗：归档头、公众号和星球的引流尾巴、广告图。

库里的 body_md 保持导入时的原样（改稿、删稿核对都靠它），清洗只发生在两处：

- 文章页显示正文之前；
- 算 body_text 和段落块之前（检索、摘要、观点读的都是清洗后的文字）。

三条规则，都按同一来源多篇文章的统计来判，不写死某个作者的话术：

1. 归档头：正文开头那几行「- 作者：」「- 时间：」「- sn：」「- 原链接：」「- 原始数据：」
   「- 类型：」「- 点赞 · 评论 · 阅读人数」「- topic_id：」「- rss_link：」，整块去掉。
2. 模板句：同一来源里在很多篇（至少 max(5, 2%) 篇）都出现的整行。文末从第一行模板句起，
   后面几乎全是模板句、引流关键词或几个字的碎行时，整段切掉（招募被拆成一行几个字的也切得到）。
   正文中间的模板句只在带引流关键词时去掉；飞书那类重复的小标题不带关键词，留着。
3. 广告图：同一来源里至少 5 篇都出现的同一张图（按内容哈希），而且不是成批一起出现
   （飞书父文档会带上子文档的整批插图，那是正文）。二维码、优惠券海报、「长按关注」都是这种。
4. 文末号召：就算这句话没有在很多篇里重复，只要落在正文后面，并且是招募、二维码、
   长按关注、加微信这类号召，显示时整段拿掉。文件名或替代文字像二维码的图也拿掉。
   飞书笔记不走这一条，只去归档头。
5. 星球长文的导出脚手架：「> 以下为话题摘要，全文见下方「长文」。」、后面的话题摘要
   （长文开头的截断，和长文重复）、「## 长文：标题」下一行的「原文链接 · 原始 HTML：raw/articles/…」。
   只留长文正文；摘要的加粗标题和长文标题不一样时留下标题。
"""

from __future__ import annotations

import math
import re
import threading
from collections import Counter
from dataclasses import dataclass, field

_KEEP = re.compile(r"[0-9A-Za-z一-鿿]+")
_IMAGE = re.compile(
    r"!\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)|<img\b[^>]*\bsrc\s*=\s*[\"']([^\"']+)[\"'][^>]*>", re.I
)
_HEADING = re.compile(r"^\s*#{1,6}\s+\S")
_META_KEYS = (
    r"作者|时间|发布时间|类型|点赞|sn|topic_id|原链接|原文链接|原始数据|来源|公众号|阅读人数|阅读|"
    r"ghid|appmsgid|position|author|url|source|rss_link"
)
_META_LINE = re.compile(r"^\s*[-*]\s*(?:" + _META_KEYS + r")\s*(?:[:：]|\s+\d|\s*·)", re.I)
# 带这些词的行在文末算引流。统计尾巴用它；单独出现在研究正文里（比如讨论扫码支付）不够切段。
PROMO = re.compile(
    r"加入(?:会员群|知识星球|星球|微信群|交流群|社群|我们)|知识星球|会员群|交流群|扫码|二维码|长按(?:识别|扫码|关注)|"
    r"白嫖|非诚勿扰|加我|加微信|微信号|私信我|优惠券|立减|免费赠送|直接购买|备注[:：]|点个?在看|点赞.{0,3}在看|"
    r"星标|关注(?:公众号|我们|本号)|转发.{0,4}朋友圈|欢迎(?:关注|转发)|赞赏|打赏|推荐阅读|往期(?:回顾|推荐|文章)"
)
# 不靠跨篇统计、也可以从文末拿掉的号召。不收单独的「扫码」「二维码」，避免误切支付、行业正文。
_CTA = re.compile(
    r"招募|加入(?:会员群|知识星球|星球|微信群|交流群|社群)|知识星球|会员群|"
    r"长按(?:识别|扫码|关注)|扫码关注|识别(?:图中)?二维码|"
    r"关注(?:公众号|本号)|加微信|加我(?:微信|[:：])|微信号|私信我(?!们)|"
    r"白嫖|非诚勿扰|点个?在看|点赞.{0,4}在看|设为星标|"
    r"赞赏码|请赞赏|打赏|推荐阅读|往期(?:回顾|推荐)|"
    r"欢迎关注(?:公众号|本号)|欢迎转发|欢迎加入"
)
_QR_NAME = re.compile(r"二维码|qrcode|qr[-_ ]?code|(?:^|[\\/_.-])qr(?:[\\/_.-]|$)", re.I)
_MD_IMAGE_FULL = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_HTML_IMAGE_TAG = re.compile(r"<img\b[^>]*>", re.I)
_QR_WIDGET = re.compile(r"<mp-common-qrcode\b[^>]*>(?:.*?</mp-common-qrcode>)?", re.I | re.S)
_HTML_ATTR = re.compile(r"""\b([A-Za-z_:][-A-Za-z0-9_:.]*)\s*=\s*(["'])(.*?)\2""", re.I)
_HTML_BLOCK = re.compile(
    r"<p\b[^>]*>.*?</p>|<h[1-6]\b[^>]*>.*?</h[1-6]>|<figure\b[^>]*>.*?</figure>|<img\b[^>]*>",
    re.I | re.S,
)
_BARE_META = re.compile(r"^\s*(?:" + _META_KEYS + r")\s*[:：]\s*\S.{0,160}\s*$", re.I)
_ONLY_URL = re.compile(r"^\s*https?://\S+\s*$", re.I)
_READ_COUNT = re.compile(r"^\s*(?:阅读|在看|点赞|评论)\s*[:：]?\s*[\d,]+\s*$")
_RULE = re.compile(r"^[\s—\-–*·•_=~─]+$")
_SENTENCE_END = re.compile(r"[。！？!?]")
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


def _html_attr(tag: str, name: str) -> str:
    wanted = name.lower()
    for key, _quote, value in _HTML_ATTR.findall(tag):
        if key.lower() == wanted:
            return value
    return ""


def looks_like_qr(ref: str, alt: str = "") -> bool:
    """文件名或替代文字像公众号二维码。正文里讨论「二维码支付」不算。"""
    if _QR_NAME.search(alt or ""):
        return True
    name = (ref or "").split("?")[0].split("#")[0].rsplit("/", 1)[-1]
    return bool(name and _QR_NAME.search(name))


def _plain(line: str) -> str:
    return re.sub(r"^\s*#{1,6}\s*", "", line or "").strip()


def _is_archive_header_line(line: str) -> bool:
    """归档头：作者、时间、原链接、阅读数。带句号的判断句不是头。链接里的问号不算句号。"""
    stripped = line.strip()
    if not stripped:
        return False
    if not (_META_LINE.match(line) or _BARE_META.match(line) or _ONLY_URL.match(line) or _READ_COUNT.match(line)):
        return False
    if re.search(r"https?://", stripped):
        return True
    return not re.search(r"[。！？]", stripped)


def is_cta_line(line: str) -> bool:
    """招募、关注、加微信这类号召。一句带句号的研究判断里偶尔出现这些词，留下。"""
    text = _plain(line)
    if not text or not _CTA.search(text):
        return False
    compact = normalize(text)
    if _SENTENCE_END.search(text) and len(compact) > 18:
        return False
    if len(compact) <= 80:
        return True
    match = _CTA.search(text)
    return bool(match and match.start() <= 6 and len(compact) <= 120)


def _qr_only(line: str) -> bool:
    stripped = line.strip()
    if not _only_images(stripped):
        return False
    refs = image_refs(stripped)
    alts = _MD_IMAGE_FULL.findall(stripped)
    if alts and any(looks_like_qr(ref, alt) for alt, ref in alts):
        return True
    return bool(refs) and all(looks_like_qr(ref) for ref in refs)


def is_fragment(line: str) -> bool:
    """文末被拆开的半句。有句号、写完整了的短判断留下。"""
    text = _plain(line)
    if not text or _RULE.match(text):
        return True
    compact = normalize(text)
    if _SENTENCE_END.search(text):
        return len(compact) <= 8
    return len(compact) <= 40


def is_soft_tail(line: str) -> bool:
    text = line.strip()
    if not text or _RULE.match(text):
        return True
    if _only_images(text):
        return _qr_only(text)
    return is_cta_line(line) or is_fragment(line)


def strip_qr_images(text: str) -> str:
    """拿掉二维码图。插图的文件名不像二维码就留下。"""

    def markdown_image(match: re.Match[str]) -> str:
        if looks_like_qr(match.group(2), match.group(1)):
            return ""
        return match.group(0)

    def html_image(match: re.Match[str]) -> str:
        tag = match.group(0)
        src = _html_attr(tag, "data-src") or _html_attr(tag, "src")
        alt = _html_attr(tag, "alt")
        if looks_like_qr(src, alt):
            return ""
        return tag

    cleaned = _MD_IMAGE_FULL.sub(markdown_image, text or "")
    cleaned = _HTML_IMAGE_TAG.sub(html_image, cleaned)
    cleaned = _QR_WIDGET.sub("", cleaned)
    return cleaned


def cut_cta_tail(lines: list[str]) -> list[str]:
    """从最后一段实质正文之后，切掉号召、二维码和夹在中间的碎句。"""
    if not lines:
        return lines
    last_substantial = -1
    for index, line in enumerate(lines):
        text = _plain(line)
        if not text or is_cta_line(line) or _qr_only(line) or _only_images(line.strip()):
            continue
        if len(normalize(text)) >= 36:
            last_substantial = index
    if last_substantial < 0:
        region_start = max(0, len(lines) - max(12, len(lines) // 3))
    else:
        region_start = last_substantial + 1
    region = lines[region_start:]
    signal_at = None
    for index, line in enumerate(region):
        if is_cta_line(line) or _qr_only(line):
            signal_at = index
            break
    if signal_at is None:
        return lines
    cursor = signal_at
    while cursor > 0 and is_soft_tail(region[cursor - 1]):
        cursor -= 1
    cut = region_start + cursor
    if last_substantial >= 0 and cut <= last_substantial:
        cut = last_substantial + 1
    kept = lines[:cut]
    while kept and not kept[-1].strip():
        kept.pop()
    return kept


def _looks_like_html_body(text: str) -> bool:
    sample = (text or "").lstrip()[:500].lower()
    if sample.startswith(("#", "---", ">")):
        return False
    return sample.startswith(("<!doctype", "<html", "<body", "<div", "<p", "<section"))


def _block_text(block: str) -> str:
    return re.sub(r"<[^>]+>", "", block or "").strip()


def clean_html_document(html: str) -> str:
    """公众号 HTML：去掉开头的归档头、文末招募和二维码图。正文插图留下。"""
    html = strip_qr_images(html or "")
    blocks = list(_HTML_BLOCK.finditer(html))
    if len(blocks) < 2:
        return html
    kinds: list[str] = []
    for match in blocks:
        raw = match.group(0)
        text = _block_text(raw)
        has_img = bool(re.search(r"<img\b", raw, re.I))
        if has_img and not text:
            src = _html_attr(raw, "data-src") or _html_attr(raw, "src")
            alt = _html_attr(raw, "alt")
            kinds.append("qr" if looks_like_qr(src, alt) else "img")
        elif _is_archive_header_line(text):
            kinds.append("meta")
        elif is_cta_line(text):
            kinds.append("cta")
        elif not text:
            kinds.append("empty")
        else:
            kinds.append("text")
    drop: set[int] = set()
    started = False
    for index, kind in enumerate(kinds):
        if kind in {"meta", "empty"} and not started:
            if kind == "meta" or (kind == "empty" and not started):
                drop.add(index)
            continue
        if kind in {"text", "img", "cta", "qr"}:
            started = True
    last_text = -1
    for index, kind in enumerate(kinds):
        if kind != "text":
            continue
        text = _block_text(blocks[index].group(0))
        if len(normalize(text)) >= 36 and not is_cta_line(text):
            last_text = index
    if last_text < 0:
        tail_from = max(0, len(kinds) - max(8, len(kinds) // 3))
    else:
        tail_from = last_text + 1
    signal = None
    for index in range(tail_from, len(kinds)):
        if kinds[index] in {"cta", "qr"}:
            signal = index
            break
    if signal is not None:
        cursor = signal
        while cursor > tail_from and kinds[cursor - 1] in {"cta", "qr", "empty", "meta"}:
            cursor -= 1
        while cursor > tail_from and kinds[cursor - 1] == "text":
            if not is_fragment(_block_text(blocks[cursor - 1].group(0))):
                break
            cursor -= 1
        for index in range(cursor, len(kinds)):
            drop.add(index)
    if not drop:
        return re.sub(r"\n{3,}", "\n\n", html).strip("\n") + ("\n" if html.strip() else "")
    pieces: list[str] = []
    pos = 0
    for index, match in enumerate(blocks):
        if index in drop:
            pieces.append(html[pos : match.start()])
            pos = match.end()
    pieces.append(html[pos:])
    text = "".join(pieces)
    text = re.sub(r"\n{3,}", "\n\n", text).strip("\n")
    return text + ("\n" if text else "")


def strip_meta(body: str) -> str:
    """去掉开头的归档头。只看标题之后、正文第一句之前那一段。有没有列表横线都认。"""
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
        if _is_archive_header_line(line):
            continue
        started = True
        out.append(line)
    text = "\n".join(out)
    return re.sub(r"\n{3,}", "\n\n", text)


_LONGFORM_NOTE = re.compile(r"^\s*>\s*以下为话题摘要[，,]\s*全文见下方「长文」[。.]?\s*$")
_LONGFORM_HEADING = re.compile(r"^\s*##\s*长文\s*[:：]\s*(?P<title>\S.*?)\s*$")
# 「原文链接：https://articles.zsxq.com/… · 原始 HTML：[raw/articles/….html](../../raw/articles/….html)」
_LONGFORM_SOURCE = re.compile(r"^\s*(?:原文链接\s*[:：]\s*\S+\s*·\s*)?原始\s*HTML\s*[:：]", re.I)
_BOLD_LINE = re.compile(r"^\s*\*\*(?P<title>[^*]+)\*\*\s*$")


def drop_longform_scaffold(body: str) -> str:
    """星球「话题 + 长文」帖的导出脚手架（模块说明第 5 条）。没有「## 长文：」小标题时原样返回。"""
    lines = body.split("\n")
    heading = next((i for i, line in enumerate(lines) if _LONGFORM_HEADING.match(line)), None)
    if heading is None:
        return body
    title = _LONGFORM_HEADING.match(lines[heading]).group("title")
    note = next((i for i in range(heading) if _LONGFORM_NOTE.match(lines[i])), None)
    head = lines[:heading] if note is None else lines[:note]
    keep_heading = True
    if note is not None:
        preview = [line for line in lines[note + 1 : heading] if line.strip()]
        bold = _BOLD_LINE.match(preview[0]) if preview else None
        # 摘要的加粗标题就是长文标题：页面顶上已经是这个标题，小标题不再重复。
        keep_heading = not (bold and bold.group("title").strip() == title)
    rest = lines[heading + 1 :]
    first = next((i for i, line in enumerate(rest) if line.strip()), None)
    if first is not None and _LONGFORM_SOURCE.match(rest[first]):
        rest = rest[first + 1 :]
    if keep_heading:
        head = [*head, "", lines[heading]]
    return re.sub(r"\n{3,}", "\n\n", "\n".join([*head, "", *rest]))


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


def clean_body(
    body: str,
    profile: Profile = EMPTY,
    hashes: dict[str, str] | None = None,
    *,
    drop_cta: bool = True,
) -> str:
    """body 是去掉 frontmatter 之后的 Markdown。hashes：图片原始引用 → 内容哈希。

    drop_cta 为假时只去归档头和统计出来的模板（飞书笔记用这条）。
    """
    hashes = hashes or {}
    body = drop_longform_scaffold(strip_meta(body or ""))
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
    if drop_cta:
        text = strip_qr_images(text)
        text = "\n".join(cut_cta_tail(text.split("\n")))
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


def clean_markdown(
    body_md: str,
    profile: Profile = EMPTY,
    hashes: dict[str, str] | None = None,
    *,
    drop_cta: bool = True,
) -> str:
    """整篇 body_md（可能带 frontmatter）→ 清洗后的正文，不带 frontmatter。"""
    from researchlib.textutil import split_frontmatter

    _front, body = split_frontmatter(body_md or "")
    if drop_cta and _looks_like_html_body(body):
        return clean_html_document(body)
    return clean_body(body, profile, hashes, drop_cta=drop_cta)


def clean_article(conn, article_id: int, source_id: str, body_md: str) -> str:
    kind = conn.execute("SELECT kind FROM sources WHERE id = %s", (source_id,)).fetchone()
    drop_cta = kind is None or kind["kind"] not in LIBRARY_KINDS
    if not drop_cta:
        return clean_markdown(body_md, EMPTY, image_hashes(conn, article_id), drop_cta=False)
    return clean_markdown(body_md, profile_for(conn, source_id), image_hashes(conn, article_id), drop_cta=True)


def search_text(cleaned: str, title: str) -> str:
    """清洗后的正文 → articles.body_text（检索、摘要、观点、卡片摘录读的文字）。

    和标题重复的那行去掉；问答帖的「提问（网友 → 方伟Rey）」只留「提问」，网友的昵称不进来。
    Stratechery 每日更新开头的「Good morning,」「On to the Update:」和结尾的转发须知不进来（housekeeping）。
    """
    from researchlib.housekeeping import strip_housekeeping
    from researchlib.present_text import plain_qa_markdown
    from researchlib.textutil import markdown_to_text, strip_duplicate_title

    return markdown_to_text(plain_qa_markdown(strip_duplicate_title(strip_housekeeping(cleaned), title)))


# —— 全库重洗 ——


def rewash(conn, source_ids: list[str] | None = None, dry_run: bool = False) -> dict:
    """按当前规则重算 body_text 和原文段落块。body_md 不动。返回各来源改动篇数。"""
    from researchlib.chunks import replace_chunks

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
        text = search_text(cleaned, row["title"])
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
