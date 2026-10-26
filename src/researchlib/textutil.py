"""Markdown 清洗、日期和内容哈希。导入与提及统计共用。"""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

import yaml

SHANGHAI = ZoneInfo("Asia/Shanghai")
_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.S)
_HEADING = re.compile(r"^(#{1,6})\s+(\S.*?)\s*$", re.M)
_FENCE = re.compile(r"```.*?```", re.S)
_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_TAG = re.compile(r"<[^>]+>")
_EMPH = re.compile(r"(\*\*|__|\*|_|~~)")
# GitHub 提示框的标记。feishu2md 把飞书的高亮块导成引用块，第一行写「[!NOTE]」。
_ALERT = r"\[!(?:NOTE|TIP|IMPORTANT|WARNING|CAUTION)\]"
_ALERT_LINE = re.compile(r"^[ \t]{0,3}>[ \t]*" + _ALERT + r"[ \t]*$\n?", re.M | re.I)
_ALERT_LEAD = re.compile(r"^([ \t]{0,3}>[ \t]*)" + _ALERT + r"[ \t]*", re.M | re.I)
_FILENAME_DATE = re.compile(
    r"^(20\d{2})[-_.]?(0[1-9]|1[0-2])[-_.]?(0[1-9]|[12]\d|3[01])"
)
_FILENAME_LEAD = re.compile(
    r"^(20\d{2})[-_.年/](0?[1-9]|1[0-2])[-_.月/](0?[1-9]|[12]\d|3[01])日?(?!\d)"
)
_FILENAME_COMPACT = re.compile(r"^(20\d{2})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])(?!\d)")
_FILENAME_ANY = re.compile(
    r"(?<!\d)(20\d{2})[-_.年/](0?[1-9]|1[0-2])[-_.月/](0?[1-9]|[12]\d|3[01])日?(?!\d)"
)
_FILENAME_COMPACT_ANY = re.compile(r"(?<!\d)(20\d{2})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])(?!\d)")
_FLEX_DATE = re.compile(
    r"^(20\d{2})(?:[./]|年|-)(0?[1-9]|1[0-2])(?:[./]|月|-)(0?[1-9]|[12]\d|3[01])日?"
    r"(?:\s*([01]?\d|2[0-3])[:：]([0-5]\d)(?:[:：]([0-5]\d))?)?$"
)
_DATE_KEYS = (
    "date",
    "published_at",
    "published",
    "publish_time",
    "published_time",
    "pub_date",
    "pubDate",
    "datetime",
    "created",
    "created_at",
    "create_time",
    "createTime",
    "ctime",
)
_MARKUP_DATES = (
    re.compile(
        r"""<meta\b[^>]*\b(?:property|name|itemprop)\s*=\s*["']"""
        r"""(?:article:published_time|og:article:published_time|datePublished|publish_time)["']"""
        r"""[^>]*\bcontent\s*=\s*["']([^"']+)["']""",
        re.I,
    ),
    re.compile(
        r"""<meta\b[^>]*\bcontent\s*=\s*["']([^"']+)["'][^>]*\b(?:property|name|itemprop)\s*=\s*["']"""
        r"""(?:article:published_time|og:article:published_time|datePublished|publish_time)["']""",
        re.I,
    ),
    re.compile(
        r"""<(?:em|span|time)\b[^>]*\bid\s*=\s*["']publish_time["'][^>]*>\s*([^<]{4,40}?)\s*</""",
        re.I,
    ),
    re.compile(r'"datePublished"\s*:\s*"([^"]+)"', re.I),
)
# 日期字符串里出现时分，才算「知道钟点」。纯日历日解析出来的 00:00 不是发布时间。
_CLOCK_IN_TEXT = re.compile(r"(?:T|\s|日)(?:[01]?\d|2[0-3])[:：][0-5]\d")


def split_frontmatter(text: str) -> tuple[dict, str]:
    match = _FRONTMATTER.match(text)
    if not match:
        return {}, text
    try:
        data = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError:
        return {}, text
    if not isinstance(data, dict):
        return {}, text
    return data, text[match.end() :]


