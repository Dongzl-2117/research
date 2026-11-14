"""审核 T4：开头一样、只差结尾一个数的两段英文是两段话，对照页不能收掉一段、把它的中文挂到前一段后面。"""

from __future__ import annotations

from researchlib.translate import provenance as pr
from researchlib.translate.display import article_pairs

INTRO = "<p>Analysts had 15 different estimates before the call, and none of them matched the guidance.</p>"
FIRST = ("<p>Management said that next year capital spending would land in the range of 10 billion "
         "to 12 billion dollars.</p>")
SECOND = ("<p>Management said that next year capital spending would land in the range of 10 billion "
          "to 15 billion dollars.</p>")
TAIL = "<p>The stock fell after hours as investors digested the new spending plan for the data centers.</p>"
ZH = [
    "<p>电话会之前分析师有 15 种不同的估计，没有一种和指引对得上。</p>",
    "<p>管理层说，明年资本开支会在 100 亿到 120 亿美元之间。</p>",
    "<p>管理层说，明年资本开支会在 100 亿到 150 亿美元之间。</p>",
    "<p>投资者消化了新的数据中心开支计划，盘后股价下跌。</p>",
]


def test_paragraphs_that_differ_only_in_a_number_both_stay():
    source = INTRO + FIRST + SECOND + TAIL
    translated = "".join(ZH)
    proof = pr.receipt(pr.source_manifest(source), translated)
    view = article_pairs(source, translated, provenance=proof)
    assert view.correspondence == "mapped" and not view.notice
    assert len(view.pairs) == 4
    assert "12 billion" in view.pairs[1].en and "15 billion" in view.pairs[2].en
    assert "150 亿" in view.pairs[2].zh and "150 亿" not in view.pairs[1].zh


def test_true_repeats_are_still_folded():
    source = INTRO + FIRST + FIRST + TAIL
    translated = ZH[0] + ZH[1] + ZH[1] + ZH[3]
    proof = pr.receipt(pr.source_manifest(source), translated)
    view = article_pairs(source, translated, provenance=proof)
    assert len(view.pairs) == 3
