"""research-translate：按队列译、单篇失败不停、--loop 不空转、--recheck、--dry-run。不连库、不调真 LLM。"""

from __future__ import annotations

import json
import re
from types import SimpleNamespace

import pytest

from researchlib.translate import cli
from researchlib.translate.service import TranslateResult


class _Rows:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row


class FakeConn:
    """记下执行过的语句。占锁的语句交回「占到了」；held 是几条连接共用的「库里的锁」。"""

    def __init__(self, held=None):
        self.rolled_back = 0
        self.closed = False
        self.statements = []
        self.held = held if held is not None else {}

    def execute(self, sql, params=None):
        self.statements.append(sql)
        if "pg_try_advisory_lock" in sql:
            owner = self.held.setdefault(params[1], self)
            return _Rows({"locked": owner is self})
        if "pg_advisory_unlock" in sql:
            if self.held.get(params[1]) is self:
                del self.held[params[1]]
            return _Rows({"pg_advisory_unlock": True})
        return _Rows()

    def commit(self):
        pass

    def rollback(self):
        self.rolled_back += 1

    def close(self):
        self.closed = True


class Slept(Exception):
    """--loop 睡下去时抛出来，结束测试里的循环。"""


@pytest.fixture
def conn(monkeypatch, tmp_path):
    fake = FakeConn()
    monkeypatch.setattr(cli.Settings, "from_env", classmethod(lambda klass: SimpleNamespace(data_dir=tmp_path)))
    monkeypatch.setattr(cli, "llm_provider", lambda: "deepseek")
    monkeypatch.setattr(cli, "deepseek_api_key", lambda: "sk-test")
    monkeypatch.setattr(cli, "prepare", lambda settings: fake)
    monkeypatch.setattr(cli, "connect", lambda settings: fake)
    return fake


def _translate(seen, *, fail=(), status="translated", reuse_seen=None):
    def fake(conn, article_id, *, data_dir, reuse=True):
        seen.append(article_id)
        if reuse_seen is not None:
            reuse_seen.append(reuse)
        if article_id in fail:
            raise RuntimeError("对齐失败")
        return TranslateResult(article_id, status, segments=2)

    return fake


def test_translates_the_pending_queue_once(conn, monkeypatch):
    seen: list[int] = []

    def pending(conn, *, limit, source_id=None):
        assert source_id == "stratechery" and limit == 2
        return [21, 22]

    monkeypatch.setattr(cli, "list_pending_english", pending)
    monkeypatch.setattr(cli, "translate_article", _translate(seen))
    assert cli.main(["--source", "stratechery", "--limit", "2"]) == 0
    assert seen == [21, 22] and conn.closed


def test_a_request_failure_prints_what_failed(conn, monkeypatch, capsys):
    """partial 且 request-failed 时把那一次的报错印出来，分得清断线、限流还是欠费。"""

    def fake(conn, article_id, *, data_dir, reuse=True):
        return TranslateResult(article_id, "partial", segments=36, reason="request-failed", untranslated=184,
                               error="连不上 DeepSeek 接口：timed out")

    monkeypatch.setattr(cli, "translate_article", fake)
    assert cli.main(["--article-id", "41"]) == 0
    out = capsys.readouterr().out
    assert "#41 partial segments=36 untranslated=184 (request-failed) 连不上 DeepSeek 接口：timed out" in out


def test_a_failed_article_does_not_stop_the_rest(conn, monkeypatch, capsys):
    seen: list[int] = []
    monkeypatch.setattr(cli, "list_pending_english", lambda conn, *, limit, source_id=None: [31, 32])
    monkeypatch.setattr(cli, "translate_article", _translate(seen, fail={31}))
    assert cli.main(["--limit", "2"]) == 1
    assert seen == [31, 32] and conn.rolled_back == 1
    assert "#31 failed: 对齐失败" in capsys.readouterr().err


