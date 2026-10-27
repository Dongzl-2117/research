"""近期审查的最小合成回归样本，不访问数据库、模型或生产服务。"""

from types import SimpleNamespace

import pytest

from researchlib import embeddings
from researchlib.author_voice import assign_poster_role, display_author, for_model
from researchlib.ingest.parse import _comments
from researchlib.migrate import require_current
from researchlib.textutil import markdown_to_text
from researchlib.web.quoting import mark_quoted


@pytest.mark.parametrize("name", ["方伟Rey", "完全改过的显示名", ""])
def test_account_identity_wins_over_nickname(name):
    meta = assign_poster_role({"poster_name": name, "poster_id": "1001"}, "方伟", ("方伟Rey",), owner_ids=("1001",))
    assert meta["poster_role"] == "owner"
    impostor = assign_poster_role(
        {"poster_name": "方伟Rey", "poster_id": "2002"}, "方伟", ("方伟Rey",), owner_ids=("1001",)
    )
    assert impostor["poster_role"] == "guest"


@pytest.mark.parametrize(
    "meta,ids",
    [({}, ("1001",)), ({"poster_name": "方伟Rey"}, ("1001",)), ({"poster_name": "示例星主", "poster_id": "1001"}, ())],
)
def test_unknown_accounts_are_explicit_without_inventing_identity(meta, ids):
    result = assign_poster_role(meta, "方伟", owner_ids=ids)
    assert result["poster_role"] == "unknown"
    assert display_author("方伟", result) == ("星球存档（账户未核实）", False)
    assert for_model("原文仍然保留", [("", "原文仍然保留")], result) == ("", [], True)


def test_complete_comments_and_nested_replies_win_even_when_preview_exists():
    node = {
        "show_comments": [{"comment_id": 1, "text": "预览"}],
        "comments": [
            {"comment_id": 1, "text": "完整正文", "replied_comments": [{"comment_id": 2, "text": "完整回复"}]},
            {"comment_id": 3, "text": "预览之外"},
        ],
    }
    assert [c["text"] for c in _comments(node)] == ["完整正文", "完整回复", "预览之外"]
    assert _comments({"comments": [], "show_comments": [{"text": "过期预览"}]}) == []
    assert _comments({"show_comments": [{"text": "只有预览"}]}) == [{"text": "只有预览"}]


@pytest.mark.parametrize(
    "text", ["成本=3*4，回报=本金*年数*收益率", "填空 ___，变量 foo_bar", "`foo_bar * x < y`", "``a`b * c_d``"]
)
def test_literal_formula_blank_and_code_survive(text):
    expected = text.strip("`")
    assert markdown_to_text(text) == expected


def test_emphasis_and_claude_alert_fix_still_work():
    assert markdown_to_text("**粗体**、_强调_、~~删除线~~") == "粗体、强调、删除线"
    assert markdown_to_text("> [!NOTE]\n> 保留正文") == "保留正文"


SOURCE = "这是合成的原文段落，说明客户续费和稳定的供应链都可以帮助企业改善现金流。"


@pytest.mark.parametrize(
    "quote", [SOURCE + "虚构的后半句。", SOURCE[:28] + "虚构结尾。", SOURCE + "编造" + "后面另一段有独立的原文内容。"]
)
def test_fabricated_tail_cannot_highlight_source(quote):
    html = "<p>" + SOURCE + "</p><p>后面另一段有独立的原文内容。</p>"
    assert mark_quoted(html, quote) == (html, False)


def test_real_quote_can_start_mid_paragraph_and_cover_multiple_blocks():
    html = "<p>前言。" + SOURCE + "</p><p>第二段也有完整的原文内容。</p>"
    marked, found = mark_quoted(html, SOURCE + "第二段也有完整的原文内容。")
    assert found and marked.count("is-quoted") == 2


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0]


def test_rejected_embedding_batch_cannot_starve_later_rows(monkeypatch):
    badrow = {"article_id": 1, "ord": 0, "title": "标题", "heading": "", "body": "改过的正文", "hash": "old"}
    goodrow = {**badrow, "article_id": 2, "hash": "current"}
    written = []

    class Conn:
        info = SimpleNamespace(dbname="isolated")

        def execute(self, sql, args=None):
            if "DELETE FROM" in sql:
                return _Rows([])
            return _Rows([goodrow])

        def commit(self):
            pass

        def cursor(self):
            return self

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def executemany(self, _sql, rows):
            written.extend(rows)

    def pending(_conn, _size, _model, skip):
        if (1, 0) not in skip:
            return [badrow]
        return [] if written else [goodrow]

    monkeypatch.setattr(embeddings, "column_type", lambda _: "halfvec")
    monkeypatch.setattr(embeddings, "_pending", pending)
    monkeypatch.setattr(embeddings, "embed_texts", lambda texts: [[1.0] for _ in texts])
    skipped = []
    assert embeddings.backfill(Conn(), batch=1, skipped=skipped) == 1
    assert skipped == [(1, 0)] and written[0][0] == 2


def test_web_version_check_never_applies_missing_or_newer_migrations():
    seen = []

    class Conn:
        def execute(self, sql):
            seen.append(sql)
            if "to_regclass" in sql:
                return _Rows([{"name": "schema_migrations"}])
            return _Rows([{"version": "unknown_future_migration.sql"}])

    with pytest.raises(RuntimeError, match="禁止自动迁移或降级"):
        require_current(Conn())
    assert all(sql.startswith("SELECT") for sql in seen)


def test_translation_loop_closes_connection_before_idle_sleep(monkeypatch):
    from researchlib.translate import cli
    class StopSleep(Exception):
        pass
    closed = []
    conn = SimpleNamespace(close=lambda:closed.append(True))
    monkeypatch.setattr(cli.Settings, 'from_env', lambda:SimpleNamespace(data_dir=None))
    monkeypatch.setattr(cli, 'llm_provider', lambda:'deepseek')
    monkeypatch.setattr(cli, 'deepseek_api_key', lambda:'synthetic-test-key')
    monkeypatch.setattr(cli, 'prepare', lambda *a,**k:conn)
    monkeypatch.setattr(cli, 'list_pending_english', lambda *a,**k:[])
    def sleep(_seconds):
        assert closed == [True]
        raise StopSleep
    monkeypatch.setattr(cli.time,'sleep',sleep)
    with pytest.raises(StopSleep):
        cli.main(['--loop'])
