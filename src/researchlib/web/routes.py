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
from researchlib.snippets import highlight_snippet, highlight_title
from researchlib.sources import enabled_sources, load_sources, sync_sources
from researchlib.textutil import excerpt, split_frontmatter, strip_duplicate_title
from researchlib.web import queries
from researchlib.web.present import depth_chips, format_date, market_label, month_chart, page_window

router = APIRouter()
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))
FAVICON = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">
<rect width="32" height="32" rx="4" fill="#8f2d2d"/>
<text x="16" y="22" text-anchor="middle" font-size="16" fill="#fff" font-family="serif">研</text>
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


def _page_arg(request: Request) -> int:
    raw = request.query_params.get("page", "1")
    try:
        return max(1, int(raw))
    except ValueError:
        return 1


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
        total = queries.count_articles(conn, ids, chosen)
        page, pages, offset = page_window(total, _page_arg(request), settings.page_size)
        rows = queries.list_articles(conn, ids, chosen, settings.page_size, offset)
        banner = queries.sample_state(conn, ids)
    articles = [
        {
            "id": row["id"],
            "title": row["title"],
            "date": format_date(row["published_at"], settings.display_timezone),
            "source": by_id[row["source_id"]].name,
            "excerpt": excerpt(row["body_text"], title=row["title"]),
        }
        for row in rows
    ]
    return render_page(
        request,
        "home.html",
        {
            "nav": "archive",
            "title": "归档",
            "sources": sources,
            "active_source": chosen or "",
            "articles": articles,
            "page": page,
            "pages": pages,
            "total": total,
            "sample_banner": banner,
        },
    )


@router.get("/articles/{article_id}")
def article_page(request: Request, article_id: int):
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
    mapping = {row["original_ref"]: f"/media/{row['storage_path']}" for row in images}
    fmt = (article["raw_meta"] or {}).get("format")
    _frontmatter, body_md = split_frontmatter(article["body_md"])
    if fmt != "html":
        body_md = strip_duplicate_title(body_md, article["title"])
    body = render_body(body_md, mapping, fmt)
    return render_page(
        request,
        "article.html",
        {
            "nav": "archive",
            "title": article["title"],
            "article": article,
            "source": source,
            "date": format_date(article["published_at"], settings.display_timezone),
            "body_html": body,
            "siblings": siblings,
            "sources": sources,
            "sample_banner": banner,
        },
    )


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
    results = [
        {
            "id": row["id"],
            "title": highlight_title(row["title"], query),
            "plain_title": row["title"],
            "date": format_date(row["published_at"], settings.display_timezone),
            "source": by_id[row["source_id"]].name,
            "snippet": highlight_snippet(_snippet_text(row["body_text"], row["title"]), query),
        }
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
            "total": sum(count_map.values()) if not chosen else count_map.get(chosen, 0),
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
        {
            "id": row["id"],
            "title": row["title"],
            "date": format_date(row["published_at"], settings.display_timezone),
            "source": by_id[row["source_id"]].name,
            "depth": row["depth"],
            "depth_label": DEPTH_LABELS[row["depth"]],
            "hits": row["hit_count"],
        }
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
            "articles": rows,
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
