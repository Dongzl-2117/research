"""对照页质量检查：硬指标、LLM 抽验的输入输出、CLI 退出码。不连库、不调真模型。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from researchlib.translate import cli_qa, qa
from researchlib.translate.pair import BilingualPair, redundant_pairs

ALPHA = (
    "Alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu nu xi omicron pi rho sigma tau "
    "upsilon phi chi psi omega. "
) * 2
ALPHA = ALPHA.strip()
BETA = (
    "A completely different paragraph about interest rates, central banks and the way monetary policy "
    "travels through credit markets over time. "
) * 2
BETA = BETA.strip()


def report_of(en_html: str, zh_html: str, *, article_id: int = 1) -> qa.BilingualReport:
    return qa.report_from_html(article_id, "stratechery", "A test article", en_html, zh_html)


def test_clean_article_passes():
    en = "".join(
        f"<p>Paragraph {index} talks about platforms and aggregation in a sentence long enough to be a leaf.</p>"
        for index in range(3)
    )
    zh = "".join(f"<p>第{index}段讲平台和聚合，句子够长，和英文对得上。</p>" for index in range(3))
    report = report_of(en, zh)
    assert report.status == "pass"
    assert report.pairs == 3
    assert report.en_leaves == report.zh_leaves == 3
    assert report.max_en_chars >= 80
    assert report.giant == () and report.near_duplicates == () and report.long_empty_zh == ()


def test_giant_pair_is_flagged():
    long_sentence = " ".join(["word"] * 500)
    report = report_of(f"<p>{long_sentence}</p>", "<p>一整段中文，和英文对得上。</p>")
    assert report.status == "fail"
    assert report.giant == (0,)
    assert report.max_en_chars >= qa.GIANT_EN_CHARS
    assert [issue.code for issue in report.issues] == ["giant-pair"]


def test_contained_pair_left_on_page_is_flagged():
    longer = ALPHA + " Plus one more clause that makes this paragraph clearly longer than the first one."
    report = report_of(f"<p>{ALPHA}</p><p>{longer}</p>", "<p>甲段中文。</p>")
    assert report.status == "fail"
    assert report.near_duplicates == (0,)
    assert "near-duplicate" in {issue.code for issue in report.issues}
    assert "leaf-gap" in {issue.code for issue in report.issues}


def test_long_english_without_chinese_is_flagged():
    report = report_of(f"<p>{ALPHA}</p>", f"<p>{ALPHA}</p>")
    assert report.status == "fail"
    assert report.long_empty_zh == (0,)
    assert any(issue.code == "long-empty-zh" for issue in report.issues)


def test_short_missing_chinese_is_not_flagged():
    report = report_of("<p>See the chart above.</p>", "<p>　</p>")
    assert report.long_empty_zh == ()


def test_archive_header_marker_is_flagged():
    report = report_of(
        f"<p>{ALPHA}</p><p>{BETA}</p>",
        "<p>甲段中文。</p><p>乙段中文。</p><p>来源：member-rss</p>",
    )
    assert report.status == "fail"
    assert "member-rss" in report.archive_markers
    assert any(issue.code == "archive-header" for issue in report.issues)


def test_large_leaf_gap_fails_and_small_gap_warns():
    many_en = "".join(
        f"<p>Unique paragraph {index} about a different subject with enough words to be a long leaf.</p>"
        for index in range(41)
    )
    big = report_of(many_en, "<p>只有一段中文。</p>")
    assert big.status == "fail"
    assert any(issue.code == "leaf-gap" and issue.level == "fail" for issue in big.issues)

    small = report_of(f"<p>{ALPHA}</p><p>{BETA}</p>", "<p>甲段中文。</p>")
    assert small.status == "warn"
    assert any(issue.code == "leaf-gap" and issue.level == "warn" for issue in small.issues)


def test_tail_extra_leaves_do_not_count_as_long_empty():
    """多出来的英文段挂在末尾，是叶子差，不算「对上的长段没中文」。"""
    report = report_of(f"<p>{ALPHA}</p><p>{BETA}</p>", "<p>甲段中文。</p>")
    assert report.tail_en == 1
    assert report.long_empty_zh == ()


def test_redundant_pairs_matches_display_rules():
    pairs = [
        BilingualPair(en=f"<p>{ALPHA}</p>", zh="<p>甲段中文。</p>"),
        BilingualPair(en=f"<p>{ALPHA}</p>", zh="<p>甲段中文。</p>"),
    ]
    assert redundant_pairs(pairs) == [1]
    longer = BilingualPair(en=f"<p>{ALPHA} And one more sentence to be longer.</p>", zh="<p>乙段中文。</p>")
    assert redundant_pairs([pairs[0], longer]) == [0]
    assert redundant_pairs([pairs[0]]) == []


def test_review_messages_sample_flagged_pairs_and_truncate():
    long_sentence = " ".join(["word"] * 500)
    en = f"<p>{long_sentence}</p>" + "".join(f"<p>{ALPHA}</p><p>{BETA}</p>" for _ in range(4))
    zh = "<p>一整段中文。</p>" + "".join("<p>甲段中文。</p><p>乙段中文。</p>" for _ in range(4))
    report = report_of(en, zh)
    pairs = qa.bilingual_pairs(en, zh) or []
    messages = qa.build_review_messages(report, pairs, max_pairs=6, chars=120)
    payload = json.loads(messages[1]["content"])
    indexes = [item["i"] for item in payload["pairs"]]
    assert 0 in indexes
    assert len(indexes) <= 6
    assert all(len(item["en"]) <= 120 for item in payload["pairs"])
    assert any(item["truncated"] for item in payload["pairs"])
    assert payload["stats"]["giant"] == [0]
    assert "articleId" in payload and messages[0]["role"] == "system"


def test_parse_review_accepts_fenced_json_and_rejects_bad_shapes():
    parsed = qa.parse_review(
        '```json\n{"verdict":"warn","score":81,"issues":[{"index":2,"kind":"misaligned",'
        '"note":"中文对到了下一段"}],"summary":"有一处错位"}\n```'
    )
    assert parsed == {
        "verdict": "warn",
        "score": 81,
        "issues": [{"index": 2, "kind": "misaligned", "note": "中文对到了下一段"}],
        "summary": "有一处错位",
    }
    with pytest.raises(qa.QaError):
        qa.parse_review('{"verdict":"maybe","score":80}')
    with pytest.raises(qa.QaError):
        qa.parse_review('{"verdict":"pass"}')
    with pytest.raises(qa.QaError):
        qa.parse_review("not json")


class FakeConn:
    def __init__(self) -> None:
        self.close_count = 0

    def close(self) -> None:
        self.close_count += 1


@pytest.fixture
def fake_cli(monkeypatch, tmp_path):
    settings = SimpleNamespace(data_dir=tmp_path, sources_path=tmp_path / "sources.yaml")
    conn = FakeConn()
    monkeypatch.setattr(cli_qa.Settings, "from_env", staticmethod(lambda: settings))
    monkeypatch.setattr(cli_qa, "connect", lambda settings: conn)
    monkeypatch.setattr(cli_qa.qa, "translated_article_ids", lambda conn, **kwargs: pytest.fail("不该扫队列"))
    return settings, conn


def test_cli_returns_one_on_hard_failure_and_zero_on_pass(fake_cli, monkeypatch, capsys):
    pass_report = report_of(f"<p>{ALPHA}</p>", "<p>甲段中文。</p>", article_id=1)
    fail_report = report_of(" ".join(["<p>" + "word " * 500 + "</p>"]), "<p>中文。</p>", article_id=2)
    reports = {1: pass_report, 2: fail_report}
    monkeypatch.setattr(cli_qa.qa, "check_article", lambda conn, settings, article_id: reports[article_id])

    assert cli_qa.main(["--article-id", "1"]) == 0
    assert "pass" in capsys.readouterr().out

    assert cli_qa.main(["--article-id", "2"]) == 1
    out = capsys.readouterr().out
    assert "#2 stratechery FAIL" in out and "giant-pair" in out


def test_cli_strict_turns_warning_into_failure(fake_cli, monkeypatch):
    warn_report = report_of(f"<p>{ALPHA}</p><p>{BETA}</p>", "<p>甲段中文。</p>")
    monkeypatch.setattr(cli_qa.qa, "check_article", lambda conn, settings, article_id: warn_report)
    assert cli_qa.main(["--article-id", "1"]) == 0
    assert cli_qa.main(["--article-id", "1", "--strict"]) == 1


def test_cli_json_and_report_file(fake_cli, monkeypatch, tmp_path, capsys):
    report = report_of(f"<p>{ALPHA}</p>", "<p>甲段中文。</p>")
    monkeypatch.setattr(cli_qa.qa, "check_article", lambda conn, settings, article_id: report)
    path = tmp_path / "qa.json"
    assert cli_qa.main(["--article-id", "1", "--json", "--report", str(path)]) == 0
    printed = json.loads(capsys.readouterr().out)
    saved = json.loads(Path(path).read_text(encoding="utf-8"))
    assert printed == saved
    assert printed["articles"][0]["articleId"] == 1
    assert printed["summary"] == {
        "checked": 1,
        "pass": 1,
        "warn": 0,
        "fail": 0,
        "skipped": 0,
        "issues": {},
        "llm": {"checked": 0, "pass": 0, "warn": 0, "fail": 0},
    }


def test_cli_llm_is_opt_in(fake_cli, monkeypatch):
    report = report_of(f"<p>{ALPHA}</p>", "<p>甲段中文。</p>")
    monkeypatch.setattr(cli_qa.qa, "check_article", lambda conn, settings, article_id: report)
    monkeypatch.setattr(cli_qa.qa, "review_with_llm", lambda *args: pytest.fail("没加 --llm 不该调模型"))
    monkeypatch.setattr(cli_qa, "ensure_llm", lambda settings: pytest.fail("没加 --llm 不该检查凭证"))
    assert cli_qa.main(["--article-id", "1"]) == 0


def test_cli_llm_reviews_clean_articles_only(fake_cli, monkeypatch, capsys):
    clean = report_of(f"<p>{ALPHA}</p>", "<p>甲段中文。</p>", article_id=2)
    failed = report_of(" ".join(["<p>" + "word " * 500 + "</p>"]), "<p>中文。</p>", article_id=1)
    reports = {1: failed, 2: clean}
    monkeypatch.setattr(cli_qa.qa, "check_article", lambda conn, settings, article_id: reports[article_id])
    monkeypatch.setattr(cli_qa, "ensure_llm", lambda settings: None)
    monkeypatch.setattr(cli_qa.qa, "pairs_for_review", lambda conn, settings, article_id: [])
    reviewed: list[int] = []

    def review(settings, report, pairs):
        reviewed.append(report.article_id)
        return {"verdict": "pass", "score": 90, "issues": [], "summary": "对得上", "model": "deepseek-flash"}

    monkeypatch.setattr(cli_qa.qa, "review_with_llm", review)
    assert cli_qa.main(["--article-id", "1", "--article-id", "2", "--llm", "--llm-sample", "3"]) == 1
    assert reviewed == [2]
    assert "llm pass 90" in capsys.readouterr().out


def test_cli_llm_failure_returns_two(fake_cli, monkeypatch, capsys):
    report = report_of(f"<p>{ALPHA}</p>", "<p>甲段中文。</p>")
    monkeypatch.setattr(cli_qa.qa, "check_article", lambda conn, settings, article_id: report)
    monkeypatch.setattr(cli_qa, "ensure_llm", lambda settings: None)
    monkeypatch.setattr(cli_qa.qa, "pairs_for_review", lambda conn, settings, article_id: [])

    def broken(*args):
        raise qa.QaError("模型返回了别的东西")

    monkeypatch.setattr(cli_qa.qa, "review_with_llm", broken)
    assert cli_qa.main(["--article-id", "1", "--llm"]) == 2
    assert "LLM 抽验失败" in capsys.readouterr().err


def _llm_verdict(monkeypatch, verdict: str) -> None:
    monkeypatch.setattr(cli_qa, "ensure_llm", lambda settings: None)
    monkeypatch.setattr(cli_qa.qa, "pairs_for_review", lambda conn, settings, article_id: [])
    monkeypatch.setattr(
        cli_qa.qa,
        "review_with_llm",
        lambda settings, report, pairs: {"verdict": verdict, "score": 50, "issues": [], "summary": ""},
    )


def test_cli_llm_fail_sets_exit_code(fake_cli, monkeypatch):
    report = report_of(f"<p>{ALPHA}</p>", "<p>甲段中文。</p>")
    monkeypatch.setattr(cli_qa.qa, "check_article", lambda conn, settings, article_id: report)
    _llm_verdict(monkeypatch, "fail")
    assert cli_qa.main(["--article-id", "1", "--llm"]) == 1


def test_cli_llm_warn_only_fails_with_strict(fake_cli, monkeypatch):
    report = report_of(f"<p>{ALPHA}</p>", "<p>甲段中文。</p>")
    monkeypatch.setattr(cli_qa.qa, "check_article", lambda conn, settings, article_id: report)
    _llm_verdict(monkeypatch, "warn")
    assert cli_qa.main(["--article-id", "1", "--llm"]) == 0
    assert cli_qa.main(["--article-id", "1", "--llm", "--strict"]) == 1
