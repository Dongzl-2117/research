"""一篇里同时有读者提问和作者本人的话时，观点和摘要只读作者那一节。

段永平的问答整条入库：读者的问题和别人的回复留在正文里，检索、作者页、公司页看得到。
交给模型抽观点、写摘要时只留他本人写的段落，避免把提问者的看法算成他的判断。
"""

from __future__ import annotations

import json
import re

from researchlib.empty_answer import is_chrome_line

# 有日期的进首页按天。没日期的文章带上这个标记，首页按天不收，作者、公司、检索仍收。
OFF_TIMELINE = "off_timeline"
# "all"：整篇都是作者的话。其它值：只取这个 Markdown 章节标题下的段落。
AUTHOR_VOICE = "author_voice"
VOICE_ALL = "all"
# 抽观点、写摘要时附在提示里，只在收窄到作者本人时加上。
VOICE_NOTE = "\n（下面只保留段永平本人写的段落。读者提问和别人的回复没有放进来，不要当成他的判断。）"
# 知识星球资格由 eligibility 的来源/账户绑定决定；poster_role 只作显示兼容。
POSTER_ROLE = "poster_role"
ROLE_GUEST = "guest"
ROLE_OWNER = "owner"


def raw_meta_of(conn, row) -> dict:
    """文章的 raw_meta。查询没带这一列时再读一次。"""
    keys = row.keys() if hasattr(row, "keys") else ()
    if "raw_meta" in keys:
        meta = row["raw_meta"]
    elif conn is not None and row.get("id"):
        found = conn.execute("SELECT raw_meta FROM articles WHERE id = %s", (row["id"],)).fetchone()
        meta = found["raw_meta"] if found else None
    else:
        meta = None
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except ValueError:
            return {}
    return meta if isinstance(meta, dict) else {}


def restrict(
    body_text: str, chunks: list[tuple[str, str]], raw_meta: dict | None
) -> tuple[str, list[tuple[str, str]], bool]:
    """返回 (交给模型的正文, 对应的段落块, 是否只留了作者本人)。

    author_voice 缺省或 all：整篇。否则只留章节标题等于这个值的块。
    """
    voice = str((raw_meta or {}).get(AUTHOR_VOICE) or "").strip()
    if not voice or voice == VOICE_ALL:
        return body_text or "", list(chunks), False
    picked = [(heading, text) for heading, text in chunks if (heading or "").strip() == voice and (text or "").strip()]
    text = "\n\n".join(piece for _heading, piece in picked)
    return text, picked, True


def not_guest_sql(alias: str = "a") -> str:
    """星球按真实账户绑定过滤；其它来源保留已有的网友标记。"""
    from researchlib.eligibility import eligibility_sql

    column = f"{alias}.raw_meta" if alias else "raw_meta"
    source = f"{alias}.source_id" if alias else "source_id"
    return (
        f"({eligibility_sql(alias)} AND ({source} IN ('fangwei-zsxq', 'chengfengpolang') "
        f"OR {column} ? 'zsxq_identity' OR coalesce({column}->>'poster_role', '') <> 'guest'))"
    )


# 段永平博客里没有具体公司、或只是列举/致谢/闲聊的条目。人话留着，不进观点。
OFF_VIEWS = "off_views"
DUAN_SOURCE = "duanyongping-blog"
_DUAN_LINE = re.compile(r"^(?:段永平|段总|段)\s*[:：]\s*(.*)$")
_OTHER_LINE = re.compile(
    r"^(?:问|Q|q)\s*[:：]|"
    r"^(?:网友|博友|读者)|"
    r"^(?:[\u4e00-\u9fffA-Za-z0-9_]{0,24}?\s*)?回复\s*[:：]?\s*段(?:永平|总)(?!的)"
)


def off_views(raw_meta: dict | None) -> bool:
    return str((raw_meta or {}).get(OFF_VIEWS) or "").lower() == "true"


def views_eligible_sql(alias: str = "a") -> str:
    """观点候选：不是网友帖，也没有标明不进观点。"""
    return f"{not_guest_sql(alias)} AND coalesce({alias}.raw_meta->>'{OFF_VIEWS}', '') <> 'true'"


