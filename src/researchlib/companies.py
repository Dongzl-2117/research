"""公司表。种子只在 seeded 仍为真时覆盖，手工改过的行不会被启动流程写回去。"""

from __future__ import annotations

import re

from psycopg.types.json import Jsonb

from researchlib.company_seed import ALIAS_RULES, MARKETS, SEEDS
from researchlib.mentions import alias_key, iter_alias_specs


def validate_rules() -> None:
    """歧义规则只能挂在种子公司上；needs 的键必须是这家公司自己的写法；正则要编译得过。"""
    by_name = {seed.name_zh: seed for seed in SEEDS}
    for name, rules in ALIAS_RULES.items():
        seed = by_name.get(name)
        if seed is None:
            raise ValueError(f"歧义规则挂在不存在的公司上：{name}")
        unknown = set(rules) - {"needs", "conflicts", "exclude"}
        if unknown:
            raise ValueError(f"{name} 的规则里有不认识的键：{'、'.join(sorted(unknown))}")
        own = {seed.name_zh, seed.name_en, seed.ticker or "", *seed.aliases}
        for alias, words in (rules.get("needs") or {}).items():
            if alias not in own:
                raise ValueError(f"{name} 的 needs 里有不是它的写法：{alias}")
            if not words:
                raise ValueError(f"{name} 的 {alias} 没有佐证词")
        for alias in rules.get("conflicts") or {}:
            if alias not in (rules.get("needs") or {}):
                raise ValueError(f"{name} 的 conflicts 里的 {alias} 没有对应的 needs")
        for pattern in rules.get("exclude") or []:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"{name} 的排除规则写错了：{pattern}（{exc}）") from exc


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
    validate_rules()


def seed_companies(conn) -> int:
    validate_seed()
    for seed in SEEDS:
        conn.execute(
            """
            INSERT INTO companies (ticker, name_zh, name_en, market, aliases, alias_rules, seeded)
            VALUES (%(ticker)s, %(name_zh)s, %(name_en)s, %(market)s, %(aliases)s, %(rules)s, true)
            ON CONFLICT (name_zh) DO UPDATE SET
              ticker = EXCLUDED.ticker,
              name_en = EXCLUDED.name_en,
              market = EXCLUDED.market,
              aliases = EXCLUDED.aliases,
              alias_rules = EXCLUDED.alias_rules,
              seeded = true
            WHERE companies.seeded = true
            """,
            {
                "ticker": seed.ticker,
                "name_zh": seed.name_zh,
                "name_en": seed.name_en,
                "market": seed.market,
                "aliases": list(seed.aliases),
                "rules": Jsonb(ALIAS_RULES.get(seed.name_zh, {})),
            },
        )
    return len(SEEDS)
