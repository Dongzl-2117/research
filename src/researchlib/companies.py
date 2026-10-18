"""公司表。种子只在 seeded 仍为真时覆盖，手工改过的行不会被启动流程写回去。"""

from __future__ import annotations

from researchlib.company_seed import MARKETS, SEEDS
from researchlib.mentions import alias_key, iter_alias_specs


def validate_seed() -> None:
    seen: dict[str, str] = {}
    names: set[str] = set()
    tickers: set[str] = set()
    for seed in SEEDS:
        if seed.market not in MARKETS:
            raise ValueError(f"{seed.name_zh} 的市场无效：{seed.market}")
        if seed.name_zh in names:
            raise ValueError(f"重复的中文名：{seed.name_zh}")
        names.add(seed.name_zh)
        if seed.ticker:
            if seed.ticker in tickers:
                raise ValueError(f"重复的代码：{seed.ticker}")
            tickers.add(seed.ticker)
        company = {
            "name_zh": seed.name_zh,
            "name_en": seed.name_en,
            "ticker": seed.ticker,
            "aliases": seed.aliases,
        }
        for text, _casefold in iter_alias_specs(company):
            key = alias_key(text)
            owner = seen.get(key)
            if owner is not None and owner != seed.name_zh:
                raise ValueError(f"别名 {text} 同时属于 {owner} 和 {seed.name_zh}")
            seen[key] = seed.name_zh


def seed_companies(conn) -> int:
    validate_seed()
    for seed in SEEDS:
        conn.execute(
            """
            INSERT INTO companies (ticker, name_zh, name_en, market, aliases, seeded)
            VALUES (%(ticker)s, %(name_zh)s, %(name_en)s, %(market)s, %(aliases)s, true)
            ON CONFLICT (name_zh) DO UPDATE SET
              ticker = EXCLUDED.ticker,
              name_en = EXCLUDED.name_en,
              market = EXCLUDED.market,
              aliases = EXCLUDED.aliases,
              seeded = true
            WHERE companies.seeded = true
            """,
            {
                "ticker": seed.ticker,
                "name_zh": seed.name_zh,
                "name_en": seed.name_en,
                "market": seed.market,
                "aliases": list(seed.aliases),
            },
        )
    return len(SEEDS)
