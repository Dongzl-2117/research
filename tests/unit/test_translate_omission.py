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
    found, dropped = omission.parse_findings(data, batch)
    assert found == {1: ["after a different customer group reduced demand",
                         "After a different customer group reduced demand."]}
    assert dropped == ["made-up words"]
    # 弯引号、破折号和大小写不影响找出处。
    curly = [omission.Pair(4, "It’s the “best” plan — for now.", "这是目前最好的计划。")]
    assert omission.parse_findings({"t": [{"i": 0, "missing": ['it\'s the "best" plan - for now']}]}, curly) == (
        {4: ['it\'s the "best" plan - for now']}, [])
    assert omission.parse_findings({"t": []}, batch) == ({}, [])
    assert omission.parse_findings("not a dict", batch) == ({}, [])


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
    assert result.failures == ["DeepSeek 返回非 JSON"]
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
    assert result.failures == ["Content Exists Risk"]


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
    # 没回执的旧稿按页面配对；这里的页面默认没有并排的段对。
    monkeypatch.setattr(cli_qa.qa, "pairs_for_review", lambda conn, settings, article_id: [])
    monkeypatch.setattr(omission, "chat_json", lambda **kwargs: _reply([{"i": 2, "missing": ["regional logistics"]}]))
    return conn


def test_cli_prints_findings_rate_and_usage_without_writing(omission_cli, capsys):
    assert cli_qa.main(["--omissions", "--article-id", "1", "--article-id", "2"]) == 0
    out = capsys.readouterr().out
    assert "#1 pairs=3 flagged=1" in out and "[2] 漏：regional logistics" in out and "中文：丙公司" in out
    assert "#2 跳过：没回执，页面也没有并排的段对" in out
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
    assert payload["articles"][0]["dropped"] == [] and payload["articles"][0]["failures"] == []
    assert json.loads(path.read_text(encoding="utf-8")) == payload


def test_cli_shows_dropped_quotes_and_why_a_batch_was_not_checked(omission_cli, monkeypatch, capsys):
    replies = iter([
        _reply([{"i": 0, "missing": ["a sentence the source never had"]}]),
        LLMHTTPError(400, "DeepSeek HTTP 400: Content Exists Risk"),
    ])

    def fake(**_kwargs):
        reply = next(replies)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(omission, "chat_json", fake)
    monkeypatch.setattr(omission, "batches", lambda items: [items[:1], items[1:]])
    assert cli_qa.main(["--omissions", "--article-id", "1"]) == 0
    out = capsys.readouterr().out
    assert "摘句在原文里找不到、丢掉：a sentence the source never had" in out
    assert "这一批没核：DeepSeek HTTP 400: Content Exists Risk" in out
    assert "模型摘的原文找不到、丢掉 1 条；1 批回答用不了、没核" in out


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


def test_a_legacy_page_is_checked_by_the_pairs_it_shows(omission_cli, monkeypatch, capsys):
    """没回执的旧稿：按页面上并排的段对核。收起的重复、图不送；只有英文的对不送模型，只计个数。"""
    shown = [
        SimpleNamespace(en="<p>Alpha Corp sells chips.</p>", zh="<p>甲公司卖芯片。</p>", repeat_of=None),
        SimpleNamespace(en="<p>Alpha Corp sells chips.</p>", zh="<p>甲公司卖芯片。</p>", repeat_of=0),
        SimpleNamespace(
            en="<p>Beta holds 30 dollars for regional logistics.</p>", zh="<p>乙持有30美元。</p>", repeat_of=None,
        ),
        SimpleNamespace(en="<p>A paragraph nobody translated.</p>", zh=None, repeat_of=None),
        SimpleNamespace(en='<p><img src="x.png"></p>', zh='<p><img src="x.png"></p>', repeat_of=None),
    ]
    monkeypatch.setattr(cli_qa.qa, "pairs_for_review", lambda conn, settings, article_id: shown)
    asked = []

    def reviewer(**kwargs):
        asked.append([pair["en"] for pair in kwargs["user"]["pairs"]])
        return _reply([{"i": 1, "missing": ["regional logistics"]}])

    monkeypatch.setattr(omission, "chat_json", reviewer)
    assert cli_qa.main(["--omissions", "--article-id", "2"]) == 0
    out = capsys.readouterr().out
    assert asked == [["Alpha Corp sells chips.", "Beta holds 30 dollars for regional logistics."]]
    assert "#2 pairs=2 flagged=1 按页面配对 只有英文 1 对" in out and "[2] 漏：regional logistics" in out
    assert "其中 1 篇没回执，按页面上并排的段对核；页面上只有英文、没有中文的 1 对（整段没译，没送模型）" in out


def test_spread_picks_evenly_and_keeps_the_order():
    assert cli_qa._spread(list(range(10)), 3) == [0, 3, 6]
    assert cli_qa._spread([5, 4], 3) == [5, 4]


def test_cli_samples_a_category_from_the_inventory(omission_cli, monkeypatch, tmp_path, capsys):
    path = tmp_path / "status" / "translate-inventory.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "generatedAt": "2026-11-09T01:00:00Z", "ids": {}, "display": {"legacy-structural": [1, 9, 8, 7]},
    }), encoding="utf-8")
    seen: list[int] = []
    monkeypatch.setattr(
        cli_qa.qa, "pairs_for_review", lambda conn, settings, article_id: seen.append(article_id) or [],
    )
    assert cli_qa.main(["--omissions", "--from-inventory", "legacy-structural", "--limit", "2"]) == 0
    captured = capsys.readouterr()
    assert "legacy-structural 共 4 篇，均匀抽 2 篇" in captured.err
    # 1 有回执，照回执核；8 没回执、页面上没有并排的段对，跳过。
    assert "#1 pairs=3 flagged=1" in captured.out and "#8 跳过" in captured.out and seen == [8]
    assert cli_qa.main(["--from-inventory", "legacy-structural"]) == 2
    assert "只和 --omissions 一起用" in capsys.readouterr().err
    assert cli_qa.main(["--omissions", "--from-inventory", "nope"]) == 2
    assert "没有「nope」这一类" in capsys.readouterr().err
