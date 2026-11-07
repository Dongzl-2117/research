"""段内漏句抽查：回执取段、分批、摘句核对、用量累加、命令行。合成样本，不连库、不调真模型。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from test_bilingual_regressions import ZA, ZB, ZC, A, B, C

from researchlib.translate import cli_qa, omission
from researchlib.translate import provenance as pr
from researchlib.translate.llm import ChatJsonResult, LLMHTTPError, LLMTransportError

USAGE = {"prompt_tokens": 1000, "prompt_cache_hit_tokens": 400, "completion_tokens": 300,
         "completion_tokens_details": {"reasoning_tokens": 250}}


def _receipt(untranslated=()):
    zh = ZA + (B if 1 in untranslated else ZB) + ZC
    return pr.receipt(pr.source_manifest(A + B + C), zh, untranslated=list(untranslated))


def _reply(rows, usage=USAGE):
    return ChatJsonResult(data={"t": rows}, model="synthetic-model", usage=usage, latency_ms=0)


def test_pairs_come_from_the_receipt_and_skip_english_gaps():
    pairs = omission.receipt_pairs(_receipt(untranslated=[1]))
    assert [pair.ordinal for pair in pairs] == [0, 2]
    assert pairs[0].en.startswith("Acme earned 10 dollars") and pairs[0].zh.startswith("甲公司")
    assert omission.receipt_pairs({}) == [] and omission.receipt_pairs(None) == []


def test_batches_cap_pairs_and_characters():
    pairs = [omission.Pair(index, "word " * 10, "中文") for index in range(30)]
    assert [len(batch) for batch in omission.batches(pairs)] == [12, 12, 6]
    long = [omission.Pair(0, "x" * 9000, "中"), omission.Pair(1, "short", "短")]
    assert [len(batch) for batch in omission.batches(long)] == [1, 1]


def test_only_quotes_found_in_the_english_count():
    batch = omission.receipt_pairs(_receipt())
    data = {"t": [
        {"i": 1, "missing": ["after a different customer group reduced demand", "made-up words"]},
        {"i": 1, "missing": ["After a different customer group reduced demand."]},  # 同一句只记一次
        {"i": 7, "missing": ["Acme earned"]},  # 下标越界
        {"i": True, "missing": ["Acme earned"]},  # 布尔不是下标
        {"i": 0, "missing": "Acme earned"},  # 不是数组
    ]}
    found, unverified = omission.parse_findings(data, batch)
    assert found == {1: ["after a different customer group reduced demand",
                         "After a different customer group reduced demand."]}
    assert unverified == 1
    # 弯引号、破折号和大小写不影响找出处。
    curly = [omission.Pair(4, "It’s the “best” plan — for now.", "这是目前最好的计划。")]
    assert omission.parse_findings({"t": [{"i": 0, "missing": ['it\'s the "best" plan - for now']}]}, curly) == (
        {4: ['it\'s the "best" plan - for now']}, 0)
    assert omission.parse_findings({"t": []}, batch) == ({}, 0)
    assert omission.parse_findings("not a dict", batch) == ({}, 0)


def test_review_counts_usage_and_skips_unusable_batches(monkeypatch):
    calls = []

    def fake(**kwargs):
        pairs = kwargs["user"]["pairs"]
        calls.append([pair["en"][:4] for pair in pairs])
        if len(calls) == 2:
            raise RuntimeError("DeepSeek 返回非 JSON")
        return _reply([{"i": 1, "missing": ["Beta lost 20 dollars"]}])

    monkeypatch.setattr(omission, "chat_json", fake)
    pairs = [*omission.receipt_pairs(_receipt()), omission.Pair(9, "Delta kept 40 dollars.", "丁公司保留了40美元。")]
    monkeypatch.setattr(omission, "batches", lambda items: [items[:2], items[2:]])
    result = omission.review_pairs(7, pairs)
    assert calls == [["Acme", "Beta"], ["Gamm", "Delt"]]
    assert result.findings == {1: ["Beta lost 20 dollars"]}
    assert result.failed_batches == 1 and result.pairs == 4 and result.checked == 2
    assert result.usage.as_dict() == {"requests": 1, "prompt": 1000, "cacheHit": 400, "completion": 300,
                                      "reasoning": 250}


@pytest.mark.parametrize("error", [LLMTransportError("断线"), LLMHTTPError(402, "Insufficient Balance")])
def test_service_failures_stop_the_review(monkeypatch, error):
    def fake(**_kwargs):
        raise error

    monkeypatch.setattr(omission, "chat_json", fake)
    with pytest.raises(type(error)):
        omission.review_pairs(7, omission.receipt_pairs(_receipt()))


def test_a_refused_batch_is_skipped_not_fatal(monkeypatch):
    def fake(**_kwargs):
        raise LLMHTTPError(400, "Content Exists Risk")

    monkeypatch.setattr(omission, "chat_json", fake)
    result = omission.review_pairs(7, omission.receipt_pairs(_receipt()))
    assert result.failed_batches == 1 and result.checked == 0 and result.findings == {}


class FakeConn:
    def __init__(self, receipts):
        self.receipts = receipts
        self.sql: list[str] = []

    def execute(self, sql, params=None):
        self.sql.append(sql)
        row = {"provenance": self.receipts.get(params[0])} if params[0] in self.receipts else None
        return SimpleNamespace(fetchone=lambda: row)

    def close(self):
        pass


@pytest.fixture
def omission_cli(monkeypatch, tmp_path):
    settings = SimpleNamespace(data_dir=tmp_path, sources_path=tmp_path / "sources.yaml")
    conn = FakeConn({1: _receipt(), 2: {}})
    monkeypatch.setattr(cli_qa.Settings, "from_env", staticmethod(lambda: settings))
    monkeypatch.setattr(cli_qa, "connect", lambda settings: conn)
    monkeypatch.setattr(cli_qa, "ensure_llm", lambda settings: None)
    monkeypatch.setattr(cli_qa.qa, "check_article", lambda *args: pytest.fail("漏句抽查不跑硬指标"))
    monkeypatch.setattr(omission, "chat_json", lambda **kwargs: _reply([{"i": 2, "missing": ["regional logistics"]}]))
    return conn


def test_cli_prints_findings_rate_and_usage_without_writing(omission_cli, capsys):
    assert cli_qa.main(["--omissions", "--article-id", "1", "--article-id", "2"]) == 0
    out = capsys.readouterr().out
    assert "#1 pairs=3 flagged=1" in out and "[2] 漏：regional logistics" in out and "中文：丙公司" in out
    assert "#2 跳过：没有回执" in out
    assert "核对 1 篇 3 段：1 段疑似漏句（33.3%）；1 篇跳过" in out
    assert "输入 1,000 token（缓存命中 400），输出 300 token（思考 250）" in out
    assert all(sql.lstrip().upper().startswith("SELECT") for sql in omission_cli.sql)


def test_cli_json_report(omission_cli, capsys, tmp_path):
    path = tmp_path / "omissions.json"
    assert cli_qa.main(["--omissions", "--article-id", "1", "--json", "--report", str(path)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"]["flagged"] == 1 and payload["summary"]["pairs"] == 3
    assert payload["articles"][0]["findings"][0] == {"ordinal": 2, "missing": ["regional logistics"],
                                                     "zh": "丙公司持有30美元现金，计划投资区域物流设施。"}
    assert json.loads(path.read_text(encoding="utf-8")) == payload


def test_cli_stops_with_two_on_service_errors(omission_cli, monkeypatch, capsys):
    def broken(**_kwargs):
        raise LLMHTTPError(402, "Insufficient Balance")

    monkeypatch.setattr(omission, "chat_json", broken)
    assert cli_qa.main(["--omissions", "--article-id", "1"]) == 2
    assert "抽查停下" in capsys.readouterr().err


def test_cli_needs_llm_credentials(omission_cli, monkeypatch, capsys):
    def missing(_settings):
        raise cli_qa.LLMUnavailable("没有 DEEPSEEK_API_KEY")

    monkeypatch.setattr(cli_qa, "ensure_llm", missing)
    assert cli_qa.main(["--omissions", "--article-id", "1"]) == 2
    assert "LLM 不可用" in capsys.readouterr().err
