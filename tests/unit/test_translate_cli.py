"""research-translate：按队列译、单篇失败不停、--loop 不空转、--recheck、--dry-run。不连库、不调真 LLM。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from researchlib.translate import cli
from researchlib.translate.service import TranslateResult


class FakeConn:
    def __init__(self):
        self.rolled_back = 0
        self.closed = False
        self.statements = []

    def execute(self, sql, params=None):
        self.statements.append(sql)

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
    assert cli.main(["--loop", "--limit", "5"]) == 0
    assert "自动上线在等 b66bfe8" in capsys.readouterr().out


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
