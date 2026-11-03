"""旧红新绿：只有既有入口，能在 #215 基线上独立重现缺陷；全部为合成资料。"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from researchlib.translate import service
from researchlib.translate.display import article_pairs
from researchlib.translate.llm import ChatJsonResult
from researchlib.translate.qa import report_from_html

A = "<p>Acme earned 10 dollars from new subscribers while the platform kept its important distribution advantage.</p>"
B = "<p>Beta lost 20 dollars after a different customer group reduced demand for its infrastructure business.</p>"
C = "<p>Gamma held 30 dollars of cash and now plans to reinvest that money into regional logistics facilities.</p>"
ZA = "<p>甲公司从新订户赚了10美元，仍保持平台分发优势。</p>"
ZA2 = "<p>新增订阅带来10美元利润，甲公司的渠道优势还在。</p>"
ZB = "<p>乙公司因另一类客户需求减少而亏损20美元。</p>"
ZC = "<p>丙公司持有30美元现金，计划投资区域物流设施。</p>"


class Rows:
    def __init__(self, one=None, rows=()):
        self.one = one
        self.rows = list(rows)

    def fetchone(self):
        return self.one

    def fetchall(self):
        return self.rows


class MemoryConn:
    def __init__(self, body):
        self.article = {"id": 990001, "source_id": "stratechery", "title": "Synthetic audit",
                        "body_md": body, "body_text": "English synthetic article.", "published_at": None,
                        "raw_meta": {"format": "markdown"}, "content_hash": "synthetic"}
        self.writes = []
        self.committed = False

    def execute(self, sql, params=None):
        if "INSERT INTO article_translations" in sql:
            self.writes.append(params)
        elif "FROM articles WHERE id" in sql:
            return Rows(self.article)
        elif "FROM sources" in sql:
            return Rows({"kind": "markdown"})
        elif "count(*)" in sql:
            return Rows({"n": 0, "t": ""})
        elif "SELECT original_ref, storage_path" in sql:
            return Rows(rows=[{"original_ref": f"/media/{name}.png", "storage_path": f"{name}.png"}
                              for name in ("a", "b", "synthetic")])
        return Rows()

    def commit(self):
        self.committed = True


def translate(body, outputs, tmp_path, *, mutate=None):
    conn = MemoryConn(body)

    def fake(**kwargs):
        user = kwargs["user"]
        values = ["合成标题"] if user["segments"] == ["Synthetic audit"] else outputs
        if "segment_ids" in user:
            if user["segment_ids"] != ["title"]:
                rendered, _digest = service._render_original(conn, conn.article, tmp_path)
                manifest = service.source_manifest(rendered)
                targets = service.translatable_blocks(manifest["source_snapshot"])
                by_id = {manifest["segments"][index]["id"]: text
                         for (index, _source), text in zip(targets, outputs, strict=True)}
                values = [by_id[identity] for identity in user["segment_ids"]]
            values = [{"id": identity, "source_hash": source_hash, "html": text}
                      for identity, source_hash, text in zip(user["segment_ids"], user["source_hashes"], values,
                                                             strict=True)]
            if mutate and user["segment_ids"] != ["title"]:
                values = mutate(values)
        return ChatJsonResult(data={"t": values}, model="synthetic-model", usage=None, latency_ms=0)

    with patch.object(service, "chat_json", side_effect=fake), patch.object(service, "replace_translation_chunks"):
        service.translate_article(conn, 990001, data_dir=tmp_path)
    return conn


def test_correct_amounts_are_never_overwritten_by_near_duplicate_reuse(tmp_path):
    en = ("<p>The platform reported strong subscriber momentum across its primary distribution channels "
          "and maintained its regional commercial advantages; quarterly profit was 10 dollars.</p>")
    zh = "<p>平台主要分销渠道订户增长强劲，区域商业优势稳定，本季度利润为10美元。</p>"
    conn = translate(en + en.replace("10 dollars", "20 dollars"), [zh, zh.replace("10美元", "20美元")], tmp_path)
    stored = conn.writes[0][3]
    assert "10美元" in stored and "20美元" in stored


def test_same_visible_source_keeps_its_own_restored_link(tmp_path):
    sources = [A.replace("advantage.", f'<a href="https://example.invalid/{name}">advantage</a>.')
               for name in ("one", "two")]
    output = ZA.replace("优势", '<a id="L0">优势</a>')
    conn = translate("".join(sources), [output, output], tmp_path)
    assert "example.invalid/one" in conn.writes[0][3] and "example.invalid/two" in conn.writes[0][3]


def test_failed_restoration_is_rejected_before_any_translation_write(tmp_path):
    source = '<p>See the chart. <code>rate = 42</code><img src="/media/synthetic.png"></p>'
    with pytest.raises(RuntimeError, match="unshield"):
        translate(source, ["<p>见图表。</p>"], tmp_path)


def test_independent_dedupe_cannot_invent_a_missing_middle_mapping():
    view = article_pairs(A + A + B + C, ZA + ZA2 + ZC)
    assert view.notice and all(not (p.en and p.zh) for p in view.pairs)


def test_qa_does_not_pass_a_four_to_three_count_repair():
    assert report_from_html(1, "synthetic", "合成", A + A + B + C, ZA + ZA2 + ZC).status == "fail"


def test_alternate_chinese_wording_with_unique_information_is_retained():
    en = A + A
    zh = ("<p>这是一段共同的中文说明，介绍平台的重要渠道优势。甲独有的信息。</p>"
          "<p>这是一段共同的中文说明，介绍平台的重要渠道优势。乙独有的信息。</p>")
    view = article_pairs(en, zh)
    assert "甲独有的信息" in "".join(p.zh or "" for p in view.pairs)
    assert "乙独有的信息" in "".join(p.zh or "" for p in view.pairs)


def test_inline_code_loss_is_visible_to_qa():
    report = report_from_html(1, "synthetic", "合成", "<p>See <code>rate = 42</code>.</p>", "<p>见说明。</p>")
    assert report.status == "fail" and any(i.code == "protected-content-loss" for i in report.issues)


def test_legacy_equal_counts_are_not_semantic_acceptance():
    report = report_from_html(1, "synthetic", "合成", A, ZA)
    assert report.status == "warn"
    assert report.as_dict()["correspondence"]["status"] == "legacy"