@pytest.mark.parametrize("outcome", ["raises", "skipped", "partial"])
def test_loop_sleeps_instead_of_retrying_the_same_article(conn, monkeypatch, outcome):
    """队列里只剩这一轮失败、没正文或没译全的那篇：先睡，不接着重试花模型的钱。"""
    seen: list[int] = []
    slept: list[float] = []

    def sleep(seconds):
        slept.append(seconds)
        raise Slept

    monkeypatch.setattr(cli, "list_pending_english", lambda conn, *, limit, source_id=None: [51])
    if outcome == "raises":
        monkeypatch.setattr(cli, "translate_article", _translate(seen, fail={51}))
    else:
        monkeypatch.setattr(cli, "translate_article", _translate(seen, status=outcome))
    monkeypatch.setattr(cli.time, "sleep", sleep)
    with pytest.raises(Slept):
        cli.main(["--loop", "--limit", "1", "--sleep", "30"])
    assert seen == [51] and slept == [30]


def test_rejected_article_is_not_retried_after_another_sleep(conn, monkeypatch):
    seen = []
    slept = []

    def sleep(seconds):
        slept.append(seconds)
        if len(slept) == 2:
            raise Slept

    monkeypatch.setattr(cli, "list_pending_english", lambda conn, **kwargs: [51])
    monkeypatch.setattr(cli, "translate_article", _translate(seen, fail={51}))
    monkeypatch.setattr(cli.time, "sleep", sleep)
    with pytest.raises(Slept):
        cli.main(["--loop", "--limit", "1", "--sleep", "30"])
    assert seen == [51] and slept == [30, 30]
    assert sum("INSERT INTO translation_attempts" in sql for sql in conn.statements) == 1


def test_recheck_dry_run_never_calls_database_prepare(conn, monkeypatch):
    monkeypatch.setattr(cli, "prepare", lambda *args: pytest.fail("只读检查不能启动迁移或维护"))
    monkeypatch.setattr(cli, "list_coarse_translations", lambda *args, **kwargs: [])
    assert cli.main(["--recheck", "--dry-run"]) == 0
    assert conn.statements == ["SET TRANSACTION READ ONLY"]


def test_recheck_takes_coarse_rows_not_the_pending_queue(conn, monkeypatch):
    seen: list[int] = []

    def coarse(conn, *, limit, source_id=None):
        assert source_id == "stratechery"
        return [{"id": 21118, "source": "stratechery", "reason": "segments-below-leaves"}]

    def pending(conn, *, limit, source_id=None):
        raise AssertionError("recheck 不该去待译队列")

    monkeypatch.setattr(cli, "list_coarse_translations", coarse)
    monkeypatch.setattr(cli, "list_pending_english", pending)
    monkeypatch.setattr(cli, "translate_article", _translate(seen))
    assert cli.main(["--source", "stratechery", "--recheck", "--limit", "5"]) == 0
    assert seen == [21118]


