"""实验捕获的 libpq 继承入口与超大首段占位反例；不连接远程数据库。"""
from __future__ import annotations

import os

import pytest
from psycopg import pq

from researchlib.experiments.capture import validate_dsn
from researchlib.experiments.demo import demo_snapshot
from researchlib.experiments.engine import compare
from researchlib.experiments.schema import CompareRequest, Tuning

# 覆盖当前 libpq 列出的全部环境变量，并覆盖单独的服务文件查找入口和未来 PG* 名称。
PG_ENV_NAMES = sorted({option.envvar.decode() for option in pq.Conninfo.get_defaults() if option.envvar} | {
    "PGSERVICEFILE", "PGSYSCONFDIR", "PGREQUIRESSL", "PGFUTUREOPTION",
})


@pytest.fixture
def clean_pg_environment(monkeypatch):
    for name in tuple(os.environ):
        if name.startswith("PG"):
            monkeypatch.delenv(name)


@pytest.mark.parametrize("name", PG_ENV_NAMES)
def test_inherited_libpq_settings_rejected_without_changing_environment(clean_pg_environment, monkeypatch, name):
    value = "127.0.0.2" if name in {"PGHOST", "PGHOSTADDR"} else "local-regression-only"
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match="PG") as error:
        validate_dsn("host=127.0.0.1 port=5432 dbname=research_test")
    assert value not in str(error.value)  # 凭据与文件路径的值不进入错误信息。
    assert os.environ[name] == value


@pytest.mark.parametrize("name", ["PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE", "PGSYSCONFDIR", "PGOPTIONS"])
def test_capture_rejects_inheritance_before_file_or_connection_io(clean_pg_environment, monkeypatch, tmp_path, name):
    import researchlib.experiments.capture as module
    value = "127.0.0.2" if name == "PGHOSTADDR" else str(tmp_path / "local-service-only")
    monkeypatch.setenv(name, value)
    calls = []
    def forbidden_connect(**kwargs):
        calls.append(kwargs)
        raise AssertionError("不应尝试任何连接")
    monkeypatch.setattr(module.psycopg, "connect", forbidden_connect)
    with pytest.raises(ValueError, match="PG"):
        module.capture("host=127.0.0.1 port=5432 dbname=research_test", tmp_path / "not-read.yaml", [])
    assert calls == [] and os.environ[name] == value


@pytest.mark.parametrize("host,port,address,expected_port", [
    ("127.0.0.1", None, "127.0.0.1", "5432"),
    ("::1", None, "::1", "5432"),
    ("localhost", "65432", "127.0.0.1", "65432"),
    ("127.0.0.1", "05432", "127.0.0.1", "5432"),
])
def test_connection_target_pins_numeric_loopback_and_single_port(
    clean_pg_environment, host, port, address, expected_port,
):
    dsn = f"host={host} dbname=research_test" + (f" port={port}" if port is not None else "")
    params = validate_dsn(dsn)
    assert params["hostaddr"] == address and params["port"] == expected_port
    assert params["dbname"] == "research_test"


@pytest.mark.parametrize("port", ["0", "-1", "65536", "5432,5433", "notaport", ""])
def test_ambiguous_or_invalid_port_rejected(clean_pg_environment, port):
    with pytest.raises(ValueError, match="端口"):
        validate_dsn(f"host=127.0.0.1 dbname=research_test port='{port}'")


@pytest.mark.parametrize("passages", [1, 2, 10])
def test_oversize_best_passage_cannot_consume_small_passages_budget(passages):
    snapshot = demo_snapshot()
    case = snapshot.cases[0]
    # 两个检索词均出现，大段词频更高，保证它在未过滤的候选池中排第一。
    large = case.qa_rows[0].model_copy(update={"body": "会员制零售" * 1000, "article_id": 1})
    small = case.qa_rows[1].model_copy(update={
        "body": "会员制零售通过年费支持稳定低价，现金流需要覆盖租金。", "article_id": 2,
    })
    frozen = case.model_copy(update={"rows": [large, small], "qa_rows": [large, small]})
    snapshot = snapshot.model_copy(update={"cases": [frozen]})
    value = compare(snapshot, CompareRequest(
        case_ids=[case.spec.id], baseline=Tuning(chars=1000, passages=passages),
        candidate=Tuning(chars=1000, passages=passages),
    ))["cases"][0]
    for side in ("baseline", "candidate"):
        assert value[side]["search"][0]["article_id"] == large.article_id
        materials = value[side]["passages"]
        assert len(materials) == 1 and materials[0]["article_id"] == small.article_id
        assert materials[0]["text"] == small.body  # 完整原文，不能以截大段凑预算。
        assert large.body not in value[side]["messages"][1]["content"]
        assert value[side]["generation_eligible"]
        assert value[side]["metrics"]["passages"] == 1
        assert value[side]["metrics"]["material_chars"] <= 1000


def test_cumulative_budget_selects_whole_small_passages_without_oversize():
    snapshot = demo_snapshot()
    case = snapshot.cases[0]
    rows = [case.qa_rows[0].model_copy(update={"body": "会员制零售" * 1000})]
    for index, character in enumerate(("甲", "乙", "丙"), 2):
        rows.append(case.qa_rows[1].model_copy(update={
            "article_id": index, "body": "会员制零售" + character * (600 - len("会员制零售")),
        }))
    case = case.model_copy(update={"rows": rows, "qa_rows": rows})
    result = compare(snapshot.model_copy(update={"cases": [case]}), CompareRequest(
        case_ids=[case.spec.id], baseline=Tuning(chars=1000), candidate=Tuning(chars=1200),
    ))["cases"][0]
    assert len(result["baseline"]["passages"]) == 1
    assert len(result["candidate"]["passages"]) == 2
    for side in ("baseline", "candidate"):
        assert all(len(p["text"]) == 600 for p in result[side]["passages"])
        assert all(p["article_id"] != 1 for p in result[side]["passages"])
