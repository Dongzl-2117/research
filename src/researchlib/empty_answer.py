"""段永平问答里，他本人那一节只剩论坛导航时，整篇不进列表。

「回本文开头」、署名加时间、上下箭头，都不是他的话。读者提问里点了公司名也不算。
整篇没有「段永平」标题、正文只有这一行导航的，同样搁下。只处理段永平博客。
文章行留着，直接打开还能看；首页按天、作者时间线、检索和公司页不收。
"""

from __future__ import annotations

import logging
import re
import unicodedata

from psycopg.types.json import Jsonb

from researchlib.ingest.duanyongping import SOURCE_ID

logger = logging.getLogger("researchlib.empty_answer")

# 和「没有段永平本人的话」同一道门槛：洗掉导航后剩下不到 6 个字，就不算他说了什么。
MIN_VOICE = 6
SUPPRESSED = "suppressed"
EMPTY_ANSWER = "empty_answer"
_OFF_VIEWS = "off_views"

_FOLD = str.maketrans(
    {
        "０": "0",
        "１": "1",
        "２": "2",
        "３": "3",
        "４": "4",
        "５": "5",
        "６": "6",
        "７": "7",
        "８": "8",
        "９": "9",
        "：": ":",
        "－": "-",
        "—": "-",
        "–": "-",
        "　": " ",
    }
)
# 字和字之间可以有空格。康熙部首先折成普通汉字，这里仍按「文」匹配。
_NAV = (
    r"(?:回\s*(?:本\s*)?文\s*开\s*头|"
    r"返\s*回\s*(?:文\s*章\s*|本\s*文\s*)?开\s*头|"
    r"回\s*到\s*(?:文\s*章\s*|本\s*文\s*)?开\s*头|"
    r"回\s*顶\s*部|返\s*回\s*顶\s*部|回\s*到\s*顶\s*部|返\s*回\s*页\s*首)"
)
_ARROWS = r"↑↓⬆⬇⇧⇩^"
_NAV_RE = re.compile(_NAV)
_NAV_LINE = re.compile(rf"^(?:{_NAV}|[{_ARROWS}]+)\s*[{_ARROWS}]*$")
_NAV_TAIL = re.compile(rf"(?:\s*{_NAV})?\s*[{_ARROWS}]+\s*$|\s*{_NAV}\s*$")
# 署名可以是「千里牛」，也可以是「尼古拉 ·特斯拉」这种带间隔号和空格的。
_ATOM = r"[^\s。！？!?，,；;：:]{1,24}"
_NAME = rf"{_ATOM}(?:\s*[·•・‧]\s*{_ATOM}){{0,3}}"
_SIG = re.compile(
    rf"^(?:{_NAME}\s+)?"
    rf"\d{{4}}\s*[-/.年]\s*\d{{1,2}}\s*[-/.月]\s*\d{{1,2}}\s*日?"
    rf"(?:\s+\d{{1,2}}\s*[:]\s*\d{{2}}(?:\s*[:]\s*\d{{2}})?)?"
    rf"(?:\s*{_NAV})?"
    rf"\s*[{_ARROWS}]*$"
)
_DUAN_HEADING = re.compile(r"(?m)^##[ \t]+段永平[ \t]*$")
_NEXT_H2 = re.compile(r"(?m)^##[ \t]+\S")
_FRONT = re.compile(r"\A---[ \t]*\n.*?\n---[ \t]*\n?", re.S)
_HEADING_MARK = re.compile(r"^#{1,6}[ \t]+")
_RULE = re.compile(r"[-*_]{3,}")
# 零宽字符和变体选择符。夹在「回本文开头」中间时，整行就对不上导航。
_INVISIBLE = re.compile(r"[\u00ad\u200b\u200c\u200d\u2060\ufeff\ufe00-\ufe0f]")
# 启动回扫用。导航词中间可能是空格或康熙部首，不要求「回本文开头」连着写。
_SCAN_LIKE = ("%## 段永平%", "%回%开%头%", "%回%顶部%", "%页首%", "%\u2f42%")


def visible_sql(alias: str = "") -> str:
    """空回答整篇不进列表。标记只写在段永平博客上，别的来源没有这一栏，不受影响。"""
    column = f"{alias}.raw_meta" if alias else "raw_meta"
    return f"coalesce({column}->>'{SUPPRESSED}', '') = ''"


def is_suppressed(raw_meta: dict | None) -> bool:
    return bool(str((raw_meta or {}).get(SUPPRESSED) or "").strip())


def normalize_voice(text: str) -> str:
    """兼容汉字和康熙部首折成普通字，再去掉汉字缝、间隔号两边的空格。PDF 抽出来常有这种缝。"""
    text = unicodedata.normalize("NFKC", text or "")
    text = _INVISIBLE.sub("", text)
    text = text.translate(_FOLD)
    text = re.sub(r"\s*([·•・‧])\s*", r"\1", text)
    text = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", text)
    text = re.sub(r"\s+([，。！？；：、])", r"\1", text)
    text = re.sub(r"([（【《「『])\s+", r"\1", text)
    text = re.sub(r"\s+([）】》」』])", r"\1", text)
    return text