def test_recheck_dry_run_lists_ids_without_translating(conn, monkeypatch, capsys):
    rows = [
        {"id": 21118, "source": "stratechery", "reason": "segments-below-source"},
        {"id": 21133, "source": "stratechery", "reason": "giant-leaf"},
    ]
    monkeypatch.setattr(cli, "list_coarse_translations", lambda conn, *, limit, source_id=None: rows)
    monkeypatch.setattr(cli, "translate_article", lambda *args, **kwargs: pytest.fail("dry-run 不调模型"))
    assert cli.main(["--recheck", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "#21118 stratechery segments-below-source" in out and "共 2 篇" in out
    assert cli.main(["--dry-run"]) == 2


def test_missing_key_stops_before_touching_the_database(conn, monkeypatch, capsys):
    monkeypatch.setattr(cli, "deepseek_api_key", lambda: "")
    monkeypatch.setattr(cli, "prepare", lambda settings: pytest.fail("没有 key 不该连库"))
    assert cli.main([]) == 2
    assert "DEEPSEEK_API_KEY" in capsys.readouterr().err


def _deploy_waits(data_dir, sha="b66bfe88885831a0", age_s=0):
    """scripts/deploy.sh 请译制让路时写的文件；age_s 是多久以前写的。"""
    import os
    import time

    path = data_dir / "status" / ".deploy-waiting"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(sha + "\n", encoding="utf-8")
    when = time.time() - age_s
    os.utime(path, (when, when))
    return path


def test_queue_run_stops_before_starting_when_a_release_is_waiting(conn, monkeypatch, tmp_path, capsys):
    """--loop 自己不会跑完，以前开着就把之后的合并一直挡在外面：在等上线的话一篇都不开。"""
    _deploy_waits(tmp_path)
    monkeypatch.setattr(cli, "prepare", lambda settings: pytest.fail("在等上线就不连库、不跑迁移"))
    monkeypatch.setattr(cli, "translate_article", lambda *args, **kwargs: pytest.fail("不该再调模型"))
    assert cli.main(["--loop", "--limit", "5", "--workers", "4"]) == 0
    assert (
        "自动上线在等 b66bfe8：不再开新的一篇，手上的译完就退；"
        "/health 报出新提交后再跑 research-translate --loop --limit 5 --workers 4"
    ) in capsys.readouterr().out


def test_queue_run_finishes_the_article_in_hand_then_makes_way(conn, monkeypatch, tmp_path):
    seen: list[int] = []

    def translate(conn, article_id, *, data_dir, reuse=True):
        seen.append(article_id)
        # 译第一篇的时候自动上线来请它停。
        _deploy_waits(tmp_path)
        return TranslateResult(article_id, "translated", segments=2)

    monkeypatch.setattr(cli, "list_pending_english", lambda conn, **kwargs: [61, 62, 63])
    monkeypatch.setattr(cli, "translate_article", translate)
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: pytest.fail("让路时不该睡下等"))
    assert cli.main(["--loop", "--limit", "3"]) == 0
    assert seen == [61]


def test_idle_loop_makes_way_after_waking(conn, monkeypatch, tmp_path):
    slept: list[float] = []

    def sleep(seconds):
        slept.append(seconds)
        _deploy_waits(tmp_path)

    monkeypatch.setattr(cli, "list_pending_english", lambda conn, **kwargs: [])
    monkeypatch.setattr(cli, "translate_article", lambda *args, **kwargs: pytest.fail("队列是空的"))
    monkeypatch.setattr(cli.time, "sleep", sleep)
    assert cli.main(["--loop", "--sleep", "30"]) == 0
    assert slept == [30]


def test_named_articles_and_stale_requests_are_translated_anyway(conn, monkeypatch, tmp_path):
    seen: list[int] = []
    monkeypatch.setattr(cli, "translate_article", _translate(seen))
    # 点名的几篇译完再说。
    _deploy_waits(tmp_path)
    assert cli.main(["--article-id", "71", "--article-id", "72"]) == 0
    assert seen == [71, 72]
    # 半小时没人重写：自动上线已经不在等了（上线完、或者没在跑），照常译。
    _deploy_waits(tmp_path, age_s=31 * 60)
    monkeypatch.setattr(cli, "list_pending_english", lambda conn, **kwargs: [73])
    assert cli.main(["--limit", "1"]) == 0
    assert seen == [71, 72, 73]


def test_fresh_turns_off_reuse_and_partial_counts_are_printed(conn, monkeypatch, capsys):
    reuse_seen: list[bool] = []
    monkeypatch.setattr(cli, "translate_article", _translate([], reuse_seen=reuse_seen))
    assert cli.main(["--article-id", "7"]) == 0
    assert cli.main(["--article-id", "7", "--fresh"]) == 0
    assert reuse_seen == [True, False]

    def partial(conn, article_id, *, data_dir, reuse=True):
        return TranslateResult(article_id, "partial", segments=5, untranslated=2, reused=3)

    monkeypatch.setattr(cli, "translate_article", partial)
    assert cli.main(["--article-id", "8"]) == 0
    assert "#8 partial segments=5 reused=3 untranslated=2" in capsys.readouterr().out


def _interrupted(article_id, untranslated=16):
    return TranslateResult(
        article_id, "partial", segments=200, reason="request-failed", untranslated=untranslated,
        error="连不上 DeepSeek 接口（同一请求试了 5 次，各连了 300、300、300、300、300 秒）：ssl eof",
        retry_later=True,
    )


def test_named_articles_cut_off_by_the_service_are_filled_in_after_a_pause(conn, monkeypatch, capsys):
    """点名补译被断线打断的篇，最后等 2 分钟自动再补一轮；已译的段存着，再补只问没译出来的段。"""
    seen: list[tuple[int, bool]] = []
    slept: list[float] = []

    def translate(conn, article_id, *, data_dir, reuse=True):
        seen.append((article_id, reuse))
        if article_id == 81 and len(seen) == 1:
            return _interrupted(81)
        return TranslateResult(article_id, "translated", segments=200, reused=184 if len(seen) > 2 else 0)

    monkeypatch.setattr(cli, "translate_article", translate)
    monkeypatch.setattr(cli.time, "sleep", slept.append)
    assert cli.main(["--article-id", "81", "--article-id", "82"]) == 0
    assert seen == [(81, True), (82, True), (81, True)]
    assert sum(slept) == cli.NAMED_RETRY_PAUSES[0] and max(slept) <= cli.PAUSE_STEP
    out = capsys.readouterr().out
    assert "#81 partial segments=200 untranslated=16 (request-failed) 连不上 DeepSeek 接口（同一请求试了 5 次" in out
    assert "#81 被打断没译完，2 分钟后再补一轮" in out
    assert out.rstrip().endswith("#81 translated segments=200 reused=184")


def test_named_articles_still_cut_off_after_two_rounds_print_the_command_to_rerun(conn, monkeypatch, capsys):
    from researchlib.translate.llm import LLMTransportError

    seen: list[int] = []
    slept: list[float] = []

    def translate(conn, article_id, *, data_dir, reuse=True):
        seen.append(article_id)
        if article_id == 91:
            # 一段都没译出来时整篇抛错（这时库里什么也没写），一样再补。
            raise LLMTransportError("连不上 DeepSeek 接口（连了 3 秒）：ssl eof")
        return _interrupted(article_id, untranslated=1)

    monkeypatch.setattr(cli, "translate_article", translate)
    monkeypatch.setattr(cli.time, "sleep", slept.append)
    assert cli.main(["--article-id", "91", "--article-id", "92", "--fresh"]) == 1
    assert seen == [91, 92, 91, 92, 91, 92]
    assert sum(slept) == sum(cli.NAMED_RETRY_PAUSES)
    captured = capsys.readouterr()
    assert captured.err.count("#91 failed: 连不上 DeepSeek 接口（连了 3 秒）：ssl eof") == 3
    assert "#91 #92 被打断没译完，10 分钟后再补一轮" in captured.out
    assert captured.out.rstrip().endswith(
        "补了 2 轮，#91 #92 还没译完：过一会儿再跑 research-translate --article-id 91 --article-id 92 --fresh"
    )


@pytest.mark.parametrize(
    "outcome",
    [
        TranslateResult(1, "partial", segments=36, reason="request-failed", untranslated=184,
                        error="DeepSeek HTTP 402: Insufficient Balance"),
        TranslateResult(1, "partial", segments=3, reason="http-400", untranslated=1),
        TranslateResult(1, "skipped", reason="no-body"),
        "alignment",
    ],
    ids=["no-balance", "refused-segment", "skipped", "alignment"],
)
def test_named_articles_that_waiting_would_not_fix_are_not_retried(conn, monkeypatch, outcome):
    """欠费、内容被拒、没正文、对不齐：隔多久再跑都一样，不等也不再花钱。"""
    from dataclasses import replace

    from researchlib.translate.provenance import AlignmentError

    seen: list[int] = []

    def translate(conn, article_id, *, data_dir, reuse=True):
        seen.append(article_id)
        if outcome == "alignment":
            raise AlignmentError("slot-0-links")
        return replace(outcome, article_id=article_id)

    monkeypatch.setattr(cli, "translate_article", translate)
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: pytest.fail("不该等着再补"))
    assert cli.main(["--article-id", "5"]) == (1 if outcome == "alignment" else 0)
    assert seen == [5]


