"""Account-bound knowledge-circle eligibility and safe, reversible projections.

Archive names are display strings, never identity evidence.  The registry below
is bound to the API's group owner response, not a post author's nickname.
Raw inbox records remain untouched; unknown identities are held out of readers.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Mapping

from researchlib.model_inputs import ocr_cache_safe_article, ocr_cache_safe_sql
from researchlib.textutil import markdown_to_text

IDENTITY_KEY = "zsxq_identity"
IDENTITY_VERSION = 1
# Verified from fangwei-zsxq/raw/group.json: resp_data.group.owner.user_id,
# and the group_id on its raw/topics records.  No signed image URLs are stored.
TRUSTED_OWNERS = {"fangwei-zsxq": ("48888181884848", "1825524112")}
ZSXQ_SOURCES = frozenset({"fangwei-zsxq", "chengfengpolang"})
_COMMENT_SECTION = re.compile(r"^##\s*(?:评论\s*[（(]\s*\d+\s*[)）]|评论里的回复)\s*$", re.M)
_VERIFIED_REPLY_SECTION = re.compile(r"^##\s*星主回复（账户已核实）\s*$", re.M)
_NEXT_SECTION = re.compile(r"^#{1,6}\s+\S", re.M)
_IMAGE = re.compile(r"!\[[^\]]*\](?:\([^)]*\)|\[[^\]]*\])?|<img\b[^>]*>", re.I)
_MODEL_FIELDS = (
    "digest", "summary", "company_views", "views", "chunks", "translations", "translation",
    "digest_summary", "digest_points", "digest_tags", "digest_model", "digest_content_hash",
    "digest_ocr_hash", "translated_md", "translated_text", "translation_body_md", "translation_body_text",
)


def _id(value: object) -> str:
    return str(value).strip() if value is not None else ""


def _meta(article: Mapping) -> dict:
    value = article.get("raw_meta") or {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def is_zsxq(article: Mapping) -> bool:
    meta = _meta(article)
    return (
        _id(article.get("source_id")) in ZSXQ_SOURCES
        or article.get("source_kind", article.get("kind")) == "zsxq"
        or IDENTITY_KEY in meta
        or meta.get("source_kind") == "zsxq"
    )


def topic_poster(topic: Mapping) -> dict:
    for key in ("answer", "talk", "task", "solution"):
        child = topic.get(key)
        if isinstance(child, dict) and isinstance(child.get("owner"), dict):
            return child["owner"]
    value = topic.get("owner")
    return value if isinstance(value, dict) else {}


def topic_group_id(topic: Mapping) -> str:
    group = topic.get("group")
    return _id(group.get("group_id")) if isinstance(group, dict) else _id(topic.get("group_id"))


def _text(value: object) -> str:
    # Import lazily so parse.py can reuse the identity implementation.
    from researchlib.ingest.parse import zsxq_text

    return zsxq_text(value)


def topic_comments(topic: Mapping) -> list[dict]:
    """Complete comments win over the UI preview; preserve explicit parents."""
    items = topic.get("comments")
    if not isinstance(items, list):
        items = topic.get("show_comments") or []
    found: list[dict] = []
    seen: set[str] = set()

    def add(comment: object, parent: str = "") -> None:
        if not isinstance(comment, dict):
            return
        item = dict(comment)
        cid = _id(item.get("comment_id"))
        if cid and cid in seen:
            return
        if cid:
            seen.add(cid)
        if parent and not item.get("parent_comment_id"):
            item["parent_comment_id"] = parent
        found.append(item)
        for child in item.get("replied_comments") or []:
            add(child, cid)

    for item in items if isinstance(items, list) else []:
        add(item)
    return found


def verified_replies(topic: Mapping, owner_account_id: str) -> list[dict]:
    """Authors come from owner.user_id; repliee is only a recipient."""
    comments = topic_comments(topic)
    by_id = {_id(c.get("comment_id")): c for c in comments if c.get("comment_id") is not None}
    replies: list[dict] = []
    for comment in comments:
        author = comment.get("owner") or {}
        if not isinstance(author, dict) or _id(author.get("user_id")) != owner_account_id:
            continue
        cid, text = _id(comment.get("comment_id")), _text(comment.get("text"))
        if not cid or not text:
            continue
        recipient = comment.get("repliee") or {}
        recipient_id = _id(recipient.get("user_id")) if isinstance(recipient, dict) else ""
        parent_id = _id(comment.get("parent_comment_id"))
        parent = by_id.get(parent_id) or {}
        parent_author = parent.get("owner") or {}
        parent_account_id = _id(parent_author.get("user_id")) if isinstance(parent_author, dict) else ""
        question = ""
        if parent_account_id and parent_account_id != owner_account_id:
            # A recipient alone does not identify which of that person's comments
            # was answered.  Only an explicit comment-parent link supplies text.
            if not recipient_id or recipient_id == parent_account_id:
                question = _text(parent.get("text"))
        replies.append({
            "comment_id": cid,
            "parent_comment_id": parent_id,
            "author_account_id": owner_account_id,
            "recipient_account_id": recipient_id,
            "text": text,
            "create_time": _id(comment.get("create_time")),
            "question": question,
            "question_account_id": parent_account_id if question else "",
            "question_comment_id": parent_id if question else "",
        })
    return replies


def identity_for_topic(topic: Mapping, source_id: str) -> dict:
    binding = TRUSTED_OWNERS.get(source_id)
    group_id = topic_group_id(topic)
    poster_id = _id(topic_poster(topic).get("user_id"))
    result = {
        "version": IDENTITY_VERSION,
        "source_id": source_id,
        "topic_id": _id(topic.get("topic_id", topic.get("topicId"))),
        "group_id": group_id,
        "poster_account_id": poster_id,
        "owner_account_id": binding[1] if binding else "",
        "kind": "unknown",
        "replies": [],
        "comments_source": "comments" if isinstance(topic.get("comments"), list) else "show_comments",
        "declared_comment_count": (
            topic.get("comments_count") if isinstance(topic.get("comments_count"), int) else None
        ),
        "observed_comment_count": len(topic_comments(topic)),
    }
    if not binding or group_id != binding[0] or not poster_id:
        return result
    result["replies"] = verified_replies(topic, binding[1])
    if poster_id == binding[1]:
        result["kind"] = "owner_post"
        for key in ("answer", "talk", "task", "solution"):
            own = topic.get(key)
            if isinstance(own, dict) and isinstance(own.get("text"), str):
                if _id((own.get("owner") or {}).get("user_id")) == binding[1]:
                    result["owner_topic_text"] = _text(own["text"])
                    break
    elif result["replies"]:
        result["kind"] = "owner_reply_context"
    else:
        result["kind"] = "excluded"
    return result


def _valid_identity(article: Mapping) -> dict | None:
    source_id = _id(article.get("source_id"))
    binding = TRUSTED_OWNERS.get(source_id)
    if binding is None:
        return None
    meta = _meta(article)
    identity = meta.get(IDENTITY_KEY)
    if isinstance(identity, dict):
        if (
            type(identity.get("version")) is not int or identity.get("version") != IDENTITY_VERSION
            or _id(identity.get("source_id")) != source_id
            or _id(identity.get("group_id")) != binding[0]
            or _id(identity.get("owner_account_id")) != binding[1]
            or not _id(identity.get("poster_account_id"))
        ):
            return None
        kind = identity.get("kind")
        if kind == "owner_post" and _id(identity.get("poster_account_id")) == binding[1]:
            return identity
        if kind == "owner_reply_context" and _id(identity.get("poster_account_id")) != binding[1]:
            replies = identity.get("replies")
            if isinstance(replies, list) and any(
                isinstance(r, dict)
                and _id(r.get("author_account_id")) == binding[1]
                and _id(r.get("comment_id"))
                and _id(r.get("text"))
                for r in replies
            ):
                return identity
        return None
    # Existing imports recorded the raw account ID but not the group response.
    # The source itself has a verified single-group binding.  An explicitly
    # conflicting group is rejected, and names/old poster_role are ignored.
    group_id = _id(meta.get("group_id"))
    if _id(meta.get("poster_id")) == binding[1] and group_id in ("", binding[0]):
        return {
            "version": IDENTITY_VERSION, "source_id": source_id, "group_id": binding[0],
            "owner_account_id": binding[1], "poster_account_id": binding[1],
            "topic_id": _id(meta.get("topic_id")), "kind": "owner_post", "replies": [],
        }
    return None


def is_eligible_article(article: Mapping) -> bool:
    return not is_zsxq(article) or _valid_identity(article) is not None


def model_cache_safe_article(article: Mapping) -> bool:
    if not is_zsxq(article):
        return True
    if not is_eligible_article(article):
        return False
    identity = _meta(article).get(IDENTITY_KEY)
    return (
        isinstance(identity, dict)
        and type(identity.get("version")) is int and identity.get("version") == IDENTITY_VERSION
        and isinstance(identity.get("projection_md"), str)
        and identity["projection_md"] == article.get("body_md")
    )


projected_cache_safe = model_cache_safe_article


def _in_fence(text: str, position: int) -> bool:
    marker = ""
    length = 0
    for line in text[:position].splitlines():
        match = re.match(r"^[ \t]{0,3}(`{3,}|~{3,})(.*)$", line)
        if not match:
            continue
        current, trailing = match.groups()
        if not marker:
            marker, length = current[0], len(current)
        elif current[0] == marker and len(current) >= length and not trailing.strip():
            marker = ""
    return bool(marker)


def strip_unverified_comments(
    body_md: str, *, include_verified: bool = True, proven_owner_text: str = ""
) -> str:
    """Quarantine comment tails; a later heading alone never proves authorship.

    A following section can be restored only when its full text is independently
    present in the API text of the account-verified original topic.
    """
    text = body_md or ""
    pattern = re.compile(
        "|".join([_COMMENT_SECTION.pattern, _VERIFIED_REPLY_SECTION.pattern])
        if include_verified else _COMMENT_SECTION.pattern,
        re.M,
    )
    start = 0
    while match := pattern.search(text, start):
        if _in_fence(text, match.start()):
            start = match.end()
            continue
        following = _NEXT_SECTION.search(text, match.end())
        while following and _in_fence(text, following.start()):
            following = _NEXT_SECTION.search(text, following.end())
        verified_tail: list[str] = []
        proof = re.sub(r"\s+", "", markdown_to_text(proven_owner_text or ""))
        while following and proof:
            next_heading = _NEXT_SECTION.search(text, following.end())
            while next_heading and _in_fence(text, next_heading.start()):
                next_heading = _NEXT_SECTION.search(text, next_heading.end())
            section = text[following.start():next_heading.start() if next_heading else len(text)]
            normalized = re.sub(r"\s+", "", markdown_to_text(section))
            if normalized and normalized in proof and not pattern.match(section):
                verified_tail.append(section.rstrip())
            following = next_heading
        text = text[:match.start()].rstrip() + (
            "\n\n" + "\n\n".join(verified_tail) + "\n" if verified_tail else "\n"
        )
        start = 0
    return text


def _reply_markdown(identity: Mapping) -> str:
    owner_id = _id(identity.get("owner_account_id"))
    parts: list[str] = []
    for reply in identity.get("replies") or []:
        if not isinstance(reply, dict) or _id(reply.get("author_account_id")) != owner_id:
            continue
        if not _id(reply.get("comment_id")) or not _id(reply.get("text")):
            continue
        question = _IMAGE.sub("", _id(reply.get("question"))).strip()
        # Questions require an explicit comment link and a non-owner account.
        if (
            question and reply.get("question_comment_id") == reply.get("parent_comment_id")
            and reply.get("question_account_id") and reply.get("question_account_id") != owner_id
        ):
            quoted = "\n".join("> " + line if line else ">" for line in question.splitlines())
            parts.append("网友问题（仅作星主回复上下文）：\n\n" + quoted)
        parts.append("星主回复：\n\n" + _IMAGE.sub("", _id(reply.get("text"))).strip())
    return "\n\n".join(parts)


def project_article(article: Mapping) -> dict | None:
    """Return eligible owner content; never expose an unverified guest cache."""
    if not is_zsxq(article):
        return dict(article)
    identity = _valid_identity(article)
    if identity is None:
        return None
    result = dict(article)
    meta = copy.deepcopy(_meta(article))
    meta[IDENTITY_KEY] = copy.deepcopy(identity)
    reply_md = _reply_markdown(identity)
    if identity["kind"] == "owner_reply_context":
        body = reply_md
        first = next((r["text"] for r in identity.get("replies", []) if r.get("text")), "")
        result["title"] = "星主回复：" + markdown_to_text(first).split("\n")[0][:60]
        meta["poster_role"] = "owner_reply_context"
        meta.pop("local_attachments", None)
        for key in ("images", "article_images", "image_texts", "ocr", "ocr_text", "digest", "summary",
                    "company_views", "views", "chunks", "translations", "translation"):
            if key in result:
                result[key] = [] if isinstance(result[key], list) else None
        result["owner_reply_context"] = True
    else:
        original = _id(result.get("body_md"))
        body = strip_unverified_comments(original, proven_owner_text=identity.get("owner_topic_text") or "")
        old_comment = _COMMENT_SECTION.search(original)
        following = _NEXT_SECTION.search(original, old_comment.end()) if old_comment else None
        if following:
            original_tail = original[following.start():]
            proof = re.sub(r"\s+", "", markdown_to_text(identity.get("owner_topic_text") or ""))
            if not proof or re.sub(r"\s+", "", markdown_to_text(original_tail)) not in proof:
                meta["owner_tail_quarantined"] = True
                result["identity_notice"] = "旧评论后的归档片段暂未确认归属，已隔离；原始归档保留。"
        if reply_md:
            body = body.rstrip() + "\n\n## 星主回复（账户已核实）\n\n" + reply_md + "\n"
        meta["poster_role"] = "owner"
    result["body_md"] = body
    result["body_text"] = markdown_to_text(body)
    if identity["kind"] == "owner_post":
        from researchlib.attachments import filter_attachment_manifest

        meta = filter_attachment_manifest(meta, body)
    result["raw_meta"] = meta
    result["identity_kind"] = identity["kind"]
    # Historical summaries/embeddings may include comments removed above.
    if body != _id(article.get("body_md")):
        result["content_projection_changed"] = True
    cache_safe = model_cache_safe_article(article)
    if not cache_safe:
        notice = "历史摘要、译文和图片文字仍待核对，暂不展示；已核实的正文或回复仍可阅读，原始归档保留。"
        previous_notice = result.get("identity_notice") or ""
        result["identity_notice"] = previous_notice if notice in previous_notice else " ".join(
            filter(None, (previous_notice, notice))
        )
    if body != _id(article.get("body_md")) or not cache_safe:
        for key in _MODEL_FIELDS:
            if key in result:
                result[key] = [] if isinstance(result[key], list) else None
    return result


def _column(alias: str, name: str) -> str:
    if alias and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", alias):
        raise ValueError("invalid SQL alias")
    return f"{alias}.{name}" if alias else name


def eligibility_sql(alias: str = "a") -> str:
    """Same source/account rule for lists, scopes, statistics and candidates."""
    source = _column(alias, "source_id")
    meta = _column(alias, "raw_meta")
    ident = f"{meta}->'{IDENTITY_KEY}'"
    zsxq = (
        f"({source} IN ('fangwei-zsxq', 'chengfengpolang') OR {meta} ? '{IDENTITY_KEY}' "
        f"OR {meta}->>'source_kind' = 'zsxq' OR EXISTS "
        f"(SELECT 1 FROM sources _elig_source WHERE _elig_source.id = {source} AND _elig_source.kind = 'zsxq'))"
    )
    verified: list[str] = []
    for source_id, (group_id, owner_id) in TRUSTED_OWNERS.items():
        reply_array = (
            f"CASE WHEN jsonb_typeof({ident}->'replies') = 'array' "
            f"THEN {ident}->'replies' ELSE '[]'::jsonb END"
        )
        identity = (
            f"({ident}->'version' = '1'::jsonb AND {ident}->>'version' = '1' "
            f"AND {ident}->>'source_id' = '{source_id}' "
            f"AND {ident}->>'group_id' = '{group_id}' AND {ident}->>'owner_account_id' = '{owner_id}' "
            f"AND (({ident}->>'kind' = 'owner_post' AND {ident}->>'poster_account_id' = '{owner_id}') "
            f"OR ({ident}->>'kind' = 'owner_reply_context' "
            f"AND coalesce({ident}->>'poster_account_id', '') NOT IN ('', '{owner_id}') "
            f"AND EXISTS (SELECT 1 FROM jsonb_array_elements({reply_array}) _elig_reply "
            f"WHERE _elig_reply->>'author_account_id' = '{owner_id}' "
            f"AND coalesce(_elig_reply->>'comment_id', '') <> '' "
            f"AND btrim(coalesce(_elig_reply->>'text', '')) <> ''))))"
        )
        legacy = (
            f"(NOT ({meta} ? '{IDENTITY_KEY}') AND {meta}->>'poster_id' = '{owner_id}' "
            f"AND coalesce({meta}->>'group_id', '') IN ('', '{group_id}'))"
        )
        verified.append(f"({source} = '{source_id}' AND ({identity} OR {legacy}))")
    return f"(NOT coalesce({zsxq}, false) OR coalesce(({' OR '.join(verified)}), false))"


def model_cache_safe_sql(alias: str = "a") -> str:
    """Legacy caches cannot prove they contain the current owner projection."""
    source = _column(alias, "source_id")
    meta = _column(alias, "raw_meta")
    body = _column(alias, "body_md")
    zsxq = (
        f"({source} IN ('fangwei-zsxq', 'chengfengpolang') OR {meta} ? '{IDENTITY_KEY}' "
        f"OR {meta}->>'source_kind' = 'zsxq' OR EXISTS "
        f"(SELECT 1 FROM sources _cache_source WHERE _cache_source.id = {source} AND _cache_source.kind = 'zsxq'))"
    )
    return (
        f"coalesce(({eligibility_sql(alias)} AND (NOT coalesce({zsxq}, false) OR "
        f"({meta}->'{IDENTITY_KEY}'->'version' = '1'::jsonb "
        f"AND {meta}->'{IDENTITY_KEY}'->>'version' = '1' "
        f"AND {meta}->'{IDENTITY_KEY}'->>'projection_md' = {body}))), false)"
    )


def chunk_cache_safe_sql(alias: str = "a", chunk_alias: str = "c") -> str:
    """Reuse only original chunks whose exact text remains in the owner body.

    This deliberately differs from model-cache safety: a legacy original-body
    chunk can be checked against the current projection, whereas a summary or
    embedding may mix in an omitted guest paragraph without an exact excerpt.
    """
    source, meta = _column(alias, "source_id"), _column(alias, "raw_meta")
    text, title = _column(alias, "body_text"), _column(alias, "title")
    body, heading = _column(chunk_alias, "body"), _column(chunk_alias, "heading")
    ordinal, lang = _column(chunk_alias, "ord"), _column(chunk_alias, "lang")
    zsxq = (
        f"({source} IN ('fangwei-zsxq', 'chengfengpolang') OR {meta} ? '{IDENTITY_KEY}' "
        f"OR {meta}->>'source_kind' = 'zsxq' OR EXISTS "
        f"(SELECT 1 FROM sources _chunk_source WHERE _chunk_source.id = {source} AND _chunk_source.kind = 'zsxq'))"
    )
    pattern = (
        r"(^|\n)(#{1,6}[[:space:]]*)?"
        r"(评论[（(][[:space:]]*[0-9]+[[:space:]]*[)）]|评论里的回复|星主回复（账户已核实）)(\n|$)"
    )
    prefix = f"(regexp_split_to_array(coalesce({text}, ''), '{pattern}'))[1]"
    safe_text = (
        f"CASE WHEN {model_cache_safe_sql(alias)} THEN coalesce({text}, '') "
        f"WHEN coalesce({meta}->'{IDENTITY_KEY}'->>'kind', 'owner_post') = 'owner_post' "
        f"THEN {prefix} ELSE '' END"
    )
    normalized_text = f"regexp_replace(({safe_text}), '[[:space:]]+', '', 'g')"
    normalized_body = f"regexp_replace({body}, '[[:space:]]+', '', 'g')"
    normalized_heading = f"regexp_replace({heading}, '[[:space:]]+', '', 'g')"
    exact = (
        f"({ordinal} < 10000 AND coalesce({lang}, '') <> 'zh' AND btrim({body}) <> '' "
        f"AND strpos({normalized_text}, {normalized_body}) > 0 "
        f"AND (coalesce({heading}, '') = '' OR {heading} = {title} "
        f"OR strpos({normalized_text}, {normalized_heading}) > 0))"
    )
    return f"({eligibility_sql(alias)} AND (NOT coalesce({zsxq}, false) OR {exact}))"


def chunk_cache_safe_article(article: Mapping, chunk: Mapping) -> bool:
    if not is_eligible_article(article):
        return False
    if not is_zsxq(article):
        return True
    if int(chunk.get("ord") or 0) >= 10000 or chunk.get("lang") == "zh":
        return False
    projected = project_article(article)
    if projected is None:
        return False
    if projected.get("owner_reply_context") and not model_cache_safe_article(article):
        return False
    text = projected.get("body_text") or ""
    if not model_cache_safe_article(article):
        # Match SQL's conservative legacy prefix. Later text after unknown
        # comment sections is not proved to belong to the original owner.
        pattern = (
            r"(^|\n)(#{1,6}[ \t]*)?"
            r"(评论[（(][ \t]*[0-9]+[ \t]*[)）]|评论里的回复|星主回复（账户已核实）)(\n|$)"
        )
        text = re.split(pattern, article.get("body_text") or text, maxsplit=1)[0]
    body, heading = _id(chunk.get("body")), _id(chunk.get("heading"))
    normalized_text = re.sub(r"\s+", "", text)
    normalized_body, normalized_heading = re.sub(r"\s+", "", body), re.sub(r"\s+", "", heading)
    return bool(normalized_body) and normalized_body in normalized_text and (
        not heading or heading == projected.get("title") or normalized_heading in normalized_text
    )


def chunk_provenance_sql(alias: str = "a", chunk_alias: str = "c") -> str:
    """Prove each cached chunk from current body, translation, or owned image."""
    aid, meta = _column(alias, "id"), _column(alias, "raw_meta")
    title = _column(alias, "title")
    body_md, body_text = _column(alias, "body_md"), _column(alias, "body_text")
    article_ocr = _column(alias, "ocr_text")
    content_hash = _column(alias, "content_hash")
    body, heading = _column(chunk_alias, "body"), _column(chunk_alias, "heading")
    lang, ordinal, anchor = (_column(chunk_alias, key) for key in ("lang", "ord", "anchor"))

    def normalized(expression: str) -> str:
        return f"regexp_replace(coalesce({expression}, ''), '[[:space:]]+', '', 'g')"

    text = normalized(body)
    def roleless(expression: str) -> str:
        without_markers = f"regexp_replace(coalesce({expression}, ''), '[\u2060-\u2064]', '', 'g')"
        return (
            f"regexp_replace({without_markers}, "
            "'(^|\\n)((方[[:space:]]*伟(Rey)?|网友|未确认)[[:space:]]*[:：][[:space:]]*"
            "|图表/资料摘录（非对话，作者未确认）[:：]?[[:space:]]*)', '', 'g')"
        )
    original = (
        f"(coalesce({lang}, '') = '' AND {ordinal} < 10000 "
        f"AND {chunk_cache_safe_sql(alias, chunk_alias)} "
        f"AND strpos({normalized(body_text)}, {text}) > 0 "
        f"AND (coalesce({heading}, '') = '' OR {heading} = {title} "
        f"OR strpos({normalized(body_text)}, {normalized(heading)}) > 0))"
    )
    translation = (
        f"({lang} = 'zh' AND {ordinal} >= 10000 AND {ordinal} < 20000 "
        f"AND {model_cache_safe_sql(alias)} "
        f"AND EXISTS (SELECT 1 FROM article_translations _proven_translation "
        f"WHERE _proven_translation.article_id = {aid} AND _proven_translation.complete "
        f"AND _proven_translation.content_hash = {content_hash} "
        f"AND strpos({normalized('_proven_translation.body_text')}, {text}) > 0 "
        f"AND (coalesce({heading}, '') = '' OR {heading} = _proven_translation.title_zh "
        f"OR strpos({normalized('_proven_translation.body_text')}, {normalized(heading)}) > 0)))"
    )
    ocr = (
        f"({lang} = 'ocr' AND {ordinal} >= 20000 AND {heading} = '图片里的字' "
        f"AND {model_cache_safe_sql(alias)} AND {ocr_cache_safe_sql(alias)} "
        f"AND coalesce({meta}->'{IDENTITY_KEY}'->>'kind', '') <> 'owner_reply_context' "
        f"AND strpos({normalized(article_ocr)}, {text}) > 0 "
        f"AND EXISTS (SELECT 1 FROM article_images _proven_image "
        f"JOIN image_texts _proven_ocr ON _proven_ocr.content_hash = _proven_image.content_hash "
        f"WHERE _proven_image.article_id = {aid} "
        f"AND {anchor} = 'img-' || left(_proven_image.content_hash, 12) "
        f"AND coalesce(_proven_image.original_ref, '') <> '' "
        f"AND strpos(coalesce({body_md}, ''), _proven_image.original_ref) > 0 "
        f"AND NOT EXISTS (SELECT 1 FROM regexp_matches({roleless(body)}, '[[:alnum:]]+', 'g') _ocr_word "
        f"WHERE strpos({roleless('_proven_ocr.text')}, _ocr_word[1]) = 0)))"
    )
    return f"({eligibility_sql(alias)} AND {text} <> '' AND ({original} OR {translation} OR {ocr}))"


def chunk_provenance_article(
    article: Mapping, chunk: Mapping, *, translation: Mapping | None = None, images=()
) -> bool:
    """Python companion to chunk_provenance_sql with freshly read evidence."""
    if not is_eligible_article(article):
        return False
    projected = project_article(article)
    if projected is None:
        return False

    def normalized(value) -> str:
        return re.sub(r"\s+", "", str(value or ""))

    body, heading = normalized(chunk.get("body")), normalized(chunk.get("heading"))
    if not body:
        return False
    lang, ordinal = chunk.get("lang") or "", int(chunk.get("ord") or 0)
    if lang == "" and ordinal < 10000:
        text = normalized(projected["body_text"])
        return (chunk_cache_safe_article(article, chunk) and body in text
                and (not heading or heading == normalized(projected.get("title")) or heading in text))
    if not model_cache_safe_article(article):
        return False
    if lang == "zh" and 10000 <= ordinal < 20000:
        return bool(
            translation and translation.get("complete")
            and translation.get("content_hash") == article.get("content_hash")
            and body in normalized(translation.get("body_text"))
            and (not heading or heading == normalized(translation.get("title_zh"))
                 or heading in normalized(translation.get("body_text")))
        )
    if (lang == "ocr" and ordinal >= 20000 and chunk.get("heading") == "图片里的字"
            and ocr_cache_safe_article(article) and not projected.get("owner_reply_context")):
        from researchlib.wechat_dialogue import present_ocr_pieces

        if body not in normalized(article.get("ocr_text")):
            return False
        return any(
            item.get("content_hash") and chunk.get("anchor") == "img-" + str(item["content_hash"])[:12]
            and item.get("original_ref") and item["original_ref"] in projected["body_md"]
            and body in normalized(present_ocr_pieces([str(item.get("text") or "")])) for item in images
        )
    return False
