"""Stratechery 双语问题的离线调查探针；断言复现现状，不是产品验收通过。

全部正文、金额、图片、链接均为编造。数据库与模型是内存替身；不启动应用。
仅对历史基线 8a857d8 运行；修复分支的验收用 tests/unit/test_bilingual_regressions.py。
运行：PYTHONPATH=src python tools/stratechery_bilingual_probe.py --out /tmp/probe.json
"""

from __future__ import annotations

import argparse
import json
import subprocess
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from researchlib.nested_copies import fold_nested_copies
from researchlib.render import render_body
from researchlib.translate import qa, service
from researchlib.translate.display import article_pairs, projection_gaps
from researchlib.translate.pair import visible_text
from researchlib.translate.segments import iter_segments, leaf_count_problem, shield, unshield

ROOT = Path(__file__).resolve().parents[1]
BASELINE = "8a857d801040600216fd851b2df6b07bc068fa92"
A = "<p>Acme earned 10 dollars from new subscribers while the platform kept its important distribution advantage.</p>"
B = "<p>Beta lost 20 dollars after a different customer group reduced demand for its infrastructure business.</p>"
C = "<p>Gamma held 30 dollars of cash and now plans to reinvest that money into regional logistics facilities.</p>"
ZA = "<p>甲公司从新订户赚了10美元，仍保持平台分发优势。</p>"
ZA2 = "<p>新增订阅带来10美元利润，甲公司的渠道优势还在。</p>"
ZB = "<p>乙公司因另一类客户需求减少而亏损20美元。</p>"
ZC = "<p>丙公司持有30美元现金，计划投资区域物流设施。</p>"


class Rows:
    def __init__(self, one=None):
        self.one = one

    def fetchone(self):
        return self.one

    def fetchall(self):
        return []


class MemoryConn:
    """SQL 只保存在内存；绝不连接数据库。"""

    def __init__(self, body: str, *, fmt: str = "markdown"):
        self.article = {
            "id": 990001,
            "source_id": "stratechery",
            "title": "Synthetic audit",
            "published_at": None,
            "body_md": body,
            "body_text": visible_text(body),
            "raw_meta": {"format": fmt},
            "content_hash": "synthetic",
        }
        self.stored = None
        self.committed = False

    def execute(self, sql, params=None):
        if "INSERT INTO article_translations" in sql:
            self.stored = params
        elif "FROM articles WHERE id" in sql:
            return Rows(self.article)
        elif "FROM sources" in sql:
            return Rows({"kind": "markdown"})
        elif "count(*)" in sql:
            return Rows({"n": 0, "t": ""})
        return Rows()

    def commit(self):
        self.committed = True


def translated_by_stub(body: str, outputs: list[str]) -> dict:
    """走真实 translate_article / unshield / splice；只替换模型和检索写入。"""
    conn = MemoryConn(body)
    calls = []

    def fake_once(parts, user, data_dir):
        calls.append({"parts": parts, "user_keys": sorted(user)})
        return ["合成标题"] if parts == ["Synthetic audit"] else outputs

    with (
        patch.object(service, "_translate_once", side_effect=fake_once),
        patch.object(service, "replace_translation_chunks"),
    ):
        result = service.translate_article(conn, 990001, data_dir=Path("/tmp/stratechery-offline-probe"))
    assert conn.stored is not None
    original, _digest = service._render_original(conn, conn.article, Path("/tmp/stratechery-offline-probe"))
    return {
        "result": asdict(result),
        "complete": conn.stored[5],
        "committed_to_memory_only": conn.committed,
        "original_html": original,
        "stored_html": conn.stored[3],
        "body_request_has_segment_ids": "segment_ids" in calls[0]["user_keys"],
        "view": view(original, conn.stored[3]),
    }


def view(en: str, zh: str) -> dict:
    displayed = article_pairs(en, zh)
    report = qa.report_from_html(990001, "stratechery", "Synthetic audit", en, zh)
    return {
        "raw_leaves": [len(iter_segments(en)), len(iter_segments(zh))],
        "notice": displayed.notice,
        "page_pairs": [[visible_text(p.en), visible_text(p.zh or "")] for p in displayed.pairs],
        "projection_gaps": list(projection_gaps(en, zh, displayed.pairs)),
        "qa": report.as_dict(),
    }