def test_an_article_edited_while_it_was_being_translated_is_retried(conn, monkeypatch, capsys):
    """译的时候夜间补钟点改了原文（22173 那次）：等一会儿按新原文再译，不用再点名跑一次。"""
    from researchlib.translate.provenance import AlignmentError

    seen: list[int] = []

    def translate(conn, article_id, *, data_dir, reuse=True):
        seen.append(article_id)
        if len(seen) == 1:
            raise AlignmentError("source-changed-during-request")
        return TranslateResult(article_id, "translated", segments=183)

    monkeypatch.setattr(cli, "translate_article", translate)
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: None)
    assert cli.main(["--article-id", "22"]) == 0
    assert seen == [22, 22]
    assert "#22 failed: translation alignment: source-changed-during-request" in capsys.readouterr().err


def test_queue_runs_leave_cut_off_articles_to_the_cooldown(conn, monkeypatch):
    """按队列译的不等着再补：没译完的记了冷却，过了冷却自己回到队列。"""
    seen: list[int] = []

    def translate(conn, article_id, *, data_dir, reuse=True):
        seen.append(article_id)
        return _interrupted(article_id)

    monkeypatch.setattr(cli, "list_pending_english", lambda conn, **kwargs: [95])
    monkeypatch.setattr(cli, "translate_article", translate)
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: pytest.fail("按队列译的不该等着再补"))
    assert cli.main(["--limit", "1"]) == 0
    assert seen == [95]


