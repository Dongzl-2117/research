"""把查询参数（author、source、company、from、to）换成检索范围。页面、JSON 接口和问答共用。"""

from __future__ import annotations

import json
from dataclasses import replace
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


# 地址里的整数（编号、offset、页码）都进 SQL 当 bigint 用。超出范围的和不是数字的一样当没给，
# 不然 /api/v1/articles?offset=99999999999999999999 查下去报「bigint out of range」，回 500。
BIGINT_MAX = 2**63 - 1


def parse_int(value: str | None) -> int | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        number = int(text)
    except ValueError:
        return None
    return number if -BIGINT_MAX <= number <= BIGINT_MAX else None


def resolve_scope(
    params, sources, *, ignore_source: bool = False, empty_intersection: bool = False,
) -> tuple[Scope, dict]:
    """sources 是已启用来源。作者或来源不存在时 404，不悄悄退回全库。"""
    ids = [source.id for source in sources]
    author = (params.get("author") or "").strip()
    source_id = "" if ignore_source else (params.get("source") or "").strip()
    if author:
        ids = [source.id for source in sources if source.author == author]
        if not ids:
            raise HTTPException(status_code=404)
    if source_id and source_id != "all":
        if source_id not in ids and (not empty_intersection or not any(source.id == source_id for source in sources)):
            raise HTTPException(status_code=404)
        ids = [item for item in ids if item == source_id]
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
    tree_id = parse_int(params.get("tree"))
    scope = Scope(
        source_ids=ids,
        company_id=company_id,
        date_from=date_from,
        date_to=date_to,
        article_id=article_id,
        tree_id=tree_id,
    )
    info = {
        "author": author,
        "source": source_id,
        "company": company_id,
        "from": date_from.isoformat() if date_from else "",
        "to": date_to.isoformat() if date_to else "",
        "article": article_id,
        "tree": tree_id,
    }
    return scope, info


def resolve_ask_scope(params, sources) -> tuple[Scope, dict]:
    """重新打开回答时沿用生成该回答的来源快照；已停用的来源不能再检索。"""
    scope, info = resolve_scope(params, sources)
    snapshot = params.get("saved_sources")
    if not snapshot:
        return scope, info
    try:
        ids = json.loads(snapshot)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail="来源范围无效") from exc
    if not isinstance(ids, list) or any(not isinstance(item, str) for item in ids):
        raise HTTPException(status_code=400, detail="来源范围无效")
    allowed = set(ids)
    kept = [item for item in scope.source_ids if item in allowed]
    info["saved_sources"] = snapshot
    info["source_options"] = [source.id for source in sources if source.id in allowed]
    return replace(scope, source_ids=kept), info


def saved_ask_filters(scope: dict, sources) -> dict:
    """旧回答也只依据实际保存的 source_ids 恢复范围，不能凭作者名扩展到后来新增的来源。"""
    saved = scope.get("source_ids")
    params = {
        "company": str(scope.get("company_id") or ""),
        "from": scope.get("date_from") or "",
        "to": scope.get("date_to") or "",
        "article": str(scope.get("article_id") or ""),
        "tree": str(scope.get("tree_id") or ""),
    }
    if saved is not None:
        params["saved_sources"] = json.dumps(saved, ensure_ascii=False)
        chosen = [source for source in sources if source.id in saved]
        if len(chosen) != len(sources):
            authors = {source.author for source in chosen}
            if len(authors) == 1:
                params["author"] = next(iter(authors))
    return resolve_ask_scope(params, sources)[1]
