"""双语写入和展示的已确认反例；全部使用合成正文和模型替身。"""

from pathlib import Path

import pytest

from researchlib.translate import service
from researchlib.translate.display import article_pairs, content_gaps

A = "<p>Acme earned 10 dollars from new subscribers while the platform kept its important distribution advantage.</p>"
B = "<p>Beta lost 20 dollars after a different customer group reduced demand for its infrastructure business.</p>"
C = "<p>Gamma held 30 dollars of cash and now plans to reinvest that money into regional logistics facilities.</p>"
ZA = "<p>甲公司从新订户赚了10美元，仍保持平台分发优势。</p>"
ZA2 = "<p>新增订阅带来10美元利润，甲公司的渠道优势还在。</p>"
ZB = "<p>乙公司因另一类客户需求减少而亏损20美元。</p>"
ZC = "<p>丙公司持有30美元现金，计划投资区域物流设施。</p>"


def test_correct_distinct_amounts_are_never_overwritten():
    source = ("<p>The platform reported strong subscriber momentum across its primary distribution channels "
              "and maintained its regional commercial advantages; quarterly profit was 10 dollars.</p>")
    assert service.reuse_duplicate_sources([source, source.replace("10", "20")], [ZA, ZB]) == [ZA, ZB]


def test_correct_distinct_link_owners_are_never_overwritten():
    source = A.replace("10 dollars", '<a href="https://example.invalid/one">the report</a>')
    other = source.replace("/one", "/two")
    left = '<p>甲的<a href="https://example.invalid/one">报告</a>。</p>'
    right = '<p>乙的<a href="https://example.invalid/two">报告</a>。</p>'
    assert service.reuse_duplicate_sources([source, other], [left, right]) == [left, right]


def test_equal_count_amount_swap_is_rejected():
    assert service.translation_problem([A, B, C], [ZA, ZC, ZB]) is not None


def test_independent_dedup_cannot_repair_missing_middle():
    displayed = article_pairs(A + A + B + C, ZA + ZA2 + ZC)
    assert displayed.notice
    assert all(not (pair.en and pair.zh) for pair in displayed.pairs)
    assert B in "".join(pair.en for pair in displayed.pairs)
    assert ZA2 in "".join(pair.zh or "" for pair in displayed.pairs)


def test_projection_checks_occurrences_and_order():
    assert content_gaps(A + A + B, A + B) > 0
    assert content_gaps(A + B + C, A + C + B) > 0


def test_alternate_translation_wording_is_preserved():
    displayed = article_pairs(A + A, ZA + ZA2)
    visible = "".join(pair.zh or "" for pair in displayed.pairs)
    assert "新增订阅" in visible and "新订户" in visible


def test_near_identical_translation_wording_is_not_deleted():
    shared = "甲公司仍保有平台分发优势，新增订阅收入保持增长，原有用户继续留在平台。" * 5
    left = f"<p>{shared}这一季度赚了10美元，尾句记录利润。</p>"
    right = f"<p>{shared}这一季度赚了10美元，尾句记录现金。</p>"
    displayed = article_pairs(A + A, left + right)
    visible = "".join(pair.zh or "" for pair in displayed.pairs)
    assert "尾句记录利润" in visible and "尾句记录现金" in visible


def test_leaf_extraction_gap_cannot_hide_numeric_table_image_or_code():
    material = ('<table><tr><td>481</td></tr></table>'
                '<img src="/media/synthetic.png"><pre><code>return 42</code></pre>')
    original = "<h2>Report</h2>" + A + material + B
    translated = "<h2>报告</h2>" + ZA + ZB
    view = article_pairs(original, translated)
    assert view.notice
    assert "481" in view.pairs[0].en and "return 42" in view.pairs[0].en
    assert "/media/synthetic.png" in view.pairs[0].en
    assert "<h2>Report</h2>" in view.pairs[0].en


class RecordingConnection:
    def __init__(self):
        self.article = {"id": 1, "source_id": "synthetic", "title": "Synthetic report", "body_md": A,
                        "body_text": A, "published_at": None, "raw_meta": {"language": "en"}}
        self.writes = []
        self.committed = False

    def execute(self, sql, params=None):
        if sql.lstrip().startswith(("INSERT", "UPDATE", "DELETE")):
            self.writes.append((sql, params))
        return self

    def fetchone(self):
        return self.article

    def commit(self):
        self.committed = True


def test_failed_restoration_never_writes_or_overwrites(monkeypatch, tmp_path: Path):
    conn = RecordingConnection()
    original = '<p>The example uses <code>return 42</code> and <img src="/media/synthetic.png"> here.</p>'
    monkeypatch.setattr(service, "_render_original", lambda *args: (original, "x"))
    monkeypatch.setattr(service, "_translate_batch", lambda *args: ["<p>示例译文遗漏了代码和图片。</p>"])
    monkeypatch.setattr(service, "_translate_title", lambda *args: "合成报告")
    monkeypatch.setattr(service, "replace_translation_chunks", lambda *args: None)
    with pytest.raises(RuntimeError, match="translation"):
        service.translate_article(conn, 1, data_dir=tmp_path)
    assert not conn.writes and not conn.committed
