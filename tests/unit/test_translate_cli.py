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


def _translate(seen, *, fail=(), status="translated"):
    def fake(conn, article_id, *, data_dir):
        seen.append(article_id)
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


def test_explicit_ids_keep_request_order_without_duplicate_calls(conn, monkeypatch):
    seen = []
    monkeypatch.setattr(cli, 'translate_article', _translate(seen))
    monkeypatch.setattr(cli, 'list_coarse_translations', lambda *a, **kw: pytest.fail('显式 id 不进入队列'))
    assert cli.main(['--recheck', '--article-id', '72', '--article-id', '71', '--article-id', '72',
                     '--source', 'unrelated', '--limit', '1']) == 0
    assert seen == [72, 71]
