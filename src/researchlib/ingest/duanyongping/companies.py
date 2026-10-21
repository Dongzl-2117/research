"""判断「是不是在谈一家具体的公司」。

研究库公司表里的名字优先。2006–2018 年博客里常出现、种子里还没有的公司补在下面，
只用于这篇要不要入库，不写进公司表，也不改别的来源的提及。
"""

from __future__ import annotations

from functools import lru_cache

from researchlib.company_seed import ALIAS_RULES, SEEDS
from researchlib.ingest.duanyongping.policy import sentence_role
from researchlib.mentions import CompanyMatcher, alias_key, sentence_pieces

# (中文名, 其它写法)。不要和种子里的别名重复，否则会抢匹配。
EXTRAS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("万科", ("万科企业", "000002.SZ")),
    ("雅虎", ("Yahoo",)),
    ("富国银行", ("富国", "Wells Fargo", "WFC")),
    ("通用电气", ()),
    ("通用汽车", ("General Motors",)),
    ("云南白药", ()),
    ("苏宁易购", ("苏宁",)),
    ("国美", ("国美零售",)),
    ("双汇发展", ("双汇",)),
    ("索尼", ("Sony",)),
    ("松下", ()),
    ("诺基亚", ("Nokia",)),
    ("摩托罗拉", ("Motorola",)),
    ("三星电子", ("三星", "Samsung")),
    ("戴尔", ("Dell",)),
    ("惠普", ()),
    ("思科", ("Cisco",)),
    ("宝洁", ()),
    ("强生", ("强生公司",)),
    ("步步高", ()),
    ("OPPO", ()),
    ("vivo", ()),
    ("创维", ("创维集团",)),
    ("盐田港", ()),
    ("海螺水泥", ("海螺",)),
    ("中国人寿", ()),
    ("中国太保", ("太保",)),
    ("汇丰", ("汇丰控股", "HSBC")),
    ("美国银行", ("Bank of America",)),
    ("花旗", ("花旗集团", "Citigroup")),
    ("福特汽车", ("福特",)),
    ("丰田", ("丰田汽车", "Toyota")),
    ("本田", ("本田汽车", "Honda")),
    ("大众汽车", ()),
    ("宝马", ("BMW",)),
    ("奔驰", ()),
    ("麦当劳", ("McDonald",)),
    ("雅戈尔", ()),
    ("IBM", ()),
)

# 短别名嵌在别的词里时不算公司。「不要太保守」不是中国太保。
EXTRA_RULES: dict[str, dict] = {
    "中国太保": {"exclude": [r"太保(?:守|险|重|健|姆)"]},
    "步步高": {"exclude": [r"步步高升"]},
    "三星电子": {"exclude": [r"三星级", r"(?:给|打|评)三星"]},
}


def library_names() -> set[str]:
    """研究库公司表里的中文名。只命中补名单、库里没有的公司，不进观点。"""
    return {seed.name_zh for seed in SEEDS}


def extra_alias_keys() -> set[str]:
    found: set[str] = set()
    for name, aliases in EXTRAS:
        found.add(alias_key(name))
        found.update(alias_key(item) for item in aliases if item)
    return found


def seed_alias_keys() -> set[str]:
    found: set[str] = set()
    for seed in SEEDS:
        for text in (seed.name_zh, seed.name_en, seed.ticker or "", *seed.aliases):
            if text:
                found.add(alias_key(text))
    return found


def _rows() -> list[dict]:
    rows: list[dict] = []
    for index, seed in enumerate(SEEDS, start=1):
        rows.append(
            {
                "id": index,
                "name_zh": seed.name_zh,
                "name_en": seed.name_en,
                "ticker": seed.ticker,
                "aliases": list(seed.aliases),
                "alias_rules": ALIAS_RULES.get(seed.name_zh) or {},
            }
        )
    base = 10000
    for offset, (name, aliases) in enumerate(EXTRAS, start=1):
        rows.append(
            {
                "id": base + offset,
                "name_zh": name,
                "name_en": "",
                "ticker": None,
                "aliases": list(aliases),
                "alias_rules": EXTRA_RULES.get(name) or {},
            }
        )
    return rows


@lru_cache(maxsize=1)
def matcher() -> tuple[CompanyMatcher, dict[int, str]]:
    rows = _rows()
    names = {int(row["id"]): row["name_zh"] for row in rows}
    return CompanyMatcher(rows), names


def companies_in(text: str) -> tuple[list[str], list[str], list[str]]:
    """返回 (论证落在其上的公司, 只出现在举例里的, 列举/致谢/闲聊里的)，按出现顺序。"""
    found, names = matcher()
    if not (text or "").strip():
        return [], [], []
    document = found.document(text)
    substantive: list[str] = []
    incidental: list[str] = []
    weak: list[str] = []
    seen_real: set[str] = set()
    seen_side: set[str] = set()
    seen_weak: set[str] = set()

    def add(bucket: list[str], seen: set[str], sentence: str) -> None:
        for _start, _end, company_id in document.spans(sentence):
            name = names.get(company_id)
            if not name or name in seen or name in seen_real:
                continue
            seen.add(name)
            bucket.append(name)

    for sentence in sentence_pieces(text):
        spans = document.spans(sentence)
        if not spans:
            continue
        surfaces = [sentence[start:end] for start, end, _company_id in spans]
        role = sentence_role(sentence, surfaces)
        if role == "real":
            add(substantive, seen_real, sentence)
        elif role == "incidental":
            add(incidental, seen_side, sentence)
        elif role == "weak":
            add(weak, seen_weak, sentence)
    incidental = [name for name in incidental if name not in seen_real]
    weak = [name for name in weak if name not in seen_real]
    return substantive, incidental, weak
