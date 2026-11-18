"""人工审核新公司前，离线列出未收录的交易所代码和明确指定的公司名，不写库。"""

from __future__ import annotations

import argparse
import json
import re

from researchlib.author_voice import site_visible_sql
from researchlib.config import Settings
from researchlib.db import connect
from researchlib.mentions import iter_alias_specs, load_company_rows
from researchlib.visibility import visible_sql

_CODE = re.compile(r"(?<![\w.])(?:\d{1,5}\.HK|\d{6}\.(?:SH|SZ|BJ))(?![\w.])", re.I | re.ASCII)


def code_key(code: str) -> str:
    number, market = code.upper().split(".")
    return f"{int(number):05d}.HK" if market == "HK" else f"{number}.{market}"


def candidates(articles, companies, names: list[str]) -> list[dict]:
    known = {alias.casefold() for company in companies for alias, _ in iter_alias_specs(company)}
    known_codes = {code_key(key) for key in known if _CODE.fullmatch(key)}
    names = [name.strip() for name in names if name.strip() and name.strip().casefold() not in known]
    found = {}
    for article in articles:
        content = (article["title"] or "") + "\n" + (article["body_text"] or "")
        hits = {code_key(match[0]) for match in _CODE.finditer(content)} - known_codes
        if names:
            folded = content.casefold()
            hits.update(name for name in names if name.casefold() in folded)
        for term in hits:
            row = found.setdefault(term, {"candidate": term, "articles": 0, "sources": set(), "examples": []})
            row["articles"] += 1
            row["sources"].add(article["source_id"])
            if len(row["examples"]) < 5:
                row["examples"].append({"id": article["id"], "title": article["title"]})
    for row in found.values():
        row["sources"] = sorted(row["sources"])
    return sorted(found.values(), key=lambda r: (-len(r["sources"]), -r["articles"], r["candidate"]))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", action="append", default=[], help="同时核对标题或正文里这个未收录的名称，可重复")
    args = parser.parse_args(argv)
    with connect(Settings.from_env()) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        companies = load_company_rows(conn)
        with conn.cursor(name="company_review") as articles:
            articles.execute(f"""
                SELECT a.id, a.title, a.body_text, a.source_id FROM articles a
                JOIN sources s ON s.id = a.source_id
                WHERE s.enabled AND a.duplicate_of IS NULL AND {site_visible_sql("a")} AND {visible_sql("a")}
                ORDER BY a.published_at DESC NULLS LAST, a.id DESC
                """)
            print(json.dumps(candidates(articles, companies, args.name), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