def normalize_newlines(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    lines = [line.rstrip() for line in text.split("\n")]
    return "\n".join(lines).strip()


def content_hash(title: str, body_md: str, published_at: datetime | None) -> str:
    """Stable hash across DB round-trips.

    Ingest keeps ``published_at`` in Asia/Shanghai. Postgres stores timestamptz
    and drivers return the same instant in UTC, so a naive ``isoformat()`` of
    the value read back differs from the stamp used at insert. Always stamp in
    Shanghai so translate / digest pending checks keep matching ``articles``.
    """
    if published_at is None:
        stamp = ""
    else:
        stamp = published_at.astimezone(SHANGHAI).isoformat()
    payload = f"{title}\n{stamp}\n{normalize_newlines(body_md)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def first_heading(md: str) -> str | None:
    match = _HEADING.search(md)
    if not match:
        return None
    return match.group(2).strip()


def strip_duplicate_title(md: str, title: str) -> str:
    match = re.match(r"\s*#\s+(.+?)\s*(?:\n|$)", md)
    if match and match.group(1).strip() == title.strip():
        return md[match.end() :]
    return md


_FILE_URL = re.compile(r"file:///[^\s<>\"')\]]*", re.I)
_MD_FILE_LINK = re.compile(r"\[([^\]]*)\]\(\s*file:///[^)]*\)", re.I)
_BARE_PAGE_FILE = re.compile(r"(?<![A-Za-z0-9./])H\.HTM(?:#[^\s<>\"')\]]*)?", re.I)
_EMPTY_PARENS = re.compile(r"[（(]\s*[）)]")


def scrub_file_page_junk(text: str) -> str:
    """去掉 PDF 页内链接留下的 `file:///H.HTM` 一类页码垃圾。没有这类字样时原样返回。"""
    if not text:
        return text or ""
    lowered = text.lower()
    if "file:" not in lowered and "h.htm" not in lowered:
        return text

    def link(match: re.Match[str]) -> str:
        label = (match.group(1) or "").strip()
        if re.fullmatch(r"\d{1,4}", label):
            return ""
        return label

    lines: list[str] = []
    for line in text.split("\n"):
        cleaned = _MD_FILE_LINK.sub(link, line)
        cleaned = _FILE_URL.sub("", cleaned)
        cleaned = _BARE_PAGE_FILE.sub("", cleaned)
        cleaned = _EMPTY_PARENS.sub("", cleaned)
        cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
        cleaned = re.sub(r"\s+([，。！？、；：])", r"\1", cleaned).rstrip()
        if not cleaned.strip() and (_FILE_URL.search(line) or _BARE_PAGE_FILE.search(line)):
            continue
        lines.append(cleaned)
    return "\n".join(lines)


def strip_alert_markers(md: str) -> str:
    """去掉引用块开头的「[!NOTE]」这类标记，引用块照留。标记不是正文，留着卡片导读会以「NOTE]」开头。"""
    if "[!" not in (md or ""):
        return md or ""
    return _ALERT_LEAD.sub(r"\1", _ALERT_LINE.sub("", md))


def markdown_to_text(md: str) -> str:
    text = _FENCE.sub(" ", strip_alert_markers(md))
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = _IMAGE.sub(r"\1", text)
    text = _LINK.sub(r"\1", text)
    text = _TAG.sub(" ", text)
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.M)
    text = _EMPH.sub("", text)
    text = re.sub(r"^\s{0,3}>\s?", "", text, flags=re.M)
    text = re.sub(r"^\s*[-+*]\s+", "", text, flags=re.M)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def split_markdown_sections(md: str) -> tuple[list[str], bool]:
    md = md.strip()
    if not md:
        return [], False
    indexes = [match.start() for match in _HEADING.finditer(md)]
    if not indexes:
        return [md], False
    parts: list[str] = []
    if indexes[0] > 0:
        parts.append(md[: indexes[0]])
    for index, start in enumerate(indexes):
        end = indexes[index + 1] if index + 1 < len(indexes) else len(md)
        parts.append(md[start:end])
    cleaned = [part.strip() for part in parts if part.strip()]
    return cleaned, True


def section_plain_texts(md: str) -> tuple[list[str], bool]:
    sections, structured = split_markdown_sections(md)
    return [markdown_to_text(section) for section in sections if section.strip()], structured


def _shanghai_clock(year, month, day, hour=0, minute=0, second=0) -> datetime | None:
    try:
        return datetime(
            int(year),
            int(month),
            int(day),
            int(hour or 0),
            int(minute or 0),
            int(second or 0),
            tzinfo=SHANGHAI,
        )
    except (TypeError, ValueError):
        return None


def _flexible_date(text: str) -> datetime | None:
    match = _FLEX_DATE.fullmatch(text.strip())
    if not match:
        return None
    year, month, day, hour, minute, second = match.groups()
    return _shanghai_clock(year, month, day, hour, minute, second)