def _flat_line(line: str) -> str:
    return re.sub(r"\s+", " ", normalize_voice(line)).strip()


def is_chrome_line(line: str) -> bool:
    """这一行只是论坛导航：回本文开头、署名加时间，或一个箭头。"""
    text = _flat_line(line)
    if not text:
        return True
    if _NAV_LINE.match(text) or _SIG.match(text):
        return True
    stripped = _NAV_TAIL.sub("", text).strip()
    return not stripped or bool(_NAV_LINE.match(stripped) or _SIG.match(stripped))


def substantive_voice(text: str) -> str:
    """去掉论坛导航之后，还剩下的话。一行一个段落。"""
    kept: list[str] = []
    for raw in re.split(r"\n+", normalize_voice(text or "")):
        line = re.sub(r"\s+", " ", raw).strip()
        if not line or is_chrome_line(line):
            continue
        line = _NAV_TAIL.sub("", line).strip()
        line = re.sub(r"\s+", " ", line).strip()
        if not line or is_chrome_line(line):
            continue
        kept.append(line)
    return "\n\n".join(kept)


def voice_is_empty(text: str) -> bool:
    return len(re.sub(r"\s+", "", substantive_voice(text))) < MIN_VOICE


def duan_section(body_md: str) -> str | None:
    """``## 段永平`` 下面的正文。没有这个标题就返回 None。"""
    match = _DUAN_HEADING.search(body_md or "")
    if not match:
        return None
    rest = body_md[match.end() :]
    nxt = _NEXT_H2.search(rest)
    body = rest[: nxt.start()] if nxt else rest
    return body.strip()


def _content_lines(body_md: str) -> list[str]:
    """去掉题头和空行之后的正文行。单独的分隔线不算正文。"""
    text = (body_md or "").replace("\r\n", "\n").replace("\r", "\n")
    match = _FRONT.match(text)
    if match:
        text = text[match.end() :]
    lines: list[str] = []
    for raw in re.split(r"\n+", text):
        line = _HEADING_MARK.sub("", raw).strip()
        if not line or _RULE.fullmatch(line):
            continue
        lines.append(line)
    return lines


def _nav_shell(body_md: str) -> bool:
    """整篇只剩署名、日期、回本文开头和箭头。有一句别的话就不是。"""
    lines = _content_lines(body_md)
    if not lines or not all(is_chrome_line(line) for line in lines):
        return False
    return bool(_NAV_RE.search(_flat_line(" ".join(lines))))


def stored_answer_empty(body_md: str, *, source_id: str = "", title: str = "") -> bool:
    """段永平那节没有正文，或整篇只剩论坛导航。

    整篇导航只对段永平博客成立。没传 source_id 时也认，回扫和单测都是这个来源。
    正文是空的、标题本身是导航时，同样算。
    """
    section = duan_section(body_md)
    if section is not None:
        return voice_is_empty(section)
    if source_id not in ("", SOURCE_ID):
        return False
    if _nav_shell(body_md):
        return True
    if title and not _content_lines(body_md):
        return _nav_shell(title)
    return False


def hide_empty_answer(
    conn, article_id: int, body_md: str, raw_meta, *, source_id: str, title: str = ""
) -> bool:
    """空回答标上标记，摘掉公司挂靠和观点。正文不删。已经标过的不再改。

    只处理段永平博客。方伟这类来源的读者帖即使正文里出现同样的标题，也不搁下。
    """
    if source_id != SOURCE_ID or not stored_answer_empty(body_md, source_id=source_id, title=title):
        return False
    meta = dict(raw_meta or {})
    if meta.get(SUPPRESSED) != EMPTY_ANSWER or meta.get(_OFF_VIEWS) is not True:
        meta[SUPPRESSED] = EMPTY_ANSWER
        meta[_OFF_VIEWS] = True
        conn.execute(
            "UPDATE articles SET raw_meta = %s WHERE id = %s",
            (Jsonb(meta), article_id),
        )
    conn.execute("DELETE FROM article_mentions WHERE article_id = %s", (article_id,))
    conn.execute("DELETE FROM company_views WHERE article_id = %s", (article_id,))
    return True


def backfill_empty_answers(conn) -> int:
    """启动时扫一遍段永平博客。还没搁下的，段永平那节是空的，或整篇只剩论坛导航，都标上。"""
    clauses: list[str] = []
    params: list[str] = [SOURCE_ID]
    for column in ("body_md", "title"):
        for pattern in _SCAN_LIKE:
            clauses.append(f"{column} LIKE %s")
            params.append(pattern)
    rows = conn.execute(
        f"""
        SELECT id, title, body_md, raw_meta
        FROM articles
        WHERE source_id = %s
          AND coalesce(raw_meta->>'suppressed', '') = ''
          AND ({" OR ".join(clauses)})
        ORDER BY id
        """,
        params,
    ).fetchall()
    hidden = 0
    for row in rows:
        if hide_empty_answer(
            conn,
            row["id"],
            row["body_md"] or "",
            row["raw_meta"],
            source_id=SOURCE_ID,
            title=row["title"] or "",
        ):
            hidden += 1
    if hidden:
        logger.info("段永平空回答已搁下 %s 篇", hidden)
    conn.commit()
    return hidden
