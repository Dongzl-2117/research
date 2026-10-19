"""把查询参数（author、source、company、from、to）换成检索范围。页面、JSON 接口和问答共用。"""

from __future__ import annotations

from datetime import date

from fastapi import HTTPException

from researchlib.web.queries import Scope


def parse_day(value: str | None) -> date | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def parse_int(value: str | None) -> int | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def resolve_scope(params, sources, *, ignore_source: bool = False) -> tuple[Scope, dict]:
    """sources 是已启用来源。作者或来源不存在时 404，不悄悄退回全库。"""
    ids = [source.id for source in sources]
    author = (params.get("author") or "").strip()
    source_id = "" if ignore_source else (params.get("source") or "").strip()
    if author:
        ids = [source.id for source in sources if source.author == author]
        if not ids:
            raise HTTPException(status_code=404)
    if source_id and source_id != "all":
        if source_id not in ids:
            raise HTTPException(status_code=404)
        ids = [source_id]
    else:
        source_id = ""
    raw_company = params.get("company")
    company_id = parse_int(raw_company)
    if raw_company and company_id is None:
        raise HTTPException(status_code=404)
    date_from = parse_day(params.get("from"))
    date_to = parse_day(params.get("to"))
    if date_from and date_to and date_from > date_to:
        date_from, date_to = date_to, date_from
    article_id = parse_int(params.get("article"))
    scope = Scope(
        source_ids=ids, company_id=company_id, date_from=date_from, date_to=date_to, article_id=article_id
    )
    info = {
        "author": author,
        "source": source_id,
        "company": company_id,
        "from": date_from.isoformat() if date_from else "",
        "to": date_to.isoformat() if date_to else "",
        "article": article_id,
    }
    return scope, info
