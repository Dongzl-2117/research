"""生成问库合成页面与响应；只渲染模板，不连库、不启动主应用、不调用模型。"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from researchlib.web.routes import templates

ROOT = Path(__file__).resolve().parents[1]
QUESTION = "这家公司增长的主要来源是什么，有哪些风险？"
ANSWER = """## 先看结论

增长主要来自**订阅续费和渠道扩展**，但现有材料只覆盖部分地区，需要结合原文判断。[1]

### 两个增长来源

1. 订阅收入随续费率改善而增加。[1]
2. 新渠道带来新增客户，但获客成本也在上升。[2]

| 观察项 | 材料中的变化 | 还需核实 |
| --- | --- | --- |
| 续费率 | 从 80% 提升到 85% | 是否持续 |
| 渠道 | 新增两个区域 | 获客成本 |

> 收入增长并不等于利润同步增长。[2]

```text
利润 = 收入 - 获客成本 - 服务成本
示例编号 [1] 是代码内容
```

下一步可以查看两篇原文的完整语境，尤其留意成本口径。[1][2]
"""


def response() -> dict:
    from researchlib.snippets import answer_html

    return {
        "id": 7, "question": QUESTION, "status": "done", "answer": ANSWER,
        "answer_html": str(answer_html(ANSWER)), "error": "", "checks": [], "terms": ["增长"],
        "model": "合成模型替身", "created": "2026-11-04", "stats": {},
        "citations": [
            {"n": 1, "article_id": 9, "ord": 0, "title": "合成研究：订阅与续费", "author": "合成作者甲",
             "date": "2026-11-01", "heading": "续费", "text": "订阅续费率从80%提升到85%，带动收入增长。",
             "url": "/articles/9?chunk=0", "cited": True},
            {"n": 2, "article_id": 10, "ord": 1, "title": "合成研究：渠道与成本", "author": "合成作者乙",
             "date": "2026-11-02", "heading": "渠道",
             "text": "新增两个区域，获客成本上升。收入增长不等于利润同步增长。",
             "url": "/articles/10?chunk=1", "cited": True},
        ],
    }


def generate(output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    static = output / "static"
    static.mkdir(exist_ok=True)
    for source in (ROOT / "src/researchlib/web/static").iterdir():
        if source.is_file():
            shutil.copyfile(source, static / source.name)
    result = response()
    context = {
        "title": "问库 · 合成验收", "nav": "ask", "canonical": "http://127.0.0.1/ask",
        "sources": [], "question": "", "filters": {"author": "", "from": "", "to": "", "source": ""},
        "authors": ["合成作者甲", "合成作者乙"], "company": None, "article": None, "tree": None,
        "llm_ready": True, "llm_label": "合成模型替身", "history": [
            {"id": 7, "question": QUESTION, "scope": {}, "status": "done"},
            {"id": 8, "question": "利润率变化的原因是什么？", "scope": {}, "status": "error"},
        ],
    }
    cases = {"empty": None, "sample": None, "answer": result,
             "running": {**result, "answer": "", "answer_html": "", "status": "running"},
             "error": {**result, "answer": "", "answer_html": "", "status": "error", "error": "合成整合失败"},
             "raw": {**result, "id": None, "answer": "", "answer_html": "", "status": "",
                     "error": "没有可用模型，只列相关原文。"}}
    for name, data in cases.items():
        page = templates().env.get_template("ask.html").render(
            {**context, "result": data, "sample_banner": name == "sample"},
        )
        (output / f"ask-{name}.html").write_text(page, encoding="utf-8")
    (output / "response.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    generate(parser.parse_args().output)
