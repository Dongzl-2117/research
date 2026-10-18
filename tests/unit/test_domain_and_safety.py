from pathlib import Path

import pytest

from researchlib.config import DEFAULT_PUBLIC_BASE_URL, Settings, assert_bind_allowed
from researchlib.embeddings import UnavailableEmbeddingIndex
from researchlib.migrate import split_sql

ROOT = Path(__file__).resolve().parents[2]


def test_public_host_default():
    settings = Settings.from_env({})
    assert settings.public_base_url == "https://investment.zldong.com"
    assert settings.public_base_url == DEFAULT_PUBLIC_BASE_URL
    assert settings.bind_host == "127.0.0.1"
    assert settings.bind_port == 8088


def test_bind_guard():
    assert_bind_allowed("127.0.0.1", False)
    assert_bind_allowed("0.0.0.0", True)
    with pytest.raises(SystemExit):
        assert_bind_allowed("0.0.0.0", False)


def test_repo_does_not_use_old_hostname():
    banned = "research" + ".zldong.com"
    roots = [
        ROOT / "src",
        ROOT / "config",
        ROOT / "docs",
        ROOT / ".github",
        ROOT / "README.md",
        ROOT / ".env.example",
        ROOT / "docker-compose.yml",
        ROOT / "Dockerfile",
    ]
    hits = []
    for root in roots:
        paths = [root] if root.is_file() else root.rglob("*")
        for path in paths:
            if not path.is_file() or "vendor" in path.parts:
                continue
            if path.suffix.lower() in {".png", ".bz2", ".gz", ".zip"}:
                continue
            if banned in path.read_text(encoding="utf-8", errors="ignore"):
                hits.append(str(path.relative_to(ROOT)))
    assert hits == []


def test_schema_has_chinese_search_and_no_pgvector():
    sql = (ROOT / "src/researchlib/migrations/001_init.sql").read_text(encoding="utf-8")
    assert "zhparser" in sql
    assert "to_tsvector('chinese'" in sql
    assert "CREATE EXTENSION IF NOT EXISTS vector" not in sql
    assert "pgvector is not installed" in sql
    statements = split_sql(sql)
    assert any("CREATE TABLE articles" in statement for statement in statements)
    assert any("CREATE EXTENSION IF NOT EXISTS zhparser" in statement for statement in statements)


def test_embedding_seam_is_not_wired():
    index = UnavailableEmbeddingIndex()
    with pytest.raises(NotImplementedError):
        index.search("腾讯", 5)
    with pytest.raises(NotImplementedError):
        index.index_article(1, "腾讯")
