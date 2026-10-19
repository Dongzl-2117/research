"""页面路由。内容只来自已启用的白名单来源。"""

from __future__ import annotations

import mimetypes
from contextlib import contextmanager
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse, Response
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from researchlib.db import connect, ensure_search_session
from researchlib.mentions import DEPTH_LABELS, DEPTHS
from researchlib.render import render_body
from researchlib.translate.pair import bilingual_pairs, stamp_paragraph_ids
from researchlib.snippets import highlight_snippet, highlight_title
from researchlib.sources import enabled_sources, load_sources, sync_sources
from researchlib.textutil import excerpt, split_frontmatter, strip_duplicate_title
from researchlib.web import queries
from researchlib.web.present import (
    FEED_ZONE,
    day_key,
    depth_chips,
    format_clock,
    format_date,
    format_iso,
    group_days,
    market_label,
    month_chart,
    page_window,
    plan_feed_page,
    relative_label,
)

router = APIRouter()
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))
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
    when = row["published_at"]
    zone = zone or settings.display_timezone
    if excerpt_text is None and "body_text" in row:
        excerpt_text = excerpt(row["body_text"], title=row["title"], limit=180)
    card = {
        "id": row["id"],
        "title": row["title"] if title is None else title,
        "date": format_date(when, zone),
        "time": format_clock(when, zone),
        "iso": format_iso(when, zone),
        "day": day_key(when, zone),
        "source": source.name,
        "source_id": source.id,
        "author": source.author,
        "excerpt": excerpt_text or "",
    }
    if extra:
        card.update(extra)
    return card


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
        counts, plan, rows = _load_feed(conn, scoped, settings, _page_arg(request))
        banner = queries.sample_state(conn, ids)
    articles = [_card(row, by_id[row["source_id"]], settings, zone=FEED_ZONE) for row in rows]
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
            "days": group_days(articles),
            "page": plan["page"],
            "pages": plan["pages"],
            "total": counts["articles"],
            "dated_days": counts["days"],
            "undated": counts["undated"],
            "sample_banner": banner,
        },
    )


@router.get("/authors")
def authors_page(request: Request):
    with database(request) as conn:
        sources = active_sources(request, conn)
        ids = [source.id for source in sources]
        counts = queries.source_article_counts(conn, ids)
        banner = queries.sample_state(conn, ids)
    _order, grouped = _author_groups(sources)
    authors = [
        {
            "name": name,
            "articles": sum(counts.get(item.id, 0) for item in items),
            "sources": [item.name for item in items],
        }
        for name, items in grouped.items()
    ]
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
        counts, plan, rows = _load_feed(conn, scoped, settings, _page_arg(request))
        banner = queries.sample_state(conn, [source.id for source in sources])
    articles = [_card(row, by_id[row["source_id"]], settings, zone=FEED_ZONE) for row in rows]
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
            "days": group_days(articles),
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

    original_url = (article["raw_meta"] or {}).get("url") or ""
    return render_page(
        request,
        "article.html",
        {
            "nav": "archive",
            "title": article["title"],
            "article": article,
            "source": source,
            "date": format_date(article["published_at"], settings.display_timezone),
            "time": format_clock(article["published_at"], settings.display_timezone),
            "iso": format_iso(article["published_at"], settings.display_timezone),
            "relative": relative_label(article["published_at"], settings.display_timezone),
            "body_html": body_html,
            "body_language": body_language,
            "has_translation": has_translation,
            "translation_complete": translation_complete,
            "pairs": pairs or [],
            "title_zh": (translation or {}).get("title_zh") or "",
            "original_url": original_url,
            "siblings": siblings,
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
    query = request.query_params.get("q", "").strip()[:80]
    source_arg = request.query_params.get("source") or ""
    with database(request) as conn:
        sources = active_sources(request, conn)
        ids = [source.id for source in sources]
        by_id = _source_map(sources)
        chosen = source_arg if source_arg in by_id else None
        banner = queries.sample_state(conn, ids)
        counts = queries.search_counts(conn, query, ids) if query else []
        hits = queries.search_articles(conn, query, ids, chosen) if query else []
    count_map = {row["source_id"]: int(row["n"]) for row in counts}
    author_counts: dict[str, int] = {}
    source_counts = []
    for source in sources:
        number = count_map.get(source.id, 0)
        source_counts.append({"id": source.id, "name": source.name, "count": number})
        author_counts[source.author] = author_counts.get(source.author, 0) + number
    total_all = sum(count_map.values())
    results = [
        _card(
            row,
            by_id[row["source_id"]],
            settings,
            title=highlight_title(row["title"], query),
            excerpt_text="",
            extra={"snippet": highlight_snippet(_snippet_text(row["body_text"], row["title"]), query)},
        )
        for row in hits
    ]
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
            "active_source": chosen or "",
            "results": results,
            "total_all": total_all,
            "total": count_map.get(chosen, 0) if chosen else total_all,
            "sample_banner": banner,
        },
    )


@router.get("/companies")
def companies_page(request: Request):
    with database(request) as conn:
        sources = active_sources(request, conn)
        ids = [source.id for source in sources]
        rows = queries.list_companies(conn, ids)
        banner = queries.sample_state(conn, ids)
    grouped: dict[str, list] = {}
    for row in rows:
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
            "sources": sources,
            "sample_banner": banner,
        },
    )


@router.get("/companies/{company_id}")
def company_page(request: Request, company_id: int):
    settings = request.app.state.settings
    depth = request.query_params.get("depth") or ""
    chosen = depth if depth in DEPTHS else None
    with database(request) as conn:
        sources = active_sources(request, conn)
        ids = [source.id for source in sources]
        by_id = _source_map(sources)
        company = queries.get_company(conn, company_id)
        if company is None:
            raise HTTPException(status_code=404)
        depths = {row["depth"]: int(row["n"]) for row in queries.company_depth_counts(conn, company_id, ids)}
        months = list(queries.company_months(conn, company_id, ids))
        total = queries.count_company_articles(conn, company_id, ids, chosen)
        page, pages, offset = page_window(total, _page_arg(request), settings.page_size)
        articles = queries.company_articles(conn, company_id, ids, chosen, settings.page_size, offset)
        banner = queries.sample_state(conn, ids)
    rows = [
        _card(
            row,
            by_id[row["source_id"]],
            settings,
            extra={
                "depth": row["depth"],
                "depth_label": DEPTH_LABELS[row["depth"]],
                "hits": row["hit_count"],
            },
        )
        for row in articles
    ]
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
            "days": group_days(rows),
            "page": page,
            "pages": pages,
            "total": total,
            "sources": sources,
            "sample_banner": banner,
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
