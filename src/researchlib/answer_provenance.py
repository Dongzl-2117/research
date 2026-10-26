"""Immutable input receipts for saved answers; no database or model operations."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping

from researchlib.eligibility import IDENTITY_KEY, model_cache_safe_article, project_article
from researchlib.empty_answer import is_suppressed
from researchlib.model_inputs import OCR_ROLE_VERSION, ocr_cache_safe_article

INPUT_PROVENANCE_VERSION = 1
VERSION_KEY = "_input_provenance_version"
INPUTS_KEY = "_input_provenance"
PAYLOAD_KEY = "_input_passages_sha256"
OUTLINE_KEY = "_input_outline"
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_LANGS = frozenset({"", "ocr", "zh"})
_PASSAGE_FIELDS = (
    "n", "article_id", "ord", "title", "heading", "text", "before", "after",
    "source_id", "source", "author", "date", "lang",
)


def _positive_id(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.isascii() and value.isdigit() and str(int(value)) == value:
        return int(value) if int(value) > 0 else None
    return None


def _digest(value) -> str | None:
    try:
        data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        return None
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _input_usage(passages, outline):
    if not isinstance(passages, list) or not isinstance(outline, list):
        return None
    usage: dict[int, set[str]] = {}
    for passage in passages:
        if not isinstance(passage, Mapping) or (article_id := _positive_id(passage.get("article_id"))) is None:
            return None
        raw_lang = passage.get("lang")
        lang = "" if raw_lang is None else raw_lang
        if not isinstance(lang, str) or lang not in _LANGS:
            return None
        usage.setdefault(article_id, set()).add(lang)
    for item in outline:
        if not isinstance(item, Mapping) or (article_id := _positive_id(item.get("article_id"))) is None:
            return None
        if not isinstance(item.get("title"), str):
            return None
        usage.setdefault(article_id, set()).add("")
    return usage or None


def _payload_hash(passages, outline) -> str | None:
    return _digest({
        "passages": [{key: passage.get(key) for key in _PASSAGE_FIELDS} for passage in passages],
        "outline": [{"article_id": item["article_id"], "title": item["title"]} for item in outline],
    })


def fingerprint_article(article: Mapping, langs: Iterable[str] = ("",)) -> str | None:
    """Fingerprint canonical content and the identity/OCR policy it was read under.

    A proved base-text passage can establish new answer provenance independently
    of an old article's untrusted model caches. Derived input still needs those
    cache boundaries, and OCR must carry the current role interpretation version.
    """
    if not isinstance(article, Mapping) or not article.get("source_id"):
        return None
    projected = project_article(article)
    if projected is None:
        return None
    meta = projected.get("raw_meta") or {}
    if not isinstance(meta, Mapping) or is_suppressed(meta):
        return None
    languages = set(langs)
    if not languages or not languages <= _LANGS:
        return None
    if languages - {""} and not model_cache_safe_article(article):
        return None
    if "ocr" in languages and not ocr_cache_safe_article(article):
        return None
    if not isinstance(projected.get("title"), str) or not isinstance(projected.get("body_md"), str):
        return None
    if not isinstance(projected.get("content_hash"), str) or not projected["content_hash"]:
        return None
    return _digest({
        "source_id": projected["source_id"],
        "title": projected["title"],
        "body_md": projected["body_md"],
        "content_hash": projected["content_hash"],
        "ocr_hash": projected.get("ocr_hash") or "",
        "ocr_role_version": meta.get("ocr_role_version"),
        "required_ocr_role_version": OCR_ROLE_VERSION if "ocr" in languages else None,
        "identity": meta.get(IDENTITY_KEY),
        "source_kind": projected.get("source_kind") or meta.get("source_kind"),
        "poster_id": meta.get("poster_id"),
        "group_id": meta.get("group_id"),
        "owner_tail_quarantined": bool(meta.get("owner_tail_quarantined")),
        "langs": sorted(languages),
    })


def snapshot_input_provenance(articles, passages, *, outline=None) -> dict | None:
    outline = [] if outline is None else outline
    usage = _input_usage(passages, outline)
    if usage is None:
        return None
    try:
        rows = list(articles)
    except TypeError:
        return None
    by_id = {}
    for article in rows:
        if not isinstance(article, Mapping) or (article_id := _positive_id(article.get("id"))) is None:
            return None
        if article_id in by_id:
            return None
        by_id[article_id] = article
    if set(by_id) != set(usage):
        return None
    manifest = {}
    for article_id, langs in usage.items():
        fingerprint = fingerprint_article(by_id[article_id], langs)
        if fingerprint is None:
            return None
        manifest[str(article_id)] = {"fingerprint": fingerprint, "langs": sorted(langs)}
    for passage in passages:
        current = project_article(by_id[_positive_id(passage["article_id"])])
        if current is None or passage.get("source_id") != current["source_id"]:
            return None
    for item in outline:
        current = project_article(by_id[_positive_id(item["article_id"])])
        if current is None or item["title"] != current["title"]:
            return None
    payload_hash = _payload_hash(passages, outline)
    if payload_hash is None:
        return None
    return {
        VERSION_KEY: INPUT_PROVENANCE_VERSION,
        INPUTS_KEY: manifest,
        PAYLOAD_KEY: payload_hash,
        OUTLINE_KEY: [dict(item) for item in outline],
    }


def provenance_article_ids(scope) -> list[int] | None:
    if not isinstance(scope, Mapping) or type(scope.get(VERSION_KEY)) is not int:
        return None
    if scope[VERSION_KEY] != INPUT_PROVENANCE_VERSION:
        return None
    manifest = scope.get(INPUTS_KEY)
    if not isinstance(manifest, Mapping) or not manifest or not isinstance(scope.get(PAYLOAD_KEY), str):
        return None
    if _HEX.fullmatch(scope[PAYLOAD_KEY]) is None:
        return None
    ids = []
    for key, proof in manifest.items():
        article_id = _positive_id(key)
        if not isinstance(key, str) or article_id is None or not isinstance(proof, Mapping):
            return None
        langs = proof.get("langs")
        if not isinstance(langs, list) or not langs or any(not isinstance(lang, str) for lang in langs):
            return None
        if not set(langs) <= _LANGS or len(set(langs)) != len(langs):
            return None
        fingerprint = proof.get("fingerprint")
        if not isinstance(fingerprint, str) or _HEX.fullmatch(fingerprint) is None:
            return None
        ids.append(article_id)
    return sorted(ids)


def verify_input_provenance(scope, current_articles, stored_passages) -> str:
    if provenance_article_ids(scope) is None:
        return "历史回答缺少完整的原始输入依据，暂不展示。原始记录保留，未自动重新整合。"
    outline = scope.get(OUTLINE_KEY)
    if not isinstance(outline, list):
        return "保存的输入依据不完整，暂不展示回答和引用。"
    current = snapshot_input_provenance(current_articles, stored_passages, outline=outline)
    keys = (VERSION_KEY, INPUTS_KEY, PAYLOAD_KEY, OUTLINE_KEY)
    if current is None or any(scope.get(key) != current[key] for key in keys):
        return "回答使用的原文、身份或图片文字依据已变化，暂不展示。原始记录保留，未自动重新整合。"
    return ""


def public_scope(scope) -> dict:
    """Internal receipts are not user-selectable filters or API response data."""
    if not isinstance(scope, Mapping):
        return {}
    return {key: value for key, value in scope.items() if not str(key).startswith("_")}