def test_a_release_waiting_during_the_pause_stops_the_retry(conn, monkeypatch, tmp_path, capsys):
    """等着再补的时候自动上线来请它停：不补了，印出上线后要再跑的命令，别把上线挡十几分钟。"""
    seen: list[int] = []
    slept: list[float] = []

    def sleep(seconds):
        slept.append(seconds)
        if len(slept) == 2:
            _deploy_waits(tmp_path)

    def translate(conn, article_id, *, data_dir, reuse=True):
        seen.append(article_id)
        return _interrupted(article_id)

    monkeypatch.setattr(cli, "translate_article", translate)
    monkeypatch.setattr(cli.time, "sleep", sleep)
    assert cli.main(["--article-id", "96"]) == 1
    assert seen == [96] and len(slept) == 2
    assert (
        "自动上线在等 b66bfe8：不等着补了，/health 报出新提交后再跑 research-translate --article-id 96"
        in capsys.readouterr().out
    )


def test_retries_and_splits_are_printed_with_the_time(conn, monkeypatch, capsys):
    """以前这些只写 INFO 日志、命令行看不到：断了几次、每次连了多久、隔多久重发都不知道。

    几篇同时译时每行前面带上是哪一篇；本来就以文章号开头的行不重复加。
    """
    import logging

    def translate(conn, article_id, *, data_dir, reuse=True):
        log = logging.getLogger("researchlib.translate")
        log.info("译制请求连了 %.0f 秒断开（ssl eof），2 秒后第 2 次发同一请求", 312)
        log.info("#%s 第 %s/%s 批请求失败（%s），已译的段先存下", article_id, 3, 5, "ssl eof")
        return TranslateResult(article_id, "translated", segments=2)

    monkeypatch.setattr(cli, "translate_article", translate)
    assert cli.main(["--article-id", "97"]) == 0
    err = capsys.readouterr().err
    assert re.search(r"^\d\d:\d\d:\d\d #97 译制请求连了 312 秒断开（ssl eof），2 秒后第 2 次发同一请求$", err, re.M)
    assert re.search(r"^\d\d:\d\d:\d\d #97 第 3/5 批请求失败（ssl eof），已译的段先存下$", err, re.M)
    # 命令跑完就撤掉，不留在进程里。
    assert not logging.getLogger("researchlib.translate").handlers


@pytest.fixture
def pool(conn, monkeypatch):
    """几个 worker 各开一条连接；held 是它们和「别的进程」共用的库里的锁。"""
    opened: list[FakeConn] = [conn]

    def open_more(settings):
        extra = FakeConn(conn.held)
        opened.append(extra)
        return extra

    monkeypatch.setattr(cli, "connect", open_more)
    return opened