def parse_datetime(value) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=SHANGHAI)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=SHANGHAI)
    if isinstance(value, (int, float)) or (isinstance(value, str) and str(value).strip().isdigit()):
        number = int(value)
        if number > 10_000_000_000:
            number = number / 1000
        if number > 1_000_000_000:
            return datetime.fromtimestamp(number, tz=SHANGHAI)
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    if re.search(r"[+-]\d{4}$", text):
        text = text[:-2] + ":" + text[-2:]
    # 只去掉秒后面的小数，避免把 2024.5.1 这种点分日期吃掉。
    text = re.sub(r"(\d{2}:\d{2}:\d{2})\.\d+", r"\1", text)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        parsed = None
    if parsed is None:
        parsed = _flexible_date(text)
    if parsed is None:
        for fmt in (
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y-%m-%d",
            "%Y/%m/%d %H:%M:%S",
            "%Y/%m/%d %H:%M",
            "%Y/%m/%d",
            "%Y.%m.%d %H:%M:%S",
            "%Y.%m.%d %H:%M",
            "%Y.%m.%d",
            "%Y%m%d",
        ):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        if parsed is None:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SHANGHAI)
    return parsed


def basename(name: str) -> str:
    return name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]


def date_from_filename(name: str) -> datetime | None:
    base = basename(name or "")
    if not base:
        return None
    patterns = (_FILENAME_LEAD, _FILENAME_COMPACT, _FILENAME_ANY, _FILENAME_COMPACT_ANY)
    for pattern in patterns:
        for match in pattern.finditer(base):
            parsed = _shanghai_clock(*match.groups()[:3])
            if parsed:
                return parsed
    return None


def clock_known_in(value) -> bool:
    """原始值里有没有时分。datetime 算有；纯 date、以及只有年月日的字符串不算。"""
    if value is None or value == "":
        return False
    if isinstance(value, datetime):
        return True
    if isinstance(value, date):
        return False
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        number = int(value)
        return abs(number) > 1_000_000_000
    text = str(value).strip()
    if text.lstrip("-").isdigit():
        return abs(int(text)) > 1_000_000_000
    return _CLOCK_IN_TEXT.search(text) is not None


def _markup_raw(text: str) -> str | None:
    head = (text or "")[:12000]
    if not head:
        return None
    for pattern in _MARKUP_DATES:
        match = pattern.search(head)
        if not match:
            continue
        raw = match.group(1).strip()
        if parse_datetime(raw):
            return raw
    return None


# 知识星球 Markdown 常在开头写「- 时间：2024-11-28 10:40:22」。只认前几行的列表项，避免正文里的「时间」被当成发布时间。
_BODY_TIME = re.compile(r"(?m)^[ \t]*[-*][ \t]+时间[ \t]*[:：][ \t]*([^\n]{4,60})")


def _body_time_raw(text: str) -> str | None:
    head = "\n".join((text or "").splitlines()[:15])
    match = _BODY_TIME.search(head)
    if not match:
        return None
    raw = match.group(1).strip().split("（", 1)[0].strip()
    if parse_datetime(raw):
        return raw
    return None


def date_from_markup(text: str) -> datetime | None:
    raw = _markup_raw(text)
    if raw is None:
        return None
    return parse_datetime(raw)


def dates_from_mapping(mapping: dict | None) -> datetime | None:
    found = _mapping_clock(mapping)
    if found is None:
        return None
    return found[0]


def _mapping_clock(mapping: dict | None) -> tuple[datetime, bool] | None:
    if not isinstance(mapping, dict):
        return None
    for key in _DATE_KEYS:
        if key not in mapping:
            continue
        raw = mapping.get(key)
        parsed = parse_datetime(raw)
        if parsed:
            return parsed, clock_known_in(raw)
    return None


