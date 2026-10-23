"""Provenance boundary for summaries created before conservative OCR roles."""

from __future__ import annotations

import re
from collections.abc import Mapping

OCR_MODEL_INPUT_VERSION = 3
OCR_ROLE_VERSION = 3


def ocr_cache_safe_sql(article_alias: str = "a") -> str:
    """Only explicitly recomposed OCR may feed readers, search or embeddings."""
    if article_alias and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", article_alias):
        raise ValueError("invalid SQL alias")
    prefix = f"{article_alias}." if article_alias else ""
    return (
        f"coalesce((jsonb_typeof({prefix}raw_meta->'ocr_role_version') = 'number' "
        f"AND {prefix}raw_meta->>'ocr_role_version' = '{OCR_ROLE_VERSION}'), false)"
    )


def ocr_cache_safe_article(article: Mapping) -> bool:
    meta = article.get("raw_meta") or {}
    return isinstance(meta, Mapping) and type(meta.get("ocr_role_version")) is int and (
        meta["ocr_role_version"] == OCR_ROLE_VERSION
    )


def cached_ocr_safe_sql(article_alias: str = "a", cache_alias: str = "d") -> str:
    """Old OCR-derived summaries stay hidden until explicit safe processing.

    Check both hashes: a changed projection can clear the current article hash
    while an older summary still contains conclusions from its previous OCR.
    """
    if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", alias) for alias in (article_alias, cache_alias)):
        raise ValueError("invalid SQL alias")
    return (
        f"((coalesce({article_alias}.ocr_hash, '') = '' AND coalesce({cache_alias}.ocr_hash, '') = '') "
        f"OR coalesce({cache_alias}.prompt_version, 0) >= {OCR_MODEL_INPUT_VERSION})"
    )
