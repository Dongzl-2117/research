"""生成只含合成资料的阅读页，供浏览器验收；不启动应用、不连库、不调模型。"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path
from types import SimpleNamespace

from markupsafe import Markup

from researchlib.mdlinks import finish_html, prepare_body_html
from researchlib.translate.display import article_pairs
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
    replacements = {index: f"<p>这是第{index}段合成译文，用来验收完整内容和排版。</p>"
                    for index, _html in translatable_blocks(original)}
    translated = finish_html(splice_segments(original, replacements), None)
    for name, zh in (("complete", translated), ("misaligned", "<p>这是缺少中段的旧译文。</p>")):
        view = article_pairs(original, zh)
        context = {
            "title": "合成排版验收", "canonical": "http://127.0.0.1/", "public_host": "127.0.0.1",
            "source": SimpleNamespace(id="synthetic", author="合成作者", name="合成来源", timeline=False),
            "sources": [], "article": {"id": 1, "title": "合成排版验收", "duplicate_of": None},
            "date": "2026-01-01", "body_language": "pairs", "has_translation": True,
            "translation_complete": True, "pair_notice": view.notice,
            "pairs": [{"en": Markup(pair.en), "zh": Markup(pair.zh) if pair.zh else None, "anchor": pair.anchor}
                      for pair in view.pairs],
        }
        page = templates().env.get_template("article.html").render(context)
        (output / f"{name}.html").write_text(page, encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    generate(parser.parse_args().output)