def test_workers_translate_different_articles_at_the_same_time(pool, monkeypatch):
    """--workers 3：三篇同时在译（前三篇互相等齐才往下走，串着译会超时），每篇只译一次，每个 worker 一条连接。"""
    import threading

    seen: list[int] = []
    together = threading.Barrier(3, timeout=5)
    lock = threading.Lock()

    def translate(conn, article_id, *, data_dir, reuse=True):
        with lock:
            seen.append(article_id)
            first_three = len(seen) <= 3
        if first_three:
            together.wait()
        return TranslateResult(article_id, "translated", segments=2)

    queue = [1, 2, 3, 4, 5, 6, 7]
    monkeypatch.setattr(cli, "list_pending_english", lambda conn, *, limit, source_id=None: queue[:limit])
    monkeypatch.setattr(cli, "translate_article", translate)
    assert cli.main(["--limit", "6", "--workers", "3"]) == 0
    assert sorted(seen) == [1, 2, 3, 4, 5, 6]
    assert len(pool) == 3 and all(item.closed for item in pool)
    # 每篇译完都把锁放了。
    assert pool[0].held == {}


def test_an_article_another_process_is_translating_is_left_to_it(pool, monkeypatch, capsys):
    """另一个 research-translate 正在译 11：按队列译的跳过它译下一篇；点名的等它放锁再接着补。"""
    other = FakeConn(pool[0].held)
    pool[0].held[11] = other
    seen: list[int] = []
    monkeypatch.setattr(cli, "list_pending_english", lambda conn, *, limit, source_id=None: [11, 12])
    monkeypatch.setattr(cli, "translate_article", _translate(seen))
    assert cli.main(["--limit", "2"]) == 0
    assert seen == [12]

    waited: list[str] = []

    def execute(sql, params=None, *, original=pool[0].execute):
        if "pg_advisory_lock(" in sql:
            # 另一个进程译完放锁，这边接着占住。
            waited.append(sql)
            pool[0].held[params[1]] = pool[0]
        return original(sql, params)

    monkeypatch.setattr(pool[0], "execute", execute)
    assert cli.main(["--article-id", "11"]) == 0
    assert seen == [12, 11] and len(waited) == 1
    assert "#11 别处正在译这一篇，等它译完再接着补" in capsys.readouterr().out
    assert pool[0].held == {}


def test_a_login_error_in_a_worker_stops_the_run_and_is_reported(pool, monkeypatch, capsys):
    """worker 里抛出来的（要重新登录这类）不吞掉：不再拿新的一篇，主线程照样报出来、退出码 2。"""
    seen: list[int] = []

    def translate(conn, article_id, *, data_dir, reuse=True):
        seen.append(article_id)
        if article_id == 1:
            raise cli.XaiLoginRequiredError("refresh token 失效")
        return TranslateResult(article_id, "translated", segments=2)

    monkeypatch.setattr(cli, "list_pending_english", lambda conn, *, limit, source_id=None: [1, 2, 3, 4][:limit])
    monkeypatch.setattr(cli, "translate_article", translate)
    assert cli.main(["--limit", "4"]) == 2
    assert seen == [1]
    assert "需要重新登录：refresh token 失效" in capsys.readouterr().err


def test_workers_out_of_range_are_refused(conn, monkeypatch, capsys):
    monkeypatch.setattr(cli, "prepare", lambda settings: pytest.fail("参数不对不连库"))
    assert cli.main(["--workers", "0"]) == 2
    assert cli.main(["--workers", str(cli.MAX_WORKERS + 1)]) == 2
    assert "--workers 要在 1 到" in capsys.readouterr().err


