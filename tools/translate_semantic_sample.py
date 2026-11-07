"""隔离核验五段实译和几条反例。不写库，不部署。

没有 DEEPSEEK_API_KEY 时直接退出，这不算实跑。
有 key 时只调独立核验，不跑 research-translate，不连正式库。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from researchlib.llm_provider import deepseek_api_key
from researchlib.translate.harness import verify_facts

NEGATIVES = [
    ("12:15AM->PM", "<p>The call is at 12:15AM.</p>", "<p>电话在12:15PM。</p>", "contradict"),
    ("November->December", "<p>Shipped in November 2014.</p>", "<p>2014年12月发出。</p>", "contradict"),
    ("Q4->Q3", "<p>Demand was fiscal Q4.</p>", "<p>需求是第3财季。</p>", "contradict"),
    ("1.85-billion-scale", "<p>The deal was $1.85 billion.</p>", "<p>交易是1.85亿美元。</p>", "contradict"),
    ("revenue-cost-swap", "<p>Revenue was 10 million and costs were 20 million.</p>",
     "<p>收入2000万，成本1000万。</p>", "contradict"),
    ("points-people-swap", "<p>800 data points on 100 million women.</p>",
     "<p>对800人掌握1亿个数据点。</p>", "contradict"),
]


def _samples():
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "tests" / "unit"))
    from test_translate_harness import _GROUNDED, _LIVE

    return _LIVE, _GROUNDED


def main() -> int:
    live, grounded = _samples()
    if not deepseek_api_key():
        print("未实跑：没有 DEEPSEEK_API_KEY。下面只是将要核验的样本，没有调模型。")
        for article_id, _source, _target in live:
            print(f"正例 #{article_id} 引用 {len(grounded[article_id])} 条")
        for name, _source, _target, _expect in NEGATIVES:
            print(f"反例 {name}")
        return 2
    data_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/tmp/translate-semantic-sample")
    data_dir.mkdir(parents=True, exist_ok=True)
    failed = False
    for article_id, source, target in live:
        found, raw = verify_facts(source, target, data_dir)
        print(json.dumps({
            "id": article_id, "expect": "accept", "status": found.status, "reason": found.reason,
            "claims": raw.get("claims") if isinstance(raw, dict) else None,
        }, ensure_ascii=False))
        failed = failed or found.status != "accept"
    for name, source, target, expect in NEGATIVES:
        found, raw = verify_facts(source, target, data_dir)
        print(json.dumps({
            "id": name, "expect": expect, "status": found.status, "reason": found.reason,
            "claims": raw.get("claims") if isinstance(raw, dict) else None,
        }, ensure_ascii=False))
        failed = failed or found.status != expect
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
