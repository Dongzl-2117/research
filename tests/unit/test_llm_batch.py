"""花模型钱的批量命令共用的那几件：结果行里的用量、什么错整批停、--workers 的范围和封顶。"""

import argparse
from types import SimpleNamespace

import pytest

from researchlib import llm_batch
from researchlib.llm import LLMError, LLMUnavailable


def test_batch_line_reports_tokens_and_time():
    assert llm_batch.spent({"prompt": 3210, "completion": 6540, "reasoning": 6120}, 42.4) == (
        "进 3,210 出 6,540（其中思考 6,120） token，用时 42 秒"
    )
    # 接口没报用量时只印用时。
    assert llm_batch.spent({}, 3725) == "用时 1 小时 2 分"


def test_total_line_mentions_skipped_articles_only_when_there_are_some():
    report = {"skipped": 0, "usage": {"prompt": 2000, "completion": 800}, "seconds": 65}
    assert llm_batch.tail(report, "写") == " · 合计进 2,000 出 800 token，用时 1 分 5 秒"
    report["skipped"] = 3
    assert llm_batch.tail(report, "写").startswith("，跳过 3 篇（别处正在写或已经写过） · 合计")


def test_batch_stops_on_credentials_and_balance_but_not_on_one_bad_article():
    assert llm_batch.fatal(LLMUnavailable("没有配置 DEEPSEEK_API_KEY"))
    assert llm_batch.fatal(LLMError("DeepSeek 接口返回 402：Insufficient Balance"))
    assert llm_batch.fatal(LLMError("DeepSeek 接口返回 401：invalid key"))
    assert not llm_batch.fatal(LLMError("DeepSeek 接口返回 500：upstream"))
    assert not llm_batch.fatal(LLMError("模型没有按格式回答"))


def test_rate_limits_server_errors_and_dropped_connections_pause_instead_of_failing_articles():
    for text in (
        "DeepSeek 接口返回 429：rate limited",
        "Grok 接口返回 503：overloaded",
        "连不上 DeepSeek 接口：timed out",
        "连不上 Grok 接口：stream ended before [DONE]",
    ):
        assert llm_batch.transient(LLMError(text)) and not llm_batch.fatal(LLMError(text))
    for text in ("DeepSeek 接口返回 400：bad request", "DeepSeek 接口返回 402：no balance", "模型没有按格式回答"):
        assert not llm_batch.transient(LLMError(text))
    assert not llm_batch.transient(LLMUnavailable("没有配置 DEEPSEEK_API_KEY"))


def test_exit_code_says_why_the_batch_stopped(capsys):
    done = {"fatal": "", "paused": ""}
    assert llm_batch.exit_code(done, "写") == 0
    assert llm_batch.exit_code({**done, "fatal": "DeepSeek 接口返回 402"}, "写") == 2
    paused = {**done, "paused": "连不上 DeepSeek 接口：timed out"}
    assert llm_batch.exit_code(paused, "写") == llm_batch.EXIT_PAUSED == 3
    err = capsys.readouterr().err
    assert "整批停下：DeepSeek 接口返回 402" in err and "写过的不会重来" in err


def _args(*argv: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    llm_batch.add_arguments(parser, "写")
    return parser.parse_args(list(argv))


def test_worker_count_must_be_in_range():
    for count in ("0", str(llm_batch.MAX_WORKERS + 1)):
        with pytest.raises(SystemExit) as stopped:
            llm_batch.check_arguments(_args("--workers", count))
        assert "--workers" in str(stopped.value)
    llm_batch.check_arguments(_args("--workers", str(llm_batch.MAX_WORKERS)))


def test_workers_are_capped_by_llm_concurrency_and_off_peak_only_applies_to_deepseek(capsys):
    deepseek = SimpleNamespace(llm_concurrency=8, llm_provider="deepseek")
    assert llm_batch.resolve(deepseek, _args("--workers", "16", "--off-peak"), "写") == (8, True)
    assert "RESEARCH_LLM_CONCURRENCY=16" in capsys.readouterr().err
    assert llm_batch.resolve(deepseek, _args("--workers", "4"), "写") == (4, False)
    # 别家没有高峰价，--off-peak 不起作用。
    grok = SimpleNamespace(llm_concurrency=16, llm_provider="grok")
    assert llm_batch.resolve(grok, _args("--workers", "16", "--off-peak"), "写") == (16, False)
    assert capsys.readouterr().err == ""


def test_digest_command_rejects_worker_counts_out_of_range():
    from researchlib.digest import main

    with pytest.raises(SystemExit) as stopped:
        main(["--workers", "0"])
    assert "--workers" in str(stopped.value)