def duan_paragraphs(text: str) -> str:
    """只留段永平的话。有「段：」就从这句接到下一位说话人之前；读者和博友回复丢掉。

    整段都没有说话人标记时，当作他的博文留下。
    """
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if not lines:
        return ""
    if not any(_DUAN_LINE.match(line) or _OTHER_LINE.match(line) for line in lines):
        return "\n".join(lines)
    kept: list[str] = []
    taking = False
    for line in lines:
        match = _DUAN_LINE.match(line)
        if match:
            taking = True
            piece = (match.group(1) or "").strip()
            if piece and not is_chrome_line(piece):
                kept.append(piece)
            continue
        if _OTHER_LINE.match(line):
            taking = False
            continue
        if taking:
            if is_chrome_line(line):
                continue
            kept.append(line)
    return "\n".join(kept)


def for_model(
    body_text: str,
    chunks: list[tuple[str, str]],
    raw_meta: dict | None,
    *,
    source_id: str = "",
) -> tuple[str, list[tuple[str, str]], bool]:
    """交给观点或摘要的正文。段永平只留他本人的章节，或「段：」起到下一位说话人之前的段落。"""
    from researchlib.eligibility import IDENTITY_KEY, is_zsxq, project_article, strip_unverified_comments

    candidate = {"source_id": source_id, "raw_meta": raw_meta, "body_md": body_text}
    if is_zsxq(candidate):
        projected = project_article(candidate)
        if projected is None:
            return "", [], True
        identity = (projected.get("raw_meta") or {}).get(IDENTITY_KEY) or {}
        # Guest questions are display context, never the star's model input.
        owner = str(identity.get("owner_account_id") or "")
        replies = [
            str(reply.get("text") or "").strip()
            for reply in identity.get("replies") or []
            if isinstance(reply, dict) and str(reply.get("author_account_id") or "") == owner
        ]
        body = "" if projected.get("owner_reply_context") else strip_unverified_comments(body_text)
        text = "\n\n".join([body, *replies]).strip()
        return text, [("星主本人", text)] if text else [], True
    if is_guest(raw_meta):
        return "", [], True
    if str((raw_meta or {}).get(AUTHOR_VOICE) or "").strip() == "foreign":
        return "", [], True
    body, picked, restricted = restrict(body_text, chunks, raw_meta)
    voice = str((raw_meta or {}).get(AUTHOR_VOICE) or "").strip()
    if source_id != DUAN_SOURCE and voice != "段永平":
        return body, picked, restricted
    if not picked and (body or "").strip():
        picked = [("", body)]
    filtered: list[tuple[str, str]] = []
    for heading, text in picked:
        piece = duan_paragraphs(text)
        if piece.strip():
            filtered.append((heading, piece))
    text = "\n\n".join(piece for _heading, piece in filtered)
    return text, filtered, True


def is_guest(raw_meta: dict | None) -> bool:
    return str((raw_meta or {}).get(POSTER_ROLE) or "") == ROLE_GUEST


def _norm_name(name: str) -> str:
    text = re.sub(r"[（(].*?[）)]", "", name or "")
    return re.sub(r"\s+", "", text).strip()


def same_person(poster: str, star: str) -> bool:
    left, right = _norm_name(poster), _norm_name(star)
    return bool(left) and left == right


def assign_poster_role(
    meta: dict, star_name: str, aliases: tuple[str, ...] = (), *, source_id: str = "", source_kind: str = ""
) -> dict:
    """星球仅凭经来源绑定的账户 ID；昵称只用于显示。"""
    from researchlib.eligibility import IDENTITY_KEY, is_zsxq, project_article

    identity = meta.get(IDENTITY_KEY) or {}
    sid = source_id or (identity.get("source_id", "") if isinstance(identity, dict) else "")
    candidate = {"source_id": sid, "source_kind": source_kind, "raw_meta": meta}
    if is_zsxq(candidate):
        projected = project_article(candidate)
        meta[POSTER_ROLE] = projected["raw_meta"][POSTER_ROLE] if projected else "unknown"
        return meta
    name = str(meta.get("poster_name") or "").strip()
    if not name:
        meta.pop(POSTER_ROLE, None)
        return meta
    owner = any(same_person(name, star) for star in (star_name, *aliases))
    meta[POSTER_ROLE] = ROLE_OWNER if owner else ROLE_GUEST
    return meta


def display_author(star_name: str, raw_meta: dict | None) -> tuple[str, bool]:
    """返回 (显示名, 是否算星主本人)。网友帖显示发帖人，不链到星主作者页。"""
    if str((raw_meta or {}).get(POSTER_ROLE) or "") in {ROLE_GUEST, "unknown", "excluded"}:
        return str((raw_meta or {}).get("poster_name") or "").strip() or "星球读者", False
    return star_name, True
