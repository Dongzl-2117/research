"""段永平博客里，没有明确判断的文章不进列表。

- ``author_voice`` 是 all：整篇都是他的话。只剩导航，或不足 40 字又没有买、贵、持有、生意原则，
  标 no_judgment。长文留下。
- ``author_voice`` 是段永平：那一节只剩导航，标 empty_answer。不足 40 字又没有立场，标 weak_judgment。
  有立场的短帖留下。
- 英文粘贴问答标 ``author_voice=foreign``、``suppressed=misattributed``。

「回本文开头」、署名加时间、上下箭头，都不是他的话。康熙部首会先折成普通字。
正文留着，直接打开还能看；首页按天、作者时间线、检索和公司页不收。只处理这个来源。
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
# 段永平那一节去掉导航后，汉字和字母不足这么多，算没有明确判断。
MIN_JUDGMENT = 40
SUPPRESSED = "suppressed"
EMPTY_ANSWER = "empty_answer"
NO_JUDGMENT = "no_judgment"
WEAK_JUDGMENT = "weak_judgment"
MISATTRIBUTED = "misattributed"
VOICE_FOREIGN = "foreign"
_AUTHOR_VOICE = "author_voice"
_VOICE_ALL = "all"
_OFF_VIEWS = "off_views"
_OFF_TIMELINE = "off_timeline"

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


# 与 suppressed 分开的隐藏标记：source_paused（整源先不要）、short_post（短帖）。
# 段永平启动回扫只改 suppressed，不会把这一栏收回。清掉这个键即恢复。
HIDDEN = "hidden"
SOURCE_PAUSED = "source_paused"
SHORT_POST = "short_post"


def visible_sql(alias: str = "") -> str:
    """搁下或隐藏的整篇不进列表。"""
    column = f"{alias}.raw_meta" if alias else "raw_meta"
    return f"(coalesce({column}->>'{SUPPRESSED}', '') = '' AND coalesce({column}->>'{HIDDEN}', '') = '')"


def is_suppressed(raw_meta: dict | None) -> bool:
    meta = raw_meta or {}
    return bool(str(meta.get(SUPPRESSED) or "").strip() or str(meta.get(HIDDEN) or "").strip())


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


def judgment_len(text: str) -> int:
    """去掉论坛导航之后，还剩多少汉字和字母。"""
    return len(re.findall(r"[\u4e00-\u9fffA-Za-z]", substantive_voice(text or "")))


# 昵称里的「买」不是买入。先挖掉再看立场。
_NICKNAMES = ("木买蚂蚁",)
_STANCE = re.compile(
    r"买|卖|贵(?!州)|持有|拿着|拿住|不预测|生意模式|能力圈|安全边际|护城河|只买生意"
)


def has_stance(text: str) -> bool:
    """短帖里有没有买、卖、贵、持有或生意原则。木买蚂蚁这个昵称里的买不算。"""
    folded = normalize_voice(substantive_voice(text or "") or (text or ""))
    for name in _NICKNAMES:
        folded = folded.replace(name, "")
    return bool(_STANCE.search(folded))


def is_foreign_paste(text: str) -> bool:
    """几乎没有汉字、字母很多的英文粘贴。夹一个 iPhone 的中文判断不算。"""
    voice = substantive_voice(text or "")
    han = len(re.findall(r"[\u4e00-\u9fff]", voice))
    lat = len(re.findall(r"[A-Za-z]", voice))
    return lat >= MIN_JUDGMENT and han <= 8


def _article_plain(body_md: str) -> str:
    return "\n".join(_content_lines(body_md))


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


def _flag_on(meta: dict, key: str) -> bool:
    value = meta.get(key)
    return value is True or str(value or "").strip().lower() == "true"


def _voice_label(raw_meta) -> str:
    return str((raw_meta or {}).get(_AUTHOR_VOICE) or "").strip()


def suppression_for(body_md: str, raw_meta, *, source_id: str, title: str = "") -> str:
    """这段永平博客该搁下时，返回要写入的 suppressed。不用搁下就返回空串。

    author_voice 为 all 表示整篇都是他的话。长文留下。
    短帖没有买、贵、持有或生意原则才标 weak_judgment 或 no_judgment。
    英文粘贴问答标 misattributed。
    """
    if source_id != SOURCE_ID:
        return ""
    voice = _voice_label(raw_meta)
    if voice == VOICE_FOREIGN:
        return MISATTRIBUTED
    section = duan_section(body_md)
    if voice == _VOICE_ALL and section is None:
        plain = _article_plain(body_md)
        if is_foreign_paste(plain):
            return MISATTRIBUTED
        if _nav_shell(body_md) or voice_is_empty(plain):
            return NO_JUDGMENT
        if judgment_len(plain) < MIN_JUDGMENT and not has_stance(plain):
            return NO_JUDGMENT
        return ""
    if section is None:
        plain = _article_plain(body_md)
        if is_foreign_paste(plain):
            return MISATTRIBUTED
        if stored_answer_empty(body_md, source_id=source_id, title=title):
            return EMPTY_ANSWER
        return ""
    if voice_is_empty(section):
        return EMPTY_ANSWER
    if is_foreign_paste(section):
        return MISATTRIBUTED
    if judgment_len(section) < MIN_JUDGMENT and not has_stance(section):
        return WEAK_JUDGMENT
    return ""


def _restore_long_essay(meta: dict, body_md: str, published_at) -> bool:
    """先前把整篇博文一律标成 no_judgment 的，长文收回来。

    all 本来就是整篇都算他的话。有发布日期的，去掉当时补上的 off_timeline，回到按天。
    """
    if _voice_label(meta) != _VOICE_ALL:
        return False
    plain = _article_plain(body_md)
    if _nav_shell(body_md) or is_foreign_paste(plain):
        return False
    if judgment_len(plain) < MIN_JUDGMENT and not has_stance(plain):
        return False
    changed = False
    if meta.get(_AUTHOR_VOICE) != "段永平":
        meta[_AUTHOR_VOICE] = "段永平"
        changed = True
    if meta.get(SUPPRESSED) == NO_JUDGMENT:
        meta.pop(SUPPRESSED, None)
        changed = True
        if published_at is not None:
            meta.pop(_OFF_TIMELINE, None)
    return changed


def _flags_ready(meta: dict, reason: str) -> bool:
    if meta.get(SUPPRESSED) != reason or not _flag_on(meta, _OFF_VIEWS):
        return False
    if reason == NO_JUDGMENT and not _flag_on(meta, _OFF_TIMELINE):
        return False
    if reason == MISATTRIBUTED and _voice_label(meta) != VOICE_FOREIGN:
        return False
    return True


def hide_empty_answer(
    conn, article_id: int, body_md: str, raw_meta, *, source_id: str, title: str = "", published_at=None
) -> bool:
    """没判断、判断太短或空回答标上标记，摘掉公司挂靠和观点。正文不删。

    只处理段永平博客。方伟这类来源即使带了同样的标记，也不搁下。
    长文若之前被标成 no_judgment，这里收回。已经标好的不再改标记。
    返回这次是否改了标记。
    """
    reason = suppression_for(body_md, raw_meta, source_id=source_id, title=title)
    meta = dict(raw_meta or {})
    if not reason:
        if not _restore_long_essay(meta, body_md, published_at):
            return False
        conn.execute(
            "UPDATE articles SET raw_meta = %s WHERE id = %s",
            (Jsonb(meta), article_id),
        )
        return True
    changed = False
    if not _flags_ready(meta, reason):
        meta[SUPPRESSED] = reason
        meta[_OFF_VIEWS] = True
        if reason == NO_JUDGMENT:
            meta[_OFF_TIMELINE] = True
        if reason == MISATTRIBUTED:
            meta[_AUTHOR_VOICE] = VOICE_FOREIGN
        conn.execute(
            "UPDATE articles SET raw_meta = %s WHERE id = %s",
            (Jsonb(meta), article_id),
        )
        changed = True
    conn.execute("DELETE FROM article_mentions WHERE article_id = %s", (article_id,))
    conn.execute("DELETE FROM company_views WHERE article_id = %s", (article_id,))
    return changed


def backfill_empty_answers(conn) -> int:
    """启动时扫一遍段永平博客。

    短碎的 all 标 no_judgment，长文若曾被这道规则搁下则收回。
    段永平那节不足 40 个字标 weak_judgment，只剩导航的标 empty_answer。
    已经按这个规则标好的跳过。再跑一遍结果一样。
    """
    like_sql: list[str] = []
    params: list[str] = [SOURCE_ID, _VOICE_ALL, "段永平", WEAK_JUDGMENT, VOICE_FOREIGN, MISATTRIBUTED]
    for column in ("body_md", "title"):
        for pattern in _SCAN_LIKE:
            like_sql.append(f"{column} LIKE %s")
            params.append(pattern)
    rows = conn.execute(
        f"""
        SELECT id, title, body_md, raw_meta, published_at
        FROM articles
        WHERE source_id = %s
          AND (
            coalesce(raw_meta->>'author_voice', '') = %s
            OR (
              coalesce(raw_meta->>'author_voice', '') = %s
              AND coalesce(raw_meta->>'suppressed', '') IN ('', %s)
              AND (
                coalesce(raw_meta->>'suppressed', '') = ''
                OR coalesce(raw_meta->>'off_views', '') <> 'true'
                OR EXISTS (SELECT 1 FROM article_mentions m WHERE m.article_id = articles.id)
                OR EXISTS (SELECT 1 FROM company_views v WHERE v.article_id = articles.id)
              )
            )
            OR (
              coalesce(raw_meta->>'author_voice', '') = %s
              AND (
                coalesce(raw_meta->>'suppressed', '') <> %s
                OR coalesce(raw_meta->>'off_views', '') <> 'true'
                OR EXISTS (SELECT 1 FROM article_mentions m WHERE m.article_id = articles.id)
                OR EXISTS (SELECT 1 FROM company_views v WHERE v.article_id = articles.id)
              )
            )
            OR (
              coalesce(raw_meta->>'suppressed', '') = ''
              AND coalesce(raw_meta->>'author_voice', '') NOT IN ('all', '段永平', 'foreign')
              AND ({" OR ".join(like_sql)})
            )
          )
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
            published_at=row["published_at"],
        ):
            hidden += 1
    if hidden:
        logger.info("段永平判断门槛已更新 %s 篇", hidden)
    conn.commit()
    return hidden
