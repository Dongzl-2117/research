"""只读范围、旧回执冲突和停用来源的运维边界；不调模型。"""

from types import SimpleNamespace

import pytest

from researchlib.translate import audit, cli
from researchlib.translate.provenance import digest, receipt, source_manifest

SOURCE = '<p>Revenue was 10 dollars.</p><p>Cash was 20 dollars.</p>'
TARGET = '<p>收入为10美元。</p><p>现金为20美元。</p>'


@pytest.mark.parametrize("current", [
    TARGET.replace("收入", "订阅收入"),
    TARGET + '<p>译者补充：需要继续核对。</p>',
    TARGET.replace("现金", '<a href="https://example.invalid/review">现金</a>'),
    TARGET.split('</p>')[0] + '</p>',
])
def test_old_receipt_cannot_authorize_overwriting_current_target(current):
    proof = receipt(source_manifest(SOURCE), TARGET)
    plan = audit.classify(SOURCE, current, proof)
    assert plan["category"] == "target-conflict"
    assert plan["action"] == "review-target-edits"
    assert plan["candidateHash"] == digest(TARGET)
    assert plan["translationHash"] == digest(current)
    assert plan["writes"] is False and plan["modelCalls"] == 0


def test_unchanged_target_is_still_mapped():
    proof = receipt(source_manifest(SOURCE), TARGET)
    assert audit.classify(SOURCE, TARGET, proof)["category"] == "mapped"


def test_explicit_recheck_dry_run_only_reads_requested_ids(monkeypatch, tmp_path, capsys):
    class Conn:
        def __init__(self):
            self.statements = []
            self.closed = False
            self.rolled_back = False

        def execute(self, sql, params=None):
            self.statements.append((sql, params))
            if sql != "SET TRANSACTION READ ONLY":
                assert "a.id = ANY(%s)" in sql
                assert params == ([72, 71, 73],)
            return self

        def fetchall(self):
            return [{"id": 71, "source": "synthetic", "enabled": False},
                    {"id": 72, "source": "synthetic", "enabled": True}]

        def rollback(self):
            self.rolled_back = True

        def close(self):
            self.closed = True

    conn = Conn()
    monkeypatch.setattr(cli.Settings, "from_env", staticmethod(lambda: SimpleNamespace(data_dir=tmp_path)))
    monkeypatch.setattr(cli, "connect", lambda _settings: conn)
    for name in ("prepare", "list_coarse_translations", "list_pending_english", "translate_article",
                 "llm_provider", "deepseek_api_key"):
        monkeypatch.setattr(cli, name, lambda *a, **kw: pytest.fail("定向干跑不应进入队列、迁移或模型"))
    assert cli.main(["--recheck", "--dry-run", "--article-id", "72", "--article-id", "71",
                     "--article-id", "73", "--article-id", "72", "--limit", "1",
                     "--source", "unrelated"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[:3] == ["#72 synthetic explicit-article", "#71 synthetic source-disabled", "#73 - not-found"]
    assert "可处理 1 篇" in lines[-1]
    assert conn.closed and conn.rolled_back
    assert len(conn.statements) == 2
