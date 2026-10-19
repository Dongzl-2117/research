from __future__ import annotations

import os
from pathlib import Path

import psycopg
import pytest

ROOT = Path(__file__).resolve().parents[1]


def _database_name(url: str) -> str:
    return url.rsplit("/", 1)[-1].split("?", 1)[0]


@pytest.fixture(scope="session")
def database_url() -> str:
    url = os.environ.get(
        "DATABASE_URL",
        "postgresql://research:research@127.0.0.1:5432/research_test",
    )
    name = _database_name(url)
    if name != "research_test" and os.environ.get("ALLOW_TEST_DB") != "1":
        pytest.skip(f"拒绝在数据库 {name} 上跑测试")
    try:
        conn = psycopg.connect(url, connect_timeout=3)
        conn.close()
    except Exception as exc:
        pytest.skip(f"Postgres 不可用：{exc}")
    return url


@pytest.fixture
def settings(database_url, tmp_path):
    from researchlib.config import Settings

    return Settings.from_env(
        {
            "DATABASE_URL": database_url,
            "DATA_DIR": str(tmp_path / "data"),
            "PUBLIC_BASE_URL": "https://investment.zldong.com",
            "SOURCES_FILE": str(ROOT / "config" / "sources.yaml"),
            "AIHOT_MAP_FILE": str(ROOT / "config" / "aihot_import.yaml"),
            "BIND_HOST": "127.0.0.1",
            "BIND_PORT": "8088",
            "DISPLAY_TIMEZONE": "Asia/Shanghai",
        }
    )


@pytest.fixture
def conn(settings):
    from researchlib.db import prepare

    connection = prepare(settings)
    connection.execute("TRUNCATE articles, llm_answers RESTART IDENTITY CASCADE")
    connection.commit()
    yield connection
    connection.close()