def test_each_article_prints_what_it_cost_and_the_run_prints_a_total(conn, monkeypatch, capsys):
    """每篇印出这一篇的请求次数和 token、用时；调过模型的话最后印一行合计。回答用不了的那次也算钱。"""
    from contextlib import nullcontext

    from researchlib import llm as llm_module
    from researchlib.translate import llm as translate_llm

    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-not-real")
    answers = {
        "good": {"choices": [{"message": {"content": '{"t": ["好"]}'}}], "model": "deepseek-flash",
                 "usage": {"prompt_tokens": 1000, "prompt_cache_hit_tokens": 600, "completion_tokens": 300,
                           "completion_tokens_details": {"reasoning_tokens": 200}}},
        "broken": {"choices": [{"message": {"content": "不是 JSON"}}], "model": "deepseek-flash",
                   "usage": {"prompt_tokens": 10, "completion_tokens": 5}},
    }
    monkeypatch.setattr(translate_llm.urllib.request, "urlopen", lambda req, timeout=None: nullcontext(req))
    monkeypatch.setattr(
        llm_module, "read_chat_completion",
        lambda req, *, stream: answers[json.loads(req.data)["messages"][1]["content"]],
    )

    def translate(conn, article_id, *, data_dir, reuse=True):
        ask = {21: ["good", "good"], 22: ["good", "broken"]}[article_id]
        for name in ask:
            try:
                translate_llm.chat_json(system="s", user=name, purpose="t", subject="b", data_dir=data_dir)
            except ValueError:
                pass
        if article_id == 22:
            raise RuntimeError("对齐失败")
        return TranslateResult(article_id, "translated", segments=2)

    monkeypatch.setattr(cli, "list_pending_english", lambda conn, *, limit, source_id=None: [21, 22])
    monkeypatch.setattr(cli, "translate_article", translate)
    assert cli.main(["--limit", "2"]) == 1
    captured = capsys.readouterr()
    assert re.search(
        r"^#21 translated segments=2 · 请求 2 次，输入 2,000 token（缓存命中 1,200），"
        r"输出 600 token（思考 400），用时 \d+ 秒$", captured.out, re.M,
    )
    assert re.search(
        r"^#22 failed: 对齐失败 · 请求 2 次，输入 1,010 token（缓存命中 600），"
        r"输出 305 token（思考 200），用时 \d+ 秒$",
        captured.err, re.M,
    )
    assert re.search(
        r"^这一轮 \d+ 秒：译完 1 篇，出错 1 篇；请求 4 次，输入 3,010 token（缓存命中 1,800），"
        r"输出 905 token（思考 600）$", captured.out, re.M,
    )


def test_from_inventory_needs_the_list_and_a_known_category(conn, monkeypatch, tmp_path, capsys):
    import json

    monkeypatch.setattr(cli, "prepare", lambda settings: pytest.fail("清单不对不连库"))
    assert cli.main(["--from-inventory", "unmapped"]) == 2
    assert "先跑 research-translate-inventory" in capsys.readouterr().err
    path = tmp_path / "status" / "translate-inventory.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"ids": {"stopped": [5]}, "display": {}, "coarse": []}), encoding="utf-8")
    assert cli.main(["--from-inventory", "stoped"]) == 2
    assert "没有「stoped」这一类" in capsys.readouterr().err
    # 网站不显示的、只有图的不交给译制；分段粗的每次现查，用 --recheck。
    assert cli.main(["--from-inventory", "site-hidden"]) == 2
    assert "网站不显示" in capsys.readouterr().err
    assert cli.main(["--from-inventory", "no-text"]) == 2
    assert "只有图" in capsys.readouterr().err
    assert cli.main(["--from-inventory", "coarse"]) == 2
    assert "--recheck" in capsys.readouterr().err
    assert cli.main(["--from-inventory", "stopped", "--loop"]) == 2
    assert cli.main(["--from-inventory", "stopped", "--article-id", "5"]) == 2


def test_from_inventory_makes_way_for_a_release_like_the_queue(conn, monkeypatch, tmp_path):
    """一类可能有几百篇：和按队列译一样，每译完一篇看一眼自动上线在不在等。"""
    import json

    path = tmp_path / "status" / "translate-inventory.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"ids": {}, "display": {"unmapped": [3, 2, 1]}, "coarse": []}), encoding="utf-8")
    seen: list[int] = []

    def translate(conn, article_id, *, data_dir, reuse=True):
        seen.append(article_id)
        _deploy_waits(tmp_path)
        return TranslateResult(article_id, "translated", segments=2)

    monkeypatch.setattr(cli.inventory, "unfinished", lambda conn, ids, data_dir: list(ids))
    monkeypatch.setattr(cli, "translate_article", translate)
    assert cli.main(["--from-inventory", "unmapped"]) == 0
    assert seen == [3]
