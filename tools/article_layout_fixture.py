"""生成只含合成资料的阅读页，供浏览器验收；不启动应用、不连库、不调模型。"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path
from types import SimpleNamespace

from markupsafe import Markup

from researchlib.mdlinks import finish_html, prepare_body_html
from researchlib.translate.display import article_pairs
from researchlib.translate.pair import stamp_paragraph_ids
from researchlib.translate.provenance import receipt, source_manifest
from researchlib.translate.segments import splice_segments, translatable_blocks
from researchlib.web.routes import templates

ROOT = Path(__file__).resolve().parents[1]


def generate(output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    (output / "static").mkdir(exist_ok=True)
    (output / "media").mkdir(exist_ok=True)
    for name in ("site.css", "article.js"):
        shutil.copyfile(ROOT / "src/researchlib/web/static" / name, output / "static" / name)
    (output / "media/chart.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="480" height="180">'
        '<rect width="480" height="180" fill="#e7eff7"/>'
        '<path d="M40 150 L140 100 L240 120 L340 50 L440 30" fill="none" stroke="#2761a0" stroke-width="4"/>'
        '<text x="20" y="24">Synthetic chart</text></svg>', encoding="utf-8",
    )
    parts = ["# Synthetic report", "## Financial results", "# " + "这是误作标题的完整正文，应该按段落阅读。" * 30]
    parts += ["| " + " | ".join(f"Period {index}" for index in range(15)) + " |",
              "| " + " | ".join("---" for _ in range(15)) + " |",
              "| " + " | ".join(f"{index + 1},234,567.89" for index in range(15)) + " |"]
    body = "\n\n".join(parts[:3]) + "\n\n" + "\n".join(parts[3:])
    body += '\n\n![Chart](chart.svg)\n\n```python\nreturn 42 < 100\n```\n\n[内嵌表格未取到]\n\n'
    for section in range(4):
        body += f"\n\n## Section {section + 1}\n\n"
        for paragraph in range(14):
            body += (f"Paragraph {section}-{paragraph}: "
                     + "The synthetic analysis considers revenue, costs and distribution across different periods. " * 5
                     + "\n\n")
    original = prepare_body_html(body, "Synthetic report", {"chart.svg": "/media/chart.svg"}, None, None)
    manifest = source_manifest(original)
    normalized = manifest["source_snapshot"]
    analysis = "The synthetic analysis considers revenue, costs and distribution across different periods."
    replacements = {index: fragment.replace("Financial results", "财务结果").replace("Period", "期间")
                    .replace("Section", "章节").replace("Paragraph", "段落")
                    .replace(analysis, "合成分析讨论各期间的收入、成本和分销。")
                    for index, fragment in translatable_blocks(normalized)}
    raw_translation = splice_segments(normalized, replacements)
    proof = receipt(manifest, raw_translation)
    repeat_source = "<p>Synthetic subscriber profit was 10 dollars.</p>" * 2 + "<p>Costs were 20 dollars.</p>"
    repeat_target = "<p>合成订户利润为10美元。</p>" * 2 + "<p>成本为20美元。</p>"
    repeat_proof = receipt(source_manifest(repeat_source), repeat_target)
    coarse_source = "<blockquote><p>" + " ".join(
        f"Topic {i} explains the independent market mechanism and why subscriber retention improves regional revenue."
        for i in range(25)
    ) + "</p></blockquote>"
    coarse_target = "<blockquote><p>" + "".join(
        f"议题{i}解释独立市场机制与区域订户收入。" for i in range(25)
    ) + "</p></blockquote>"
    cases = [
        ("complete", original, raw_translation, proof),
        ("misaligned", original, "<p>这是缺少中段的旧译文。</p>", None),
        ("repeated", repeat_source, repeat_target, repeat_proof),
        ("incompatible", repeat_source.replace("10 dollars", "30 dollars"), repeat_target, repeat_proof),
        ("anchored", coarse_source, coarse_target, None),
    ]
    for name, en, zh, provenance in cases:
        view = article_pairs(stamp_paragraph_ids(en), finish_html(zh, None), provenance=provenance,
                             canonical_source=en, canonical_target=zh)
        context = {
            "title": "合成排版验收", "canonical": "http://127.0.0.1/", "public_host": "127.0.0.1",
            "source": SimpleNamespace(id="synthetic", author="合成作者", name="合成来源", timeline=False),
            "sources": [], "article": {"id": 1, "title": "合成排版验收", "duplicate_of": None},
            "date": "2026-01-01", "body_language": "pairs", "has_translation": True,
            "translation_complete": True, "pair_notice": view.notice,
            "pair_correspondence": view.correspondence,
            "pairs": [{"en": Markup(pair.en), "zh": Markup(pair.zh) if pair.zh else None,
                       "anchor": pair.anchor, "repeat_of": pair.repeat_of}
                      for pair in view.pairs],
        }
        page = templates().env.get_template("article.html").render(context)
        (output / f"{name}.html").write_text(page, encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    generate(parser.parse_args().output)
