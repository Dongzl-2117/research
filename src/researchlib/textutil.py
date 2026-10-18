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
    stamp = published_at.isoformat() if published_at else ""
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


def markdown_to_text(md: str) -> str:
    text = _FENCE.sub(" ", md)
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


def date_from_markup(text: str) -> datetime | None:
    head = (text or "")[:12000]
    if not head:
        return None
    for pattern in _MARKUP_DATES:
        match = pattern.search(head)
        if not match:
            continue
        parsed = parse_datetime(match.group(1).strip())
        if parsed:
            return parsed
    return None


def dates_from_mapping(mapping: dict | None) -> datetime | None:
    if not isinstance(mapping, dict):
        return None
    for key in _DATE_KEYS:
        if key not in mapping:
            continue
        parsed = parse_datetime(mapping.get(key))
        if parsed:
            return parsed
    return None


def resolve_published_at(
    *,
    frontmatter: dict | None = None,
    hints: dict | None = None,
    filename: str = "",
    text: str = "",
    extra_filenames: tuple[str, ...] = (),
) -> tuple[datetime | None, str]:
    """按 frontmatter、元数据、正文标记、文件名的顺序取发布时间。"""
    found = dates_from_mapping(frontmatter)
    if found:
        return found, "frontmatter"
    found = dates_from_mapping(hints)
    if found:
        return found, "meta"
    found = date_from_markup(text)
    if found:
        return found, "markup"
    for name in (filename, *extra_filenames):
        found = date_from_filename(name)
        if found:
            return found, "filename"
    return None, ""


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
