"""页面路由。内容只来自已启用的白名单来源。"""

from __future__ import annotations

import mimetypes
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from urllib.parse import quote, urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from researchlib.ask import PRESETS, ask
from researchlib.db import connect, ensure_search_session
from researchlib.llm import llm_configured
from researchlib.mentions import DEPTH_LABELS, DEPTHS, CompanyMatcher, company_on_card
from researchlib.render import render_body
from researchlib.snippets import answer_html, highlight_snippet, highlight_title, mention_snippet
from researchlib.sources import enabled_sources, load_sources, sync_sources
from researchlib.textutil import excerpt, split_frontmatter, strip_duplicate_title
from researchlib.translate.pair import bilingual_pairs, stamp_paragraph_ids
from researchlib.web import queries
from researchlib.web.daybrief import compose_day_brief
from researchlib.web.present import (
    FEED_ZONE,
    day_key,
    day_label,
    depth_chips,
    format_clock,
    format_date,
    format_iso,
    group_days,
    market_label,
    month_chart,
    order_cards,
    page_window,
    plan_feed_page,
    relative_label,
    resolve_feed_instant,
    weekday_label,
)
from researchlib.web.scoping import parse_day, resolve_scope

router = APIRouter()
SEARCH_PAGE_SIZE = 20
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))
TEMPLATES.env.filters["answer_html"] = answer_html
FAVICON = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">
<rect width="32" height="32" rx="8" fill="#176b75"/>
<circle cx="16" cy="16" r="5" fill="#faf9f6"/>
</svg>"""


def templates():
    return TEMPLATES


@contextmanager
def database(request: Request):
    conn = connect(request.app.state.settings)
    ensure_search_session(conn)
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()
    finally:
        conn.close()


def active_sources(request: Request, conn):
    settings = request.app.state.settings
    mtime = settings.sources_path.stat().st_mtime
    if getattr(request.app.state, "sources_mtime", None) != mtime:
        sources = load_sources(settings.sources_path)
        sync_sources(conn, sources)
        conn.commit()
        request.app.state.sources = sources
        request.app.state.sources_mtime = mtime
    return enabled_sources(request.app.state.sources)


def render_page(request: Request, name: str, context: dict, status_code: int = 200):
    settings = request.app.state.settings
    path = request.url.path
    if request.url.query:
        path = f"{path}?{request.url.query}"
    payload = {
        "query": request.query_params.get("q", ""),
        "nav": "",
        "public_host": settings.public_host,
        "canonical": settings.public_base_url + path,
        "sample_banner": False,
        "sources": [],
        "active_source": "",
    }
    payload.update(context)
    return templates().TemplateResponse(request, name, payload, status_code=status_code)


def _source_map(sources):
    return {source.id: source for source in sources}


def _snippet_text(body: str, title: str) -> str:
    text = (body or "").strip()
    if title and text.startswith(title):
        return text[len(title) :].lstrip()
    return text


def _card(row, source, settings, *, title=None, excerpt_text=None, extra=None, zone=None) -> dict:
    stored = row["published_at"]
    zone = zone or settings.display_timezone
    shown, known = resolve_feed_instant(
        stored,
        clock_hint=row.get("clock_hint"),
        date_source=row.get("date_source") or "",
        time_known=row.get("time_known"),
        zone=zone,
    )
    if excerpt_text is None and "body_text" in row:
        excerpt_text = excerpt(row["body_text"], title=row["title"], limit=180)
    card = {
        "id": row["id"],
        "title": row["title"] if title is None else title,
        "plain_title": row["title"],
        "date": format_date(stored, zone),
        "time": format_clock(shown, zone) if known else "",
        "iso": format_iso(shown, zone) if known else day_key(stored, zone),
        "day": day_key(stored, zone),
        "sort_at": shown if known else stored,
        "source": source.name,
        "source_id": source.id,
        "author": source.author,
        # 来源名和作者同名时（例如「乘风破浪」）只显示一次。
        "show_author": source.author != source.name,
        "excerpt": excerpt_text or "",
        "summary": "",
        "tags": [],
        "body_text": row.get("body_text") or "",
    }
    if extra:
        card.update(extra)
    return card


def _visible_tags(card: dict, tags: list[dict]) -> list[dict]:
    title = card.get("plain_title") or ""
    if not isinstance(title, str):
        title = str(title)
    body = card.get("body_text") or ""
    visible = []
    for tag in tags:
        aliases = [tag.get("name_en") or "", *(tag.get("aliases") or [])]
        if not company_on_card(title, body, name=tag["name"], aliases=aliases, depth=tag["depth"]):
            continue
        visible.append({"id": tag["id"], "name": tag["name"], "depth": tag["depth"]})
        if len(visible) >= 4:
            break
    return visible


def _decorate(conn, cards: list[dict]) -> list[dict]:
    """给卡片挂上公司标签和 LLM 摘要。两条批量查询，不按篇查。"""
    ids = [card["id"] for card in cards]
    tags = queries.article_tags(conn, ids, per_article=12)
    digests = queries.digests_for(conn, ids)
    for card in cards:
        card["tags"] = _visible_tags(card, tags.get(card["id"], []))
        digest = digests.get(card["id"])
        if digest and not card.get("summary"):
            card["summary"] = digest["summary"]
        card.pop("body_text", None)
    return cards


def _feed_days(conn, rows, by_id, settings):
    cards = [_card(row, by_id[row["source_id"]], settings, zone=FEED_ZONE) for row in rows]
    return group_days(order_cards(_decorate(conn, cards)))


def _query_string(**params) -> str:
    return urlencode({key: value for key, value in params.items() if value not in (None, "", 0)})


def _page_arg(request: Request) -> int:
    raw = request.query_params.get("page", "1")
    try:
        return max(1, int(raw))
    except ValueError:
        return 1


def _load_feed(conn, source_ids: list[str], settings, page: int):
    counts = queries.feed_counts(conn, source_ids)
    plan = plan_feed_page(
        counts["days"],
        counts["undated"],
        page,
        settings.days_per_page,
        settings.page_size,
    )
    if plan["mode"] == "undated":
        rows = queries.list_undated_articles(conn, source_ids, plan["limit"], plan["offset"])
    else:
        rows = queries.list_day_page(conn, source_ids, plan["limit"], plan["offset"])
    return counts, plan, rows


def _author_groups(sources):
    grouped: dict[str, list] = {}
    order: list[str] = []
    for source in sources:
        if source.author not in grouped:
            grouped[source.author] = []
            order.append(source.author)
        grouped[source.author].append(source)
    return order, grouped


@router.get("/")
def home(request: Request):
    settings = request.app.state.settings
    source_arg = request.query_params.get("source") or ""
    with database(request) as conn:
        sources = active_sources(request, conn)
        ids = [source.id for source in sources]
        by_id = _source_map(sources)
        chosen = source_arg if source_arg in by_id else None
        if source_arg and source_arg not in by_id and source_arg != "all":
            raise HTTPException(status_code=404)
        scoped = [chosen] if chosen else ids
        jump = parse_day(request.query_params.get("date"))
        if jump is not None:
            return _jump_to_day(conn, scoped, jump, settings, "/", source=chosen or "")
        counts, plan, rows = _load_feed(conn, scoped, settings, _page_arg(request))
        banner = queries.sample_state(conn, ids)
        days = _feed_days(conn, rows, by_id, settings)
    if chosen:
        heading = by_id[chosen].name
        lede = "只看这个来源，仍按天从新到旧。"
        empty = "这个来源还没有文章。"
    else:
        heading = "按天"
        lede = "按日期从新到旧。同一天里，新的在前。翻页按整天翻。"
        empty = "还没有文章。"
    return render_page(
        request,
        "home.html",
        {
            "nav": "archive",
            "title": heading,
            "lede": lede,
            "empty": empty,
            "sources": sources,
            "active_source": chosen or "",
            "days": days,
            "day_scope": _query_string(source=chosen or ""),
            "page": plan["page"],
            "pages": plan["pages"],
            "total": counts["articles"],
            "dated_days": counts["days"],
            "undated": counts["undated"],
            "sample_banner": banner,
        },
    )


def _jump_to_day(conn, source_ids: list[str], day, settings, path: str, **params):
    """把「跳到某天」换成那一天所在的页，带上锚点。没有那天就落到更早的最近一天。"""
    found = queries.nearest_day(conn, source_ids, day)
    if found is None:
        query = _query_string(**params)
        return RedirectResponse(path + (f"?{query}" if query else ""), status_code=303)
    page = queries.day_offset(conn, source_ids, found) // settings.days_per_page + 1
    query = _query_string(**params, page=page if page > 1 else "")
    return RedirectResponse(f"{path}{'?' + query if query else ''}#d-{found.isoformat()}", status_code=303)


@router.get("/authors")
def authors_page(request: Request):
    settings = request.app.state.settings
    with database(request) as conn:
        sources = active_sources(request, conn)
        ids = [source.id for source in sources]
        stats = queries.source_stats(conn, ids)
        banner = queries.sample_state(conn, ids)
        _order, grouped = _author_groups(sources)
        authors = []
        for name, items in grouped.items():
            item_ids = [item.id for item in items]
            rows = [stats[item_id] for item_id in item_ids if item_id in stats]
            firsts = [row["first_at"] for row in rows if row["first_at"]]
            lasts = [row["last_at"] for row in rows if row["last_at"]]
            latest = queries.latest_articles(conn, item_ids, 1)
            authors.append(
                {
                    "name": name,
                    "articles": sum(row["n"] for row in rows),
                    "sources": [item.name for item in items],
                    "first": format_date(min(firsts), settings.display_timezone) if firsts else "",
                    "last": format_date(max(lasts), settings.display_timezone) if lasts else "",
                    "latest": latest[0] if latest else None,
                    "companies": queries.top_companies(conn, item_ids, 6),
                }
            )
    authors.sort(key=lambda item: (-item["articles"], item["name"]))
    return render_page(
        request,
        "authors.html",
        {
            "nav": "authors",
            "title": "作者",
            "authors": authors,
            "sources": sources,
            "sample_banner": banner,
        },
    )


@router.get("/authors/{author}")
def author_page(request: Request, author: str):
    settings = request.app.state.settings
    source_arg = request.query_params.get("source") or ""
    with database(request) as conn:
        sources = active_sources(request, conn)
        _order, grouped = _author_groups(sources)
        author_sources = grouped.get(author)
        if not author_sources:
            raise HTTPException(status_code=404)
        by_id = _source_map(author_sources)
        if source_arg and source_arg not in by_id:
            raise HTTPException(status_code=404)
        chosen = source_arg or None
        scoped = [chosen] if chosen else [item.id for item in author_sources]
        jump = parse_day(request.query_params.get("date"))
        if jump is not None:
            return _jump_to_day(conn, scoped, jump, settings, f"/authors/{author}", source=chosen or "")
        counts, plan, rows = _load_feed(conn, scoped, settings, _page_arg(request))
        stats = queries.source_stats(conn, scoped)
        companies = queries.top_companies(conn, scoped, 12)
        banner = queries.sample_state(conn, [source.id for source in sources])
        days = _feed_days(conn, rows, by_id, settings)
    firsts = [row["first_at"] for row in stats.values() if row["first_at"]]
    lasts = [row["last_at"] for row in stats.values() if row["last_at"]]
    return render_page(
        request,
        "author.html",
        {
            "nav": "authors",
            "title": author,
            "author_name": author,
            "author_sources": author_sources,
            "sources": sources,
            "active_source": chosen or "",
            "days": days,
            "day_scope": _query_string(author=author, source=chosen or ""),
            "first": format_date(min(firsts), settings.display_timezone) if firsts else "",
            "last": format_date(max(lasts), settings.display_timezone) if lasts else "",
            "companies": companies,
            "page": plan["page"],
            "pages": plan["pages"],
            "total": counts["articles"],
            "dated_days": counts["days"],
            "undated": counts["undated"],
            "sample_banner": banner,
        },
    )



def _article_view(request: Request, article_id: int, mode: str):
    """mode: auto | zh | pairs | original。auto 有对照则对照，否则单语。"""
    settings = request.app.state.settings
    chunk_arg = request.query_params.get("chunk")
    with database(request) as conn:
        sources = active_sources(request, conn)
        ids = [source.id for source in sources]
        by_id = _source_map(sources)
        article = queries.get_article(conn, article_id, ids)
        if article is None:
            raise HTTPException(status_code=404)
        images = queries.article_images(conn, article_id)
        source = by_id[article["source_id"]]
        parent = (article["raw_meta"] or {}).get("parent")
        siblings = []
        if source.kind in {"feishu", "markdown"} and parent:
            siblings = queries.sibling_articles(conn, source.id, article_id, parent)
        mentions = queries.article_mentions(conn, article_id)
        digest = queries.digests_for(conn, [article_id]).get(article_id)
        quoted = None
        if chunk_arg is not None and chunk_arg.isdigit():
            wanted = int(chunk_arg)
            quoted = next((row for row in queries.article_chunks(conn, article_id) if row["ord"] == wanted), None)
        banner = queries.sample_state(conn, ids)
        translation = queries.get_translation(conn, article_id)
    mapping = {row["original_ref"]: f"/media/{row['storage_path']}" for row in images}
    fmt = (article["raw_meta"] or {}).get("format")
    _frontmatter, body_md = split_frontmatter(article["body_md"])
    if fmt != "html":
        body_md = strip_duplicate_title(body_md, article["title"])
    original_html = stamp_paragraph_ids(str(render_body(body_md, mapping, fmt)))

    has_translation = bool(translation and translation.get("body_html"))
    # 原文已改、译文 hash 对不上时仍可展示旧译文，但标 incomplete
    translation_complete = bool(
        translation
        and translation.get("complete")
        and translation.get("content_hash") == article.get("content_hash")
    )
    zh_html = stamp_paragraph_ids(translation["body_html"]) if has_translation else None
    raw_pairs = bilingual_pairs(original_html, zh_html) if has_translation and zh_html else None
    pairs = None
    if raw_pairs:
        pairs = [
            {
                "en": Markup(p.en),
                "zh": Markup(p.zh) if p.zh else None,
                "anchor": p.anchor,
            }
            for p in raw_pairs
        ]

    if mode == "original":
        body_language = "original"
    elif mode == "zh":
        body_language = "zh" if zh_html else "original"
    elif mode == "pairs":
        body_language = "pairs" if pairs else ("zh" if zh_html else "original")
    else:
        body_language = "pairs" if pairs else ("zh" if zh_html else "original")

    if body_language == "pairs":
        body_html = None
    elif body_language == "zh":
        body_html = Markup(zh_html) if zh_html else None
    else:
        body_html = Markup(original_html)

    meta = article["raw_meta"] or {}
    shown, known = resolve_feed_instant(
        article["published_at"],
        clock_hint=meta.get("create_time"),
        date_source=meta.get("date_source") or "",
        time_known=meta.get("time_known"),
        zone=settings.display_timezone,
    )
    original_url = meta.get("url") or ""
    return render_page(
        request,
        "article.html",
        {
            "nav": "archive",
            "title": article["title"],
            "article": article,
            "source": source,
            "date": format_date(article["published_at"], settings.display_timezone),
            "time": format_clock(shown, settings.display_timezone) if known else "",
            "iso": (
                format_iso(shown, settings.display_timezone)
                if known
                else day_key(article["published_at"], settings.display_timezone)
            ),
            "relative": relative_label(shown or article["published_at"], settings.display_timezone),
            "body_html": body_html,
            "body_language": body_language,
            "has_translation": has_translation,
            "translation_complete": translation_complete,
            "pairs": pairs or [],
            "title_zh": (translation or {}).get("title_zh") or "",
            "original_url": original_url,
            "siblings": siblings,
            "mentions": [
                {**row, "depth_label": DEPTH_LABELS[row["depth"]]} for row in mentions
            ],
            "digest": digest,
            "quoted": quoted,
            "sources": sources,
            "sample_banner": banner,
        },
    )


@router.get("/articles/{article_id}")
def article_page(request: Request, article_id: int):
    return _article_view(request, article_id, "auto")


@router.get("/articles/{article_id}/zh")
def article_page_zh(request: Request, article_id: int):
    return _article_view(request, article_id, "zh")


@router.get("/articles/{article_id}/original")
def article_page_original(request: Request, article_id: int):
    return _article_view(request, article_id, "original")



@router.get("/search")
def search_page(request: Request):
    settings = request.app.state.settings
    query = request.query_params.get("q", "").strip()[:120]
    with database(request) as conn:
        sources = active_sources(request, conn)
        ids = [source.id for source in sources]
        by_id = _source_map(sources)
        scope, info = resolve_scope(request.query_params, sources)
        # 来源分面的计数不受当前所选来源影响，否则切到一个来源后别的来源都显示 0。
        facet_scope, _ = resolve_scope(request.query_params, sources, ignore_source=True)
        banner = queries.sample_state(conn, ids)
        company = queries.get_company(conn, scope.company_id) if scope.company_id is not None else None
        if scope.company_id is not None and company is None:
            raise HTTPException(status_code=404)
        has_filter = bool(info["author"] or info["company"] or info["from"] or info["to"])
        counts = queries.search_counts(conn, query, facet_scope) if query else []
        count_map = {row["source_id"]: int(row["n"]) for row in counts}
        total_all = sum(count_map.values())
        total = count_map.get(info["source"], 0) if info["source"] else total_all
        page, pages, offset = page_window(total, _page_arg(request), SEARCH_PAGE_SIZE)
        hits = queries.search_articles(conn, query, scope, SEARCH_PAGE_SIZE, offset) if query else []
        best = queries.best_chunks(conn, query, [row["id"] for row in hits])
        entity_companies = queries.matching_companies(conn, query) if query and page == 1 else []
        results = []
        for row in hits:
            chunk = best.get(row["id"])
            text = chunk["body"] if chunk else _snippet_text(row["body_text"], row["title"])
            results.append(
                _card(
                    row,
                    by_id[row["source_id"]],
                    settings,
                    title=highlight_title(row["title"], query),
                    excerpt_text="",
                    extra={
                        "snippet": highlight_snippet(text, query),
                        "heading": chunk["heading"] if chunk else "",
                        "chunk": chunk["ord"] if chunk else None,
                    },
                )
            )
        results = _decorate(conn, results)
        # 只靠片段就够判断相关性，摘要放在这里反而挤掉命中句。
        for card in results:
            card["summary"] = ""
    author_counts: dict[str, int] = {}
    source_counts = []
    facet_ids = set(facet_scope.source_ids)
    for source in sources:
        if source.id not in facet_ids:
            continue
        number = count_map.get(source.id, 0)
        source_counts.append({"id": source.id, "name": source.name, "count": number})
        author_counts[source.author] = author_counts.get(source.author, 0) + number
    _order, grouped = _author_groups(sources)
    entity_authors = [name for name in grouped if query and (name == query or (len(query) >= 2 and query in name))]
    base = {
        "q": query,
        "author": info["author"],
        "company": info["company"] or "",
        "from": info["from"],
        "to": info["to"],
    }
    return render_page(
        request,
        "search.html",
        {
            "nav": "search",
            "title": f"搜索 {query}" if query else "检索",
            "query": query,
            "sources": sources,
            "source_counts": source_counts,
            "author_counts": [{"name": name, "count": count} for name, count in author_counts.items()],
            "authors": list(grouped),
            "active_source": info["source"],
            "filters": info,
            "has_filter": has_filter,
            "company": company,
            "entity_companies": entity_companies,
            "entity_authors": entity_authors,
            "results": results,
            "total_all": total_all,
            "total": total,
            "page": page,
            "pages": pages,
            "base_query": _query_string(**base),
            "page_query": _query_string(**base, source=info["source"]),
            "ask_query": _query_string(
                q=query, author=info["author"], source=info["source"], company=info["company"] or "",
                **{"from": info["from"], "to": info["to"]},
            ),
            "sample_banner": banner,
        },
    )


@router.get("/companies")
def companies_page(request: Request):
    settings = request.app.state.settings
    show_all = request.query_params.get("all") == "1"
    with database(request) as conn:
        sources = active_sources(request, conn)
        ids = [source.id for source in sources]
        rows = queries.list_companies(conn, ids)
        banner = queries.sample_state(conn, ids)
    grouped: dict[str, list] = {}
    hidden = 0
    for row in rows:
        if not show_all and not row["mentions"]:
            hidden += 1
            continue
        aliases = list(row["aliases"] or [])
        if row["name_en"]:
            aliases.append(row["name_en"])
        if row["ticker"]:
            aliases.append(row["ticker"])
        grouped.setdefault(row["market"], []).append(
            {
                "id": row["id"],
                "name": row["name_zh"],
                "ticker": row["ticker"] or "未上市",
                "mentions": row["mentions"],
                "deep": row["deep"],
                "last": format_date(row["last_at"], settings.display_timezone) if row["last_at"] else "",
                "last_iso": format_iso(row["last_at"], settings.display_timezone) if row["last_at"] else "",
                "filter": " ".join([row["name_zh"], row["name_en"] or "", row["ticker"] or "", *aliases]),
            }
        )
    order = ["A", "HK", "US", "PRIVATE"]
    groups = [
        {"market": market, "label": market_label(market), "rows": grouped[market]}
        for market in order
        if market in grouped
    ]
    return render_page(
        request,
        "companies.html",
        {
            "nav": "companies",
            "title": "公司",
            "groups": groups,
            "show_all": show_all,
            "hidden": hidden,
            "sources": sources,
            "sample_banner": banner,
        },
    )


@router.get("/companies/{company_id}")
def company_page(request: Request, company_id: int):
    settings = request.app.state.settings
    depth = request.query_params.get("depth") or ""
    chosen = depth if depth in DEPTHS else None
    author = (request.query_params.get("author") or "").strip()
    with database(request) as conn:
        sources = active_sources(request, conn)
        all_ids = [source.id for source in sources]
        by_id = _source_map(sources)
        company = queries.get_company(conn, company_id)
        if company is None:
            raise HTTPException(status_code=404)
        _order, grouped = _author_groups(sources)
        if author and author not in grouped:
            raise HTTPException(status_code=404)
        ids = [item.id for item in grouped[author]] if author else all_ids
        per_source = {row["source_id"]: row["n"] for row in queries.company_author_counts(conn, company_id, all_ids)}
        depths = {row["depth"]: int(row["n"]) for row in queries.company_depth_counts(conn, company_id, ids)}
        months = list(queries.company_months(conn, company_id, ids))
        total = queries.count_company_articles(conn, company_id, ids, chosen)
        page, pages, offset = page_window(total, _page_arg(request), settings.page_size)
        articles = queries.company_articles(conn, company_id, ids, chosen, settings.page_size, offset)
        banner = queries.sample_state(conn, all_ids)
        matcher = CompanyMatcher([company])
        rows = _decorate(
            conn,
            [
                _card(
                    row,
                    by_id[row["source_id"]],
                    settings,
                    excerpt_text="",
                    extra={
                        "depth": row["depth"],
                        "depth_label": DEPTH_LABELS[row["depth"]],
                        "hits": row["hit_count"],
                        "snippet": mention_snippet(row["body_text"] or "", matcher.spans(row["body_text"] or "")),
                    },
                )
                for row in articles
            ],
        )
    for card in rows:
        # 公司页看的是「怎么提到它」，提及原句比通篇摘要有用；没有原句时才退回摘要。
        card["tags"] = [tag for tag in card["tags"] if tag["id"] != company_id]
        if card["snippet"]:
            card["summary"] = ""
    rows = order_cards(rows)
    author_counts = []
    for name, items in grouped.items():
        number = sum(per_source.get(item.id, 0) for item in items)
        if number:
            author_counts.append({"name": name, "count": number})
    author_counts.sort(key=lambda item: -item["count"])
    filter_query = _query_string(author=author, depth=chosen or "")
    return render_page(
        request,
        "company.html",
        {
            "nav": "companies",
            "title": company["name_zh"],
            "company": company,
            "market": market_label(company["market"]),
            "chips": depth_chips(depths),
            "chart": Markup(month_chart(months)),
            "active_depth": chosen or "",
            "active_author": author,
            "author_counts": author_counts,
            "author_total": sum(per_source.values()),
            "filter_query": filter_query,
            "days": group_days(rows),
            "day_scope": _query_string(company=company_id, author=author),
            "page": page,
            "pages": pages,
            "total": total,
            "sources": sources,
            "sample_banner": banner,
        },
    )


def _day_back(info: dict, company) -> str:
    if company:
        query = _query_string(author=info["author"])
        return f"/companies/{company['id']}" + (f"?{query}" if query else "")
    if info["author"]:
        query = _query_string(source=info["source"])
        base = "/authors/" + quote(info["author"])
        return base + (f"?{query}" if query else "")
    if info["source"]:
        return "/?source=" + quote(info["source"])
    return "/"


@router.get("/days/{day}")
def day_page(request: Request, day: str):
    """从时间轴点进某一天：先给汇总，再按作者列出这一天的文章。不在这里调用模型。"""
    try:
        found = date.fromisoformat(day)
    except ValueError as exc:
        raise HTTPException(status_code=404) from exc
    settings = request.app.state.settings
    params = {
        "author": request.query_params.get("author") or "",
        "source": request.query_params.get("source") or "",
        "company": request.query_params.get("company") or "",
        "from": day,
        "to": day,
    }
    with database(request) as conn:
        sources = active_sources(request, conn)
        scope, info = resolve_scope(params, sources)
        by_id = _source_map(sources)
        rows = queries.articles_on_day(conn, scope)
        cards = order_cards(
            _decorate(
                conn,
                [_card(row, by_id[row["source_id"]], settings, zone=FEED_ZONE) for row in rows],
            )
        )
        previous, following = queries.neighbor_days(conn, scope, found)
        saved = queries.day_answer(conn, found, scope.source_ids, scope.company_id)
        company = queries.get_company(conn, info["company"]) if info["company"] else None
        banner = queries.sample_state(conn, [source.id for source in sources])
    brief = compose_day_brief(cards)
    key = found.isoformat()
    scope_query = _query_string(author=info["author"], source=info["source"], company=info["company"] or "")

    def href(target) -> str:
        if target is None:
            return ""
        return f"/days/{target.isoformat()}" + (f"?{scope_query}" if scope_query else "")

    answer = None
    if saved:
        answer = {
            "id": saved["id"],
            "question": saved["question"],
            "answer": saved["answer"],
            "citations": saved["citations"] or [],
            "model": saved["model"],
        }
    if company:
        nav = "companies"
    elif info["author"]:
        nav = "authors"
    else:
        nav = "archive"
    return render_page(
        request,
        "day.html",
        {
            "nav": nav,
            "title": day_label(key),
            "label": day_label(key),
            "weekday": weekday_label(key),
            "year": found.year,
            "count": len(cards),
            "brief": brief,
            "answer": answer,
            "prev_href": href(previous),
            "next_href": href(following),
            "back_href": _day_back(info, company),
            "sources": sources,
            "active_source": info["source"],
            "company": company,
            "author_name": info["author"],
            "sample_banner": banner,
        },
    )


def _same_origin(request: Request) -> bool:
    """POST 只接受本站页面发起的请求。没有 Origin 头的（命令行、测试）放行。"""
    origin = request.headers.get("origin")
    if not origin:
        return True
    host = request.headers.get("host", "")
    return origin.split("://", 1)[-1] == host or origin == request.app.state.settings.public_base_url


def _ask_context(request: Request, sources, info: dict, conn) -> dict:
    company = queries.get_company(conn, info["company"]) if info["company"] else None
    article = None
    if info.get("article"):
        article = queries.get_article(conn, info["article"], [source.id for source in sources])
    preset = request.query_params.get("preset") or ""
    question = request.query_params.get("q") or PRESETS.get(preset, "")
    return {
        "question": question[:500],
        "filters": info,
        "company": company,
        "article": article,
        "authors": list(_author_groups(sources)[1]),
        "llm_ready": llm_configured(request.app.state.settings),
        "model": request.app.state.settings.llm_model,
        "history": queries.recent_answers(conn, 15),
    }


@router.get("/ask")
def ask_page(request: Request):
    with database(request) as conn:
        sources = active_sources(request, conn)
        _scope, info = resolve_scope(request.query_params, sources)
        context = _ask_context(request, sources, info, conn)
        banner = queries.sample_state(conn, [source.id for source in sources])
    return render_page(
        request,
        "ask.html",
        {"nav": "ask", "title": "问库", "sources": sources, "result": None, "sample_banner": banner, **context},
    )


@router.post("/ask")
async def ask_submit(request: Request):
    if not _same_origin(request):
        raise HTTPException(status_code=403, detail="跨站请求被拒绝")
    form = await request.form()
    question = str(form.get("q") or "").strip()[:500]
    params = {key: str(form.get(key) or "") for key in ("author", "source", "company", "from", "to", "article")}
    with database(request) as conn:
        sources = active_sources(request, conn)
        scope, info = resolve_scope(params, sources)
        if not question:
            return RedirectResponse("/ask?" + _query_string(**params), status_code=303)
        result = ask(conn, request.app.state.settings, question, scope, sources)
        if result["id"] is not None:
            conn.commit()
            return RedirectResponse(f"/ask/{result['id']}", status_code=303)
        context = _ask_context(request, sources, info, conn)
        context["question"] = question
        banner = queries.sample_state(conn, [source.id for source in sources])
    return render_page(
        request,
        "ask.html",
        {"nav": "ask", "title": "问库", "sources": sources, "result": result, "sample_banner": banner, **context},
    )


@router.get("/ask/{answer_id}")
def ask_answer(request: Request, answer_id: int):
    with database(request) as conn:
        sources = active_sources(request, conn)
        row = queries.get_answer(conn, answer_id)
        if row is None:
            raise HTTPException(status_code=404)
        scope = row["scope"] or {}
        info = {
            "author": "",
            "source": "",
            "company": scope.get("company_id"),
            "from": scope.get("date_from") or "",
            "to": scope.get("date_to") or "",
            "article": scope.get("article_id"),
        }
        context = _ask_context(request, sources, info, conn)
        context["question"] = row["question"]
        banner = queries.sample_state(conn, [source.id for source in sources])
    result = {
        "id": row["id"],
        "question": row["question"],
        "answer": row["answer"],
        "error": "",
        "model": row["model"],
        "citations": row["citations"],
        "created": format_date(row["created_at"], request.app.state.settings.display_timezone),
    }
    return render_page(
        request,
        "ask.html",
        {
            "nav": "ask",
            "title": row["question"][:40],
            "sources": sources,
            "result": result,
            "sample_banner": banner,
            **context,
        },
    )


@router.get("/media/{storage_path:path}")
def media(request: Request, storage_path: str):
    if not storage_path or storage_path.startswith(("/", "\\")):
        raise HTTPException(status_code=404)
    parts = Path(storage_path).parts
    if any(part in {"..", ""} for part in parts):
        raise HTTPException(status_code=404)
    root = (request.app.state.settings.data_dir / "images").resolve()
    target = (root / storage_path).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        raise HTTPException(status_code=404)
    media_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    return FileResponse(target, media_type=media_type)


@router.get("/health")
def health(request: Request):
    try:
        conn = connect(request.app.state.settings)
        try:
            conn.execute("SELECT 1 AS ok")
        finally:
            conn.close()
    except Exception:
        return Response(content='{"status":"down"}', status_code=503, media_type="application/json")
    return {"status": "ok"}


@router.get("/robots.txt")
def robots():
    return PlainTextResponse("User-agent: *\nDisallow: /\n")


@router.get("/favicon.svg")
def favicon():
    return Response(content=FAVICON, media_type="image/svg+xml")
