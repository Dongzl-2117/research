"""检索结果的信息质量盲评：从线上接口拉结果，把没打过分的段落打乱、去掉作者标题名次出盲评单，记分，算指标。

只读线上公开接口（不用登录、不调模型、不花钱），结果和盲评单写进 --dir（默认 data/quality-eval，不进仓库：
里面是库里的原文）。分数记在本目录的 labels.json（只有「问题|文章|块号 → 分数」，不带原文），可以提交。

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
QUERIES = json.loads((HERE / "queries.json").read_text())
TOP = 10


def _queries() -> list[str]:
    return QUERIES["tune"] + QUERIES["hold"]


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
    """文章级前 50（检索页卡片的摘录段）和段落级前 50（问库、外部调用方拿到的）。"""
    result = {}
    for query in _queries():
        encoded = urllib.parse.quote(query)
        articles = _get(f"{base}/api/v1/search?limit=50&q={encoded}") or {}
        chunks = _get(f"{base}/api/v1/search?level=chunk&limit=50&q={encoded}") or {}
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
        }
        print(query, len(result[query]["articles"]), len(result[query]["chunks"]), file=sys.stderr, flush=True)
    out.write_text(json.dumps(result, ensure_ascii=False))


def _labels() -> dict[str, int]:
    return json.loads(LABELS.read_text()) if LABELS.exists() else {}


def _items(result: dict) -> tuple[list[tuple[int, int, str]], list[tuple[int, int, str]]]:
    articles = [
        (a["id"], a["chunk"], a.get("snippet") or "") for a in result.get("articles", []) if a.get("chunk") is not None
    ]
    chunks = [(p["article_id"], p["chunk"], p.get("text") or "") for p in result.get("chunks", [])]
    return articles, chunks


def pool(paths: list[Path], prefix: Path) -> None:
    """几份结果里前 TOP 条、还没打过分的段落，按问题分组、打乱，编号盲评。"""
    labels = _labels()
    entries: dict[str, dict[str, str]] = {}
    for path in paths:
        data = json.loads(path.read_text())
        for query in _queries():
            if query not in data:
                continue
            articles, chunks = _items(data[query])
            for article_id, ord_, body in articles[:TOP] + chunks[:TOP]:
                key = f"{query}|{article_id}|{ord_}"
                if key not in labels:
                    entries.setdefault(query, {})[key] = body
    rng = random.Random(7)
    lines, keymap = [], {}
    for query in _queries():
        items = list(entries.get(query, {}).items())
        if not items:
            continue
        rng.shuffle(items)
        lines.append(f"##### 问题：{query}")
        for key, body in items:
            code = hashlib.md5(key.encode()).hexdigest()[:6]
            keymap[code] = key
            text = " ".join(body.split())
            lines.append(f"[{code}] {text[:600]}{'…' if len(text) > 600 else ''}")
        lines.append("")
    Path(f"{prefix}_blind.txt").write_text("\n".join(lines))
    Path(f"{prefix}_keys.json").write_text(json.dumps(keymap, ensure_ascii=False))
    print(len(keymap), "段待打分 →", f"{prefix}_blind.txt")


def label(prefix: Path, grades: str) -> None:
    keymap = json.loads(Path(f"{prefix}_keys.json").read_text())
    labels = _labels()
    for item in grades.split():
        code, grade = item.split(":")
        if grade not in {"0", "1", "2"}:
            raise SystemExit(f"分数只能是 0、1、2：{item}")
        labels[keymap[code]] = int(grade)
    LABELS.write_text(json.dumps(dict(sorted(labels.items())), ensure_ascii=False, indent=0) + "\n")
    print(len(grades.split()), "段记分；共", len(labels))


def _shingles(text: str, size: int = 20) -> set[str]:
    text = re.sub(r"\s+", "", text or "")
    return {text[i : i + size] for i in range(0, max(1, len(text) - size + 1), 5)}


def _duplicate(a: str, b: str) -> bool:
    sa, sb = _shingles(a), _shingles(b)
    return bool(sa and sb) and len(sa & sb) / min(len(sa), len(sb)) >= 0.5


def view(query: str, items: list[tuple[int, int, str]], labels: dict[str, int]) -> dict[str, float]:
    """前 TOP 条：平均分、NDCG（理想排序用这个问题打过分的全部段落）、0 分条数、不重复的 2 分条数、没打分的条数。"""
    grades = [labels.get(f"{query}|{a}|{o}") for a, o, _ in items[:TOP]]
    texts = [body for _, _, body in items[:TOP]]
    known = [g for g in grades if g is not None]
    ideal = sorted((g for k, g in labels.items() if k.startswith(query + "|")), reverse=True)[:TOP]
    dcg = sum((2**g - 1) / math.log2(i + 2) for i, g in enumerate(grades) if g is not None)
    idcg = sum((2**g - 1) / math.log2(i + 2) for i, g in enumerate(ideal))
    return {
        "mean": sum(known) / len(known) if known else 0.0,
        "ndcg": dcg / idcg if idcg else 0.0,
        "zeros": sum(1 for g in known if g == 0),
        "fresh_twos": sum(
            1 for i, g in enumerate(grades) if g == 2 and not any(_duplicate(texts[i], texts[j]) for j in range(i))
        ),
        "unlabelled": sum(1 for g in grades if g is None),
    }


def metrics(path: Path) -> None:
    data = json.loads(path.read_text())
    labels = _labels()
    for split in ("tune", "hold"):
        for name, index in (("文章级", 0), ("段落级", 1)):
            rows = [view(q, _items(data[q])[index], labels) for q in QUERIES[split] if q in data]
            if not rows:
                continue
            summary = {k: sum(r[k] for r in rows) / len(rows) for k in rows[0]}
            print(f"{split:4} {name} " + " ".join(f"{k}={v:.3f}" for k, v in summary.items()))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dir", default="data/quality-eval", help="结果和盲评单放哪（不进仓库）")
    parser.add_argument("--base", default="https://investment.zldong.com")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("fetch").add_argument("name")
    pool_parser = sub.add_parser("pool")
    pool_parser.add_argument("names", nargs="+", help="几份结果的名字，最后一个是盲评单的前缀")
    label_parser = sub.add_parser("label")
    label_parser.add_argument("prefix")
    label_parser.add_argument("grades", help="'编号:分数 编号:分数 ...'")
    sub.add_parser("metrics").add_argument("name")
    args = parser.parse_args()
    folder = Path(args.dir)
    folder.mkdir(parents=True, exist_ok=True)
    if args.command == "fetch":
        fetch(args.base.rstrip("/"), folder / f"{args.name}.json")
    elif args.command == "pool":
        names = args.names if len(args.names) > 1 else args.names * 2
        pool([folder / f"{name}.json" for name in names[:-1]], folder / names[-1])
    elif args.command == "label":
        label(folder / args.prefix, args.grades)
    else:
        metrics(folder / f"{args.name}.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
