"""造一份可控的评测语料，写进独立的 research_eval 库（不要指向正式库）。

    createdb research_eval && psql research_eval -c "CREATE EXTENSION zhparser"
    python3 tools/retrieval_eval/gen_corpus.py --articles 6000


每段带真值：主角公司、主题、作者、年份。段落写成 520～700 字，切块后基本一段一块，
再按段落文字把真值对到块上。套话段落故意塞满「公司、数据、核心、关键、时间、观点」，
模拟真实文章里大量出现的泛词；顺带提及用「比如」「就像」句式。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

from psycopg.types.json import Jsonb

from researchlib.chunks import replace_chunks
from researchlib.config import Settings
from researchlib.db import prepare
from researchlib.mentions import rebuild_all

SH = timezone(timedelta(hours=8))
ROOT = Path(__file__).resolve().parents[2]

COMPANIES = {
    "腾讯": ["腾讯", "腾讯控股"],
    "拼多多": ["拼多多", "PDD"],
    "贵州茅台": ["茅台", "贵州茅台"],
    "农夫山泉": ["农夫山泉"],
    "苹果": ["苹果", "Apple"],
    "美团": ["美团"],
    "阿里巴巴": ["阿里巴巴", "阿里"],
    "京东": ["京东"],
    "宁德时代": ["宁德时代"],
    "比亚迪": ["比亚迪"],
    "英伟达": ["英伟达", "NVIDIA"],
    "开市客": ["Costco", "开市客"],
}

# 主题 → 同义说法。问题里用第一个，正文里随机用其中一个，用来测同义召回。
CONCEPTS = {
    "护城河": ["护城河", "壁垒", "竞争优势", "门槛"],
    "定价权": ["定价权", "提价能力", "涨价空间"],
    "广告加载率": ["广告加载率", "加载率", "广告库存"],
    "批价": ["批价", "一批价", "批发价"],
    "渠道库存": ["渠道库存", "经销商库存", "社会库存"],
    "回购": ["回购", "分红", "股东回报"],
    "出海": ["出海", "海外业务", "国际化"],
    "管理层": ["管理层", "股权激励", "组织架构"],
    "成本结构": ["成本结构", "毛利率", "原材料成本"],
    "用户增长": ["用户增长", "日活", "用户规模"],
    "估值": ["估值", "市盈率", "自由现金流"],
    "低价": ["低价", "性价比", "价格战"],
    "监管": ["监管", "反垄断", "政策风险"],
    "游戏": ["游戏", "版号", "手游"],
    "算力": ["算力", "大模型", "GPU"],
}

# 公司 → 常见主题，让语料像真实文章：谈茅台多半谈批价、库存、定价权。
AFFINITY = {
    "腾讯": ["广告加载率", "游戏", "回购", "用户增长", "监管", "护城河", "算力"],
    "拼多多": ["低价", "出海", "护城河", "管理层", "监管", "用户增长"],
    "贵州茅台": ["批价", "渠道库存", "定价权", "护城河", "管理层", "估值"],
    "农夫山泉": ["成本结构", "护城河", "定价权", "渠道库存"],
    "苹果": ["定价权", "回购", "护城河", "估值", "成本结构"],
    "美团": ["低价", "监管", "用户增长", "护城河"],
    "阿里巴巴": ["回购", "估值", "管理层", "算力"],
    "京东": ["低价", "成本结构", "估值"],
    "宁德时代": ["成本结构", "出海", "定价权"],
    "比亚迪": ["出海", "低价", "成本结构"],
    "英伟达": ["算力", "估值", "定价权"],
    "开市客": ["护城河", "低价", "用户增长"],
}

AUTHORS = [
    ("fangwei-mp", "方伟", 0.30),
    ("fangwei-zsxq", "方伟", 0.20),
    ("chengfengpolang", "乘风破浪", 0.25),
    ("gudujinhuati", "孤独进化体", 0.15),
    ("linwenfeng-feishu", "林文丰", 0.10),
]

STANCES = [
    "我的判断是，{c}在{k}上的位置比市场想象的更稳，原因在于供给端很难被复制。",
    "这一点我和不少人看法不同：{c}的{k}并没有变弱，变的是短期预期。",
    "风险在于{c}的{k}可能已经见顶，如果后面两个季度没有改善，我会下调判断。",
    "我可能错在对{c}的{k}过于乐观，验证方法是看接下来的经营数据能不能接上。",
    "从生意机制上看，{c}的{k}来自需求端的心智和供给端的效率，两者缺一不可。",
    "{c}这几年在{k}上的变化，说明组织和激励已经跟上了业务的节奏。",
]

DETAILS = [
    "具体看，{k}的变化要放到行业周期里理解，单看一个季度容易被噪音误导。",
    "很多人把{k}当成结果，其实它是原因：先有用户心智，后有{k}。",
    "如果把时间拉长到五年，{k}的趋势比任何单季的数字都更能说明问题。",
    "和同行对比，{c}在{k}上的差距不是靠一次投入能补上的。",
    "管理层在电话会上也谈到了{k}，但给出的口径比较保守。",
    "我更关心{k}能不能持续，而不是今年能做到多少。",
]

FILLERS = [
    "公司的核心数据在这个时间点需要重新看一遍，关键在于不要被短期波动带偏。",
    "从关键数据看，市场在这个阶段的分歧很大，不同作者的观点也各不相同。",
    "很多人讨论这个问题时忽略了时间维度，按时间梳理之后，核心矛盾会清楚很多。",
    "这家公司的数据和观点值得持续关注，但我们不需要对每个数据都做出反应。",
    "关于公司经营，核心还是看长期，短期的数据只能作为参考而不是结论。",
    "这段时间市场情绪起伏，关键的问题是公司的基本面有没有发生变化。",
    "我们按时间顺序梳理了一下过去几年的关键数据，大体趋势没有改变。",
    "观点可以不同，但数据是同一份，分歧来自对数据的解读而不是数据本身。",
    "投资里最难的是等待，大部分时间什么都不做才是正确的选择。",
    "写这篇的时候又翻了一遍过去的笔记，发现当时担心的很多事情并没有发生。",
]

PASSING = [
    "比如{o}当年也遇到过类似的问题。",
    "就像{o}一样，好生意往往看起来很无聊。",
    "顺便说一句，{o}最近的动作也值得留意。",
]

TITLES = [
    "{c}：关于{k}的再思考",
    "读{c}财报的几点笔记",
    "{c}的{k}，我是怎么看的",
    "周记：{c}、{c2}和最近的几件事",
    "关于{k}的一些看法",
    "随笔：长期主义和{k}",
]


def pick_author(rng: random.Random):
    roll = rng.random()
    acc = 0.0
    for source_id, author, weight in AUTHORS:
        acc += weight
        if roll <= acc:
            return source_id, author
    return AUTHORS[-1][0], AUTHORS[-1][1]


def surface(rng: random.Random, company: str) -> str:
    return rng.choice(COMPANIES[company])


def fill(rng: random.Random, sentences: list[str], target: int) -> str:
    text = "".join(sentences)
    while len(text) < target:
        text += rng.choice(FILLERS)
    return text


def topic_paragraph(rng, company, concept, others):
    c = surface(rng, company)
    word = rng.choice(CONCEPTS[concept])
    parts = [rng.choice(STANCES).format(c=c, k=word)]
    for _ in range(rng.randint(2, 4)):
        word = rng.choice(CONCEPTS[concept])
        parts.append(rng.choice(DETAILS).format(c=c, k=word))
    if others and rng.random() < 0.35:
        parts.insert(rng.randint(1, len(parts)), rng.choice(PASSING).format(o=surface(rng, rng.choice(others))))
    rng.shuffle(parts[1:])
    return fill(rng, parts, rng.randint(520, 700))


def company_general_paragraph(rng, company, others):
    c = surface(rng, company)
    parts = [
        f"再说{c}。这家公司过去一年的变化不小，但我关注的点和市场不太一样。",
        f"{c}的经营节奏比较稳，管理层的表态也比较克制。",
    ]
    if others and rng.random() < 0.4:
        parts.append(rng.choice(PASSING).format(o=surface(rng, rng.choice(others))))
    return fill(rng, parts, rng.randint(520, 700))


def concept_only_paragraph(rng, concept):
    word = rng.choice(CONCEPTS[concept])
    parts = [
        f"抛开具体公司，单独说说{word}。",
        rng.choice(DETAILS).format(c="一家公司", k=word),
        rng.choice(DETAILS).format(c="很多公司", k=rng.choice(CONCEPTS[concept])),
    ]
    return fill(rng, parts, rng.randint(520, 700))


def filler_paragraph(rng, others):
    parts = []
    if others and rng.random() < 0.3:
        parts.append(rng.choice(PASSING).format(o=surface(rng, rng.choice(others))))
    return fill(rng, parts, rng.randint(520, 700))


def build_article(rng, index: int, start: datetime, span_days: int):
    source_id, author = pick_author(rng)
    published = start + timedelta(days=rng.randint(0, span_days), minutes=rng.randint(0, 1439))
    names = list(COMPANIES)
    mains = rng.sample(names, 2 if rng.random() < 0.3 else 1)
    others = [name for name in names if name not in mains]
    plan = []
    n = rng.randint(3, 11)
    for _ in range(n):
        roll = rng.random()
        company = rng.choice(mains)
        if roll < 0.45:
            concept = rng.choice(AFFINITY[company])
            plan.append(("topic", company, concept, topic_paragraph(rng, company, concept, others)))
        elif roll < 0.62:
            plan.append(("company", company, None, company_general_paragraph(rng, company, others)))
        elif roll < 0.72:
            concept = rng.choice(list(CONCEPTS))
            plan.append(("concept", None, concept, concept_only_paragraph(rng, concept)))
        else:
            plan.append(("filler", None, None, filler_paragraph(rng, others)))
    main = mains[0]
    concept = next((p[2] for p in plan if p[0] == "topic"), rng.choice(AFFINITY[main]))
    template = rng.choice(TITLES)
    title = template.format(
        c=surface(rng, main), c2=surface(rng, mains[-1] if len(mains) > 1 else rng.choice(others)),
        k=rng.choice(CONCEPTS[concept]),
    ) + f"（{index}）"
    body = "\n\n".join(p[3] for p in plan)
    return {
        "source_id": source_id,
        "author": author,
        "published_at": published,
        "title": title,
        "body": body,
        "plan": [{"kind": p[0], "company": p[1], "concept": p[2], "text": p[3]} for p in plan],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--articles", type=int, default=6000)
    parser.add_argument("--seed", type=int, default=20261019)
    parser.add_argument("--url", default="postgresql://research:research@127.0.0.1:5432/research_eval")
    args = parser.parse_args()
    if not args.url.rstrip("/").rsplit("/", 1)[-1].startswith("research_eval"):
        raise SystemExit("评测语料只写进名字以 research_eval 开头的库，免得冲掉正式数据")
    rng = random.Random(args.seed)
    url = args.url
    settings = Settings.from_env({
        "DATABASE_URL": url,
        "DATA_DIR": str(ROOT / "data" / "eval"),
        "SOURCES_FILE": str(ROOT / "config" / "sources.yaml"),
    })
    conn = prepare(settings)
    conn.execute("TRUNCATE articles RESTART IDENTITY CASCADE")
    conn.execute("DROP TABLE IF EXISTS eval_truth")
    conn.execute(
        "CREATE TABLE eval_truth (article_id bigint, ord int, kind text, company text, concept text,"
        " author text, source_id text, year int, PRIMARY KEY (article_id, ord))"
    )
    conn.commit()
    start = datetime(2019, 1, 1, tzinfo=SH)
    span = (datetime(2026, 10, 18, tzinfo=SH) - start).days
    for index in range(args.articles):
        art = build_article(rng, index, start, span)
        digest = hashlib.sha256(art["body"].encode()).hexdigest()
        row = conn.execute(
            "INSERT INTO articles (source_id, external_id, content_hash, title, published_at, body_md, body_text,"
            " raw_meta) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (art["source_id"], f"eval:{index}", digest, art["title"], art["published_at"], art["body"],
             art["body"], Jsonb({"eval": True})),
        ).fetchone()
        article_id = row["id"]
        replace_chunks(conn, article_id, art["title"], art["body"])
        chunks = conn.execute(
            "SELECT ord, body FROM article_chunks WHERE article_id = %s ORDER BY ord", (article_id,)
        ).fetchall()
        for chunk in chunks:
            hits = [p for p in art["plan"] if p["text"][:60] in chunk["body"]]
            # 一块里如果并进了多段，取最「具体」的那段当真值（topic > company > concept > filler）。
            order = {"topic": 0, "company": 1, "concept": 2, "filler": 3}
            hits.sort(key=lambda p: order[p["kind"]])
            p = hits[0] if hits else {"kind": "filler", "company": None, "concept": None}
            conn.execute(
                "INSERT INTO eval_truth VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (article_id, chunk["ord"], p["kind"], p["company"], p["concept"], art["author"],
                 art["source_id"], art["published_at"].year),
            )
        if index % 500 == 499:
            conn.commit()
            print(f"{index + 1} 篇", flush=True)
    conn.commit()
    print("重算公司提及…", flush=True)
    rebuild_all(conn)
    conn.commit()
    stats = conn.execute(
        "SELECT (SELECT count(*) FROM articles) AS a, (SELECT count(*) FROM article_chunks) AS c,"
        " (SELECT count(*) FROM article_mentions) AS m"
    ).fetchone()
    print(json.dumps(stats, default=str))
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