def resolve_published_at(
    *,
    frontmatter: dict | None = None,
    hints: dict | None = None,
    filename: str = "",
    text: str = "",
    extra_filenames: tuple[str, ...] = (),
) -> tuple[datetime | None, str, bool]:
    """按 frontmatter、元数据、正文标记、「- 时间：」、文件名的顺序取发布时间。

    第三项表示原始值里有没有时分。只有日历日时，如果后面的来源在同一天带了钟点
    （例如前言只写了日期，知识星球 JSON 里有 create_time），用那个钟点，避免把
    13:29 收成 00:00。
    """
    candidates: list[tuple[datetime, str, bool]] = []
    for mapping, label in ((frontmatter, "frontmatter"), (hints, "meta")):
        found = _mapping_clock(mapping)
        if found:
            candidates.append((found[0], label, found[1]))
    raw_markup = _markup_raw(text)
    if raw_markup:
        parsed = parse_datetime(raw_markup)
        if parsed:
            candidates.append((parsed, "markup", clock_known_in(raw_markup)))
    raw_body = _body_time_raw(text)
    if raw_body:
        parsed = parse_datetime(raw_body)
        if parsed:
            candidates.append((parsed, "body", clock_known_in(raw_body)))
    for name in (filename, *extra_filenames):
        found = date_from_filename(name)
        if found:
            candidates.append((found, "filename", False))
            break
    if not candidates:
        return None, "", False
    when, source, known = candidates[0]
    if known:
        return when, source, True
    top_day = when.astimezone(SHANGHAI).date()
    for later, later_source, later_known in candidates[1:]:
        if later_known and later.astimezone(SHANGHAI).date() == top_day:
            return later, later_source, True
    if source == "filename":
        for later, later_source, later_known in candidates[1:]:
            if later_known:
                return later, later_source, True
    return when, source, False


def title_from_filename(name: str) -> str:
    stem = basename(name)
    if stem.lower().endswith(".md"):
        stem = stem[:-3]
    elif stem.lower().endswith(".markdown"):
        stem = stem[:-9]
    stem = _FILENAME_DATE.sub("", stem).strip(" _-.")
    stem = stem.replace("_", " ").strip()
    return stem or basename(name)


def derive_title(frontmatter: dict, body: str, filename: str, extra: str | None = None) -> str:
    for key in ("title", "name"):
        value = frontmatter.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:300]
    if extra and extra.strip():
        return extra.strip()[:300]
    heading = first_heading(body)
    if heading:
        return heading[:300]
    for line in body.splitlines():
        cleaned = line.strip().lstrip("#").strip()
        if cleaned:
            return cleaned[:80]
    return title_from_filename(filename)[:300] or "未命名"


def excerpt(text: str, limit: int = 120, title: str = "") -> str:
    flat = re.sub(r"\s+", " ", text).strip()
    if title and flat.startswith(title):
        flat = flat[len(title) :].strip()
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1] + "…"


# 知识星球、公众号归档常把 sn、点赞、作者、时间、原链接贴在正文开头。卡片不要拿这些当导读。
_META_KEY = (
    r"sn|likes?|like_count|comments?|comment_count|rewards?|readers?|views?|view_count|"
    r"create_time|created_at|topic_id|digested|reading_count|favorites?|favs?"
)
_CLOCK = r"\d{1,2}[:：]\d{2}(?::\d{2})?"
_DATE = r"\d{4}[-/.年]\d{1,2}[-/.月]\d{1,2}日?(?:\s+" + _CLOCK + r")?"
_COUNT = r"(?:点赞|赞|评论|回复|阅读|在看|打赏|收藏|浏览)\s*[:：]?\s*\d+"
_KEY_VALUE = r"(?:" + _META_KEY + r")\s*[:：]?\s*\d+"
_URL = r"(?:https?://|www\.)[^\s。！？!?，,|｜]+"
_LEADING_META = re.compile(
    r"^(?:" + "|".join((_DATE, _CLOCK, _KEY_VALUE, _COUNT, _URL)) + r")(?:\s+|$)",
    re.I,
)
_META_LINE = re.compile(
    r"^(?:"
    + "|".join((r"(?:" + _META_KEY + r")\s*[:：]?\s*\S+", _COUNT, _DATE, _CLOCK, _URL))
    + r")\s*$",
    re.I,
)
# 长的放前面，避免「时间」吃掉「发布时间」。
_LABEL_KEY = (
    r"发布时间|创建时间|原文链接|原链接|阅读量|公众号|作者|时间|日期|链接|来源|出处|"
    r"点赞|评论|阅读|在看|转发|author|source|url|link"
)
_LABEL_VALUE = r"(?:https?://[^\s。！？!?，,|｜]+|(?:[^\s。！？!?，,|｜]){1,40}(?:\s+" + _CLOCK + r")?)"
_LABEL_PREFIX = re.compile(
    r"^(?P<key>" + _LABEL_KEY + r")\s*[:：]\s*(?P<value>" + _LABEL_VALUE + r")",
    re.I,
)
_GENERIC_PREFIX = re.compile(r"^([\w\u4e00-\u9fff]{1,12})\s*[:：]\s*(\S+)")
_FIELD_SEPARATORS = " |｜·•,，;；、/／"
_NAME_KEYS = frozenset({"作者", "来源", "出处", "公众号", "author", "source"})
_TIME_KEYS = frozenset({"时间", "发布时间", "创建时间", "日期"})
_LINK_KEYS = frozenset({"原链接", "原文链接", "链接", "url", "link"})
_COUNT_KEYS = frozenset({"点赞", "评论", "阅读", "阅读量", "在看", "转发"})
_SENTENCE_END = "。！？!?，,"


