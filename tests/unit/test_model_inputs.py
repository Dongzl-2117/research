"""Execute the cache predicate against actual SQL, with an in-memory engine."""

import sqlite3

import pytest

from researchlib.model_inputs import cached_ocr_safe_sql, ocr_cache_safe_article, ocr_cache_safe_sql


@pytest.mark.parametrize(("article_hash", "cache_hash", "version", "visible"), [
    ("", "", 2, True),
    ("old-ocr", "old-ocr", 2, False),
    ("", "previous-ocr", 2, False),
    ("current-ocr", "", 2, False),
    ("current-ocr", "current-ocr", 3, True),
    ("current-ocr", "current-ocr", None, False),
])
def test_ocr_derived_cache_requires_current_role_pipeline(article_hash, cache_hash, version, visible):
    with sqlite3.connect(":memory:") as conn:
        value = conn.execute(
            "WITH a(ocr_hash) AS (VALUES (?)), d(ocr_hash,prompt_version) AS (VALUES (?,?)) "
            f"SELECT {cached_ocr_safe_sql()} FROM a CROSS JOIN d", (article_hash, cache_hash, version)
        ).fetchone()[0]
    assert bool(value) is visible


def test_untrusted_sql_alias_is_rejected():
    with pytest.raises(ValueError):
        cached_ocr_safe_sql("a; invalid", "d")


@pytest.mark.parametrize("version,safe", [
    (3, True), ("3", False), (3.0, False), (True, False), (2, False), (None, False),
])
def test_ocr_role_marker_requires_the_exact_numeric_version(version, safe):
    assert ocr_cache_safe_article({"raw_meta": {"ocr_role_version": version}}) is safe
    sql = ocr_cache_safe_sql("a")
    assert "jsonb_typeof(a.raw_meta->'ocr_role_version') = 'number'" in sql
    assert "a.raw_meta->>'ocr_role_version' = '3'" in sql


def test_ocr_role_sql_alias_validation():
    assert "raw_meta->" in ocr_cache_safe_sql("")
    with pytest.raises(ValueError):
        ocr_cache_safe_sql("a; invalid")
