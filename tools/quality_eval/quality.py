"""检索结果的信息质量盲评：从线上接口拉结果，把没打过分的段落打乱、去掉作者标题名次出盲评单，记分，算指标。

只读线上公开接口（不用登录、不调模型、不花钱），结果和盲评单写进 --dir（默认 data/quality-eval，不进仓库：
里面是库里的原文）。分数记在本目录的 labels.json，键包含问题、文章、块号、范围和正文指纹，不带原文。

    python tools/quality_eval/quality.py fetch base            # 线上 24 个问题的结果 → data/quality-eval/base.json
    python tools/quality_eval/quality.py pool base             # 没打过分的段落 → base_blind.txt（打乱，编号）
    python tools/quality_eval/quality.py label base 'a1b2c3:2 d4e5f6:0'   # 把盲评的分并进 labels.json
    python tools/quality_eval/quality.py metrics base          # 调参题、留出题各自的指标

打分只看信息质量（Zhilin 2026-11-09 定的唯一标准）：2 有料（原创判断、机制、数据、具体例子），1 一般（定义、常识
复述、泛泛的 AI 汇编、和问题只沾一点边），0 没料（一行字、只有标题、和问题无关、目录和链接）。打分时不看是哪一版
排出来的。调参题（queries.json 的 tune）用来调规则，留出题（hold）只看不调。见 docs/retrieval.md「段落本身的信息量」。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
LABELS = HERE / "labels.json"
# 第二套分：片段是不是专门讲这个问题（2 专门讲，1 讲到，0 只是提到）。和信息质量分开记，同一个键。
FOCUS = HERE / "focus.json"
QUERIES = json.loads((HERE / "queries.json").read_text())
TOP = 10


def _queries() -> list[str]:
    queries = [query for group in QUERIES.values() for query in group]
    if len(set(queries)) != len(queries):
        raise ValueError("调参和留出集合必须按整题隔离")
    return queries


def _get(url: str):
    for attempt in range(3):
        try:
            # Cloudflare 拦 Python 默认的 User-Agent（403），换一个说明用途的名字。
            request = urllib.request.Request(url, headers={"User-Agent": "research-quality-eval/1"})
            with urllib.request.urlopen(request, timeout=120) as response:
                return json.loads(response.read())
        except (OSError, ValueError):
            if attempt == 2:
                raise
            time.sleep(5)
    return None


def fetch(base: str, out: Path) -> None:
    """文章级前 50（检索页卡片的摘录段）、段落级前 50（问库、外部调用方拿到的）和片段级前 50（检索页默认，
    level=passage；保留范围和待评文本）。线上还没有片段级的版本，这一项留空。"""
    result = {}
    for query in _queries():
        encoded = urllib.parse.quote(query)
        articles = _get(f"{base}/api/v1/search?limit=50&q={encoded}") or {}
        chunks = _get(f"{base}/api/v1/search?level=chunk&limit=50&q={encoded}") or {}
        fragments = _get(f"{base}/api/v1/search?level=passage&limit=50&q={encoded}") or {}
        result[query] = {
            "articles": [
                {"id": item["id"], "chunk": item.get("chunk"), "title": item.get("title"),
                 "snippet": item.get("snippet")}
                for item in articles.get("items", [])
            ],
            "chunks": [
                {"article_id": p["article_id"], "chunk": p["chunk"], "title": p.get("title"), "text": p.get("text"),
                 "score": p.get("score")}
                for p in chunks.get("passages", [])
            ],
            "fragments": [
                {"article_id": p["article_id"], "chunk": p["chunk"], "from": p.get("from"), "to": p.get("to"),
                 "title": p.get("title"), "text": p.get("text")}
                for p in fragments.get("passages", [])
                if p.get("from") is not None
            ],
        }
        counts = [len(result[query][kind]) for kind in ("articles", "chunks", "fragments")]
        print(query, *counts, file=sys.stderr, flush=True)
    out.write_text(json.dumps(result, ensure_ascii=False))


def _labels(path: Path = LABELS) -> dict[str, int]:
    return json.loads(path.read_text()) if path.exists() else {}


def evidence_key(query: str, article_id: int, ord_: int, text: str, start=None, end=None) -> str:
    normalized = " ".join(text.split())
    digest = hashlib.sha256(normalized.encode()).hexdigest()
    first, last = (ord_ if start is None else start), (ord_ if end is None else end)
    return f"{query}|{article_id}|{ord_}|{first}-{last}|{digest}"


def _items(result: dict):
    articles = [(a["id"], a["chunk"], a.get("snippet") or "") for a in result.get("articles", [])
                if a.get("chunk") is not None]
    chunks = [(p["article_id"], p["chunk"], p.get("text") or "") for p in result.get("chunks", [])]
    fragments = [(p["article_id"], p["chunk"], p.get("text") or "", p.get("from"), p.get("to"))
                 for p in result.get("fragments", [])]
    return articles, chunks, fragments


def pool(paths: list[Path], prefix: Path, labels_path: Path = LABELS) -> None:
    """正文或片段范围变了必须重新盲评；旧的三段式坐标留着存档，不猜测当时所见文本。"""
    labels = _labels(labels_path)
    entries: dict[str, dict[str, str]] = {}
    for path in paths:
        data = json.loads(path.read_text())
        for query in _queries():
            if query not in data:
                continue
            for group in _items(data[query]):
                for item in group[:TOP]:
                    key = evidence_key(query, *item)
                    if key not in labels:
                        entries.setdefault(query, {})[key] = item[2]
    rng = random.Random(7)
    lines, keymap = [], {}
    for query in _queries():
        items = list(entries.get(query, {}).items())
        if not items:
            continue
        rng.shuffle(items)
        lines.append(f"##### 问题：{query}")
        for key, body in items:
            code = hashlib.sha256(key.encode()).hexdigest()[:16]
            keymap[code] = key
            text = " ".join(body.split())
            lines.append(f"[{code}] {text}")
        lines.append("")
    Path(f"{prefix}_blind.txt").write_text("\n".join(lines))
    Path(f"{prefix}_keys.json").write_text(json.dumps(keymap, ensure_ascii=False))
    print(len(keymap), "段待打分 →", f"{prefix}_blind.txt")


def label(prefix: Path, grades: str, path: Path = LABELS) -> None:
    keymap = json.loads(Path(f"{prefix}_keys.json").read_text())
    labels = _labels(path)
    for item in grades.split():
        code, grade = item.split(":")
        if grade not in {"0", "1", "2"}:
            raise SystemExit(f"分数只能是 0、1、2：{item}")
        key = keymap[code]
        if not re.search(r"\|[0-9a-f]{64}$", key):
            raise SystemExit("旧盲评单未绑定正文，请重新 pool 后判分")
        labels[key] = int(grade)
    path.write_text(json.dumps(dict(sorted(labels.items())), ensure_ascii=False, indent=0) + "\n")
    print(len(grades.split()), "段记分；共", len(labels))


def _shingles(text: str, size: int = 20) -> set[str]:
    text = re.sub(r"\s+", "", text or "")
    return {text[i : i + size] for i in range(0, max(1, len(text) - size + 1), 5)}


def _duplicate(a: str, b: str) -> bool:
    sa, sb = _shingles(a), _shingles(b)
    return bool(sa and sb) and len(sa & sb) / min(len(sa), len(sb)) >= 0.5


def view(query: str, items: list, labels: dict[str, int], pool_items=None) -> dict[str, float]:
    """前 TOP 条：平均分、NDCG（理想排序用这个问题打过分的全部段落）、0 分条数、不重复的 2 分条数、没打分的条数。"""
    grades = [labels.get(evidence_key(query, *item)) for item in items[:TOP]]
    texts = [item[2] for item in items[:TOP]]
    known = [g for g in grades if g is not None]
    current_keys = {evidence_key(query, *item) for item in (items if pool_items is None else pool_items)}
    ideal = sorted((labels[key] for key in current_keys if key in labels), reverse=True)[:TOP]
    dcg = sum((2**g - 1) / math.log2(i + 2) for i, g in enumerate(grades) if g is not None)
    idcg = sum((2**g - 1) / math.log2(i + 2) for i, g in enumerate(ideal))
    return {
        "mean": sum(known) / len(known) if known else 0.0,
        "ndcg": dcg / idcg if idcg else 0.0,
        "zeros": sum(1 for g in known if g == 0),
        "fresh_twos": sum(
            1 for i, g in enumerate(grades) if g == 2 and not any(_duplicate(texts[i], texts[j]) for j in range(i))
        ),
        "focus_at_10": sum(g == 2 for g in known) / TOP,
        "judged": len(known),
        "unlabelled": sum(1 for g in grades if g is None),
    }


def metrics(path: Path) -> None:
    data = json.loads(path.read_text())
    labels = _labels()
    focus = _labels(FOCUS)
    pools = {q: [item for group in _items(results) for item in group] for q, results in data.items()}
    for split in QUERIES:
        present = [q for q in QUERIES[split] if q in data]
        print(f"{split} 已收集 {len(present)}/{len(QUERIES[split])} 道题；旧坐标标签不计入本次结果")
        if not present:
            continue
        for name, index, grades in (("文章级", 0, labels), ("段落级", 1, labels), ("片段级", 2, labels),
                                    ("片段专门讲", 2, focus)):
            rows = [view(q, _items(data[q])[index], grades, pools[q]) for q in present]
            judged = [row for row in rows if row["judged"]]
            marked = sum(row["judged"] for row in rows)
            unmarked = sum(row["unlabelled"] for row in rows)
            coverage = f"有评分 {len(judged)}/{len(rows)} 题，已判 {marked}/{marked + unmarked} 条"
            empty = sum(not row["judged"] and not row["unlabelled"] for row in rows)
            if empty:
                coverage += f"，{empty} 题无结果"
            if not judged:
                print(f"{split:4} {name} {coverage}" + ("，等待人工按此版正文判分" if unmarked else ""))
                continue
            if name == "片段专门讲":
                scores = (("ndcg", "ndcg"), ("专门讲占比", "focus_at_10"))
                missing_label = "没打分"
            else:
                scores = ((key, key) for key in ("mean", "ndcg", "zeros", "fresh_twos"))
                missing_label = "unlabelled"
            summary = " ".join(f"{label}={sum(row[key] for row in judged) / len(judged):.3f}" for label, key in scores)
            print(f"{split:4} {name} {summary} {missing_label}={unmarked / len(rows):.3f} {coverage}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dir", default="data/quality-eval", help="结果和盲评单放哪（不进仓库）")
    parser.add_argument("--base", default="https://investment.zldong.com")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("fetch").add_argument("name")
    pool_parser = sub.add_parser("pool")
    pool_parser.add_argument("names", nargs="+", help="几份结果的名字，最后一个是盲评单的前缀")
    pool_parser.add_argument("--focus", action="store_true", help="准备专注度盲评单")
    label_parser = sub.add_parser("label")
    label_parser.add_argument("prefix")
    label_parser.add_argument("grades", help="'编号:分数 编号:分数 ...'")
    label_parser.add_argument("--focus", action="store_true", help="记到 focus.json：是不是专门讲这个问题")
    sub.add_parser("metrics").add_argument("name")
    args = parser.parse_args()
    folder = Path(args.dir)
    folder.mkdir(parents=True, exist_ok=True)
    if args.command == "fetch":
        fetch(args.base.rstrip("/"), folder / f"{args.name}.json")
    elif args.command == "pool":
        names = args.names if len(args.names) > 1 else args.names * 2
        pool([folder / f"{name}.json" for name in names[:-1]], folder / names[-1], FOCUS if args.focus else LABELS)
    elif args.command == "label":
        label(folder / args.prefix, args.grades, FOCUS if args.focus else LABELS)
    else:
        metrics(folder / f"{args.name}.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