def _norm_title(text: str) -> str:
    return re.sub(r"[\s《》「」\"'“”‘’]", "", text or "")


def _label_match(text: str) -> re.Match[str] | None:
    match = _LABEL_PREFIX.match(text)
    if not match:
        return None
    key = match.group("key")
    value = match.group("value").strip()
    folded = key.lower()
    end = match.end()
    if end < len(text) and text[end] in _SENTENCE_END:
        return None
    if folded in _LINK_KEYS or key in _LINK_KEYS:
        if re.search(r"[。！？!?]", value):
            return None
        if not re.match(r"https?://|www\.", value, re.I) and len(value) > 16:
            return None
        return match
    if key in _TIME_KEYS:
        if not re.fullmatch(r"[\d\s:：./\-年月日时分秒]+", value):
            return None
        return match
    if key in _COUNT_KEYS:
        if not re.fullmatch(r"[\d\s,，]+", value):
            return None
        return match
    if folded in _NAME_KEYS or key in _NAME_KEYS:
        if re.search(r"[。！？!?，,]", value) or len(value) > 24:
            return None
        return match
    return None


def _generic_meta_match(text: str) -> re.Match[str] | None:
    """开头的 key: value，值要像链接、数字、日期或英文 id，不能是一句中文判断。"""
    match = _GENERIC_PREFIX.match(text)
    if not match:
        return None
    value = match.group(2)
    if re.search(r"[。！？!?，,]", value):
        return None
    end = match.end()
    if end < len(text) and text[end] in _SENTENCE_END:
        return None
    if re.fullmatch(r"https?://\S+|www\.\S+", value, re.I):
        return match
    if re.fullmatch(r"[\d\s:：./\-年月日时分秒]+", value):
        return match
    if re.fullmatch(r"[A-Za-z0-9_@./+-]{1,64}", value):
        return match
    return None


def _strip_leading_meta(text: str) -> str:
    current = text.strip()
    while current:
        stripped = current.lstrip(_FIELD_SEPARATORS).strip()
        if stripped != current:
            current = stripped
            continue
        match = _LEADING_META.match(current) or _label_match(current) or _generic_meta_match(current)
        if not match or match.end() == 0:
            break
        current = current[match.end() :].strip()
    return current


def _strip_title_label(line: str, title: str) -> str:
    """归档头里的「标题：…」在重复文章标题时丢掉；带句号的判断句留下。"""
    match = re.match(r"^标题\s*[:：]\s*(.*)$", line)
    if not match:
        return line
    value = match.group(1).strip()
    title = (title or "").strip()
    if title:
        bare = re.sub(r"^[《「\"'“]+", "", value)
        for candidate in (value, bare):
            if candidate.startswith(title):
                return candidate[len(title) :].strip(" ：:|-》」\"'”")
        if _norm_title(value) == _norm_title(title):
            return ""
    if re.search(r"[。！？!?]", value):
        return line
    if not title and len(value) <= 80:
        return ""
    return line


def argumentative_excerpt(text: str, limit: int = 180, title: str = "") -> str:
    """从正文前部取一句实质内容。

    跳过 sn、点赞、钟点，以及公众号 / 知识星球开头的作者、时间、原链接、
    来源、重复标题和只有 URL 的行。没有实质内容就返回空串。
    """
    kept: list[str] = []
    for raw_line in (text or "").splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip()
        if title and line.startswith(title):
            line = line[len(title) :].strip(" ：:|-")
        line = _strip_title_label(line, title)
        line = _strip_leading_meta(line)
        if not line or _META_LINE.match(line):
            continue
        if title and line == title.strip():
            continue
        kept.append(line)
    blob = _strip_leading_meta(" ".join(kept))
    if title and blob.startswith(title):
        blob = _strip_leading_meta(blob[len(title) :].strip())
    blob = _strip_title_label(blob, title)
    blob = _strip_leading_meta(blob)
    if not blob:
        return ""
    parts = re.split(r"(?<=[。！？!?])\s*", blob)
    chosen = ""
    for part in parts:
        part = _strip_leading_meta(part.strip())
        if not part or _META_LINE.match(part) or len(part) < 8:
            continue
        chosen = part
        break
    if not chosen:
        chosen = blob
    if len(chosen) <= limit:
        return chosen
    return chosen[: limit - 1] + "…"