def run() -> dict:
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    assert head == BASELINE, f"调查基线变了：{head}"
    cases = {}

    reordered = translated_by_stub(A + B + C, [ZA, ZC, ZB])
    assert reordered["complete"] and reordered["result"]["status"] == "translated"
    assert reordered["view"]["qa"]["status"] == "pass"
    assert "丙公司" in reordered["view"]["page_pairs"][1][1]
    assert leaf_count_problem(A + B + C, ZA + ZC + ZB) is None
    cases["equal_count_reordered_is_stored_complete"] = reordered

    fake_equal = view(A + A + B + C, ZA + ZA2 + ZC)
    assert fake_equal["raw_leaves"] == [4, 3]
    assert fake_equal["qa"]["displayLeaves"] == [3, 3]
    assert fake_equal["qa"]["status"] == "pass" and not fake_equal["notice"]
    assert fake_equal["page_pairs"][1] == [visible_text(B), visible_text(ZA2)]
    cases["dedupe_hides_a_missing_middle"] = fake_equal

    amount_en = (
        "<p>The platform reported strong subscriber momentum across its primary distribution channels "
        "and maintained its regional commercial advantages; quarterly profit was 10 dollars.</p>"
    )
    amount_zh = "<p>平台主要分销渠道订户增长强劲，区域商业优势稳定，本季度利润为10美元。</p>"
    sources = [amount_en, amount_en.replace("10 dollars", "20 dollars")]
    correct = [amount_zh, amount_zh.replace("10美元", "20美元")]
    amounts = translated_by_stub("".join(sources), correct)
    assert amounts["complete"] and "20美元" not in amounts["stored_html"]
    assert amounts["stored_html"].count("10美元") == 2
    assert amounts["view"]["qa"]["status"] == "pass"
    amounts["correct_stub_outputs"] = correct
    cases["near_duplicate_reuse_overwrites_a_correct_amount"] = amounts

    linked_en = [
        A.replace("advantage.", f'<a href="https://example.invalid/{item}">advantage</a>.')
        for item in ("one", "two")
    ]
    model_linked = ZA.replace("优势", '<a id="L0">优势</a>')
    expected = [unshield(model_linked, shield(src)) for src in linked_en]
    assert expected[0] and "example.invalid/one" in expected[0]
    assert expected[1] and "example.invalid/two" in expected[1]
    links = translated_by_stub("".join(linked_en), [model_linked, model_linked])
    assert links["complete"] and "example.invalid/two" not in links["stored_html"]
    links["correct_after_unshield_before_reuse"] = expected
    cases["duplicate_reuse_copies_the_other_source_link"] = links

    inline = (
        "<p>See the chart of subscriber demand and the durable advantages in platform distribution. "
        '<code>rate = 42</code><img src="/media/synthetic-chart.png" alt="Synthetic chart"></p>'
    )
    missing = "<p>请看订户需求与平台分发优势的图表。</p>"
    assert unshield(missing, shield(inline)) is None
    lost = translated_by_stub(inline, [missing])
    assert lost["complete"] and "<code>" not in lost["stored_html"] and "<img" not in lost["stored_html"]
    assert lost["view"]["qa"]["status"] == "pass"
    lost["unshield_returned_none"] = True
    cases["failed_unshield_is_stored_complete"] = lost

    raw = (
        "# Synthetic audit\n- source: member-rss\n\n"
        "The platform retains its independent distribution business and subscriber network.\n\n"
        "The next paragraph considers regional infrastructure investment and logistics."
    )
    conn = MemoryConn(raw, fmt="markdown")
    with patch.object(service, "clean_article", return_value=raw):
        old, old_hash = service._render_original(conn, conn.article, Path("/tmp"))
    new, new_hash = service._render_original(conn, conn.article, Path("/tmp"))
    assert old != new and old_hash == new_hash
    assert [len(iter_segments(old)), len(iter_segments(new))] == [3, 2]
    cases["cleaner_drift_does_not_change_translation_hash"] = {
        "method": "替身模拟旧清洗器保留归档头；当前清洗器正常执行",
        "raw_md": raw,
        "old_html": old,
        "current_html": new,
        "old_hash": old_hash,
        "current_hash": new_hash,
        "leaves": [3, 2],
    }

    en = "<blockquote><p>" + " ".join(
        f"Topic {i} explains the independent market mechanism "
        "and why customer retention improves revenue across regions."
        for i in range(25)
    ) + "</p></blockquote>"
    zh = "<blockquote><p>" + "".join(
        f"议题{i}说明独立市场机制以及客户留存如何提高各地区收入。" for i in range(25)
    ) + "</p></blockquote>"
    coarse = view(en, zh)
    assert coarse["raw_leaves"] == [25, 1] and len(coarse["page_pairs"]) == 1
    assert not coarse["notice"] and coarse["qa"]["maxEnChars"] == 2764
    assert "giant-pair" in [i["code"] for i in coarse["qa"]["issues"]]
    cases["legacy_coarse_mode_is_still_one_giant_pair"] = coarse

    split_en = "".join(
        f"<p>Topic {i} explains the independent market mechanism "
        "and why customer retention improves revenue across regions.</p>" for i in range(25)
    )
    split_zh = "".join(
        f"<p>议题{i}说明独立市场机制以及客户留存如何提高各地区收入。</p>" for i in range(25) if i != 12
    )
    fallback = view(split_en, split_zh)
    actual_max = max(len(p[0]) for p in fallback["page_pairs"])
    assert fallback["notice"] and actual_max > qa.GIANT_EN_CHARS
    assert fallback["qa"]["maxEnChars"] < qa.GIANT_EN_CHARS and not fallback["qa"]["giant"]
    fallback["actual_page_max_en_chars"] = actual_max
    cases["pr212_full_document_fallback_differs_from_qa_pair_metrics"] = fallback

    export_samples = {
        "quote": "> A note about demand and subscriber retention.\n\nA note about demand and subscriber retention.",
        "list": "- Search: Customer demand improves retention.\n\n**Search:** Customer demand improves retention.",
        "footnote": (
            "- A note about demand and subscriber retention ↩\n\n"
            "A note about demand and subscriber retention [↩](#synthetic-note)"
        ),
    }
    for kind, md in export_samples.items():
        before = str(render_body(md, {}))
        after = str(render_body(fold_nested_copies(md), {}))
        assert [len(iter_segments(before)), len(iter_segments(after))] == [2, 1]
        cases[f"export_{kind}_has_distinct_original_and_translation_inputs"] = {
            "raw_md": md, "folded_md": fold_nested_copies(md), "raw_leaves": 2, "folded_leaves": 1,
        }

    controls = {}
    separated = view(A + B + C, ZA + ZC)
    assert separated["notice"] and len(separated["page_pairs"]) == 2
    assert not separated["page_pairs"][0][1] and not separated["page_pairs"][1][0]
    controls["missing_middle_without_dedupe_is_safely_separated"] = separated

    zh_links = "".join(
        ZA.replace("优势", f'<a href="https://example.invalid/{item}">优势</a>') for item in ("one", "two")
    )
    kept = article_pairs(A + A, zh_links)
    visible = "".join(p.zh or "" for p in kept.pairs)
    assert "example.invalid/one" in visible and "example.invalid/two" in visible
    controls["pr212_keeps_different_links_in_stored_chinese"] = view(A + A, zh_links)

    en = (
        '<h2>Chart</h2><p><img src="/media/synthetic-chart.png" alt="Synthetic chart"></p>'
        '<pre><code>rate = 42</code></pre><table><tr><th>Revenue</th><td>1234</td></tr></table>'
        '<ol start="4"><li>First point</li></ol>'
    )
    zh = en.replace("Chart</h2>", "图表</h2>").replace("Revenue", "收入").replace("First point", "第一点")
    preserved = article_pairs(en, zh)
    assert projection_gaps(en, zh, preserved.pairs) == (0, 0)
    shown = "".join(p.en + (p.zh or "") for p in preserved.pairs)
    assert '<ol start="4">' in shown and "<pre><code>rate = 42" in shown and "1234" in shown
    assert shown.count('<img src="/media/synthetic-chart.png"') == 1
    controls["numeric_table_image_code_heading_and_list_shell_survive"] = view(en, zh)

    # 只在进程内取消写入前复用，证明两项局部修复效果；不修改产品源码。
    with patch.object(service, "reuse_duplicate_sources", side_effect=lambda _sources, values: list(values)):
        fixed_amounts = translated_by_stub("".join(sources), correct)
        fixed_links = translated_by_stub("".join(linked_en), [model_linked, model_linked])
    assert "10美元" in fixed_amounts["stored_html"] and "20美元" in fixed_amounts["stored_html"]
    assert "example.invalid/one" in fixed_links["stored_html"] and "example.invalid/two" in fixed_links["stored_html"]
    exploratory = {
        "method": "仅在进程内以恒等函数替代 reuse_duplicate_sources；无产品源码修改",
        "amounts_retained": True,
        "distinct_source_links_retained": True,
        "scope": "只证明去掉写入前复用可修复这两例；不修复等长错序或旧数据问题",
    }

    return {
        "baseline": head,
        "scope": "合成内容、内存 SQL 与模型替身；无生产、数据库、付费调用或发布",
        "note": "断言成功说明重现当前问题，不表示这些问题已修复；无产品代码改动。",
        "case_count": len(cases),
        "control_count": len(controls),
        "cases": cases,
        "controls": controls,
        "exploratory": exploratory,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    # 意外走到真实网络、数据库或模型入口时立即失败。
    with (
        patch("socket.socket.connect", side_effect=AssertionError("禁止网络")),
        patch("psycopg.connect", side_effect=AssertionError("禁止真实数据库")),
        patch.object(service, "chat_json", side_effect=AssertionError("禁止真实模型")),
    ):
        payload = run()
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in payload.items() if k not in {"cases", "controls"}}, ensure_ascii=False))


if __name__ == "__main__":
    main()
