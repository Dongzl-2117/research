"""页面路由。内容只来自已启用的白名单来源。"""

from __future__ import annotations

import mimetypes
import os
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from urllib.parse import quote, urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from researchlib import embeddings
from researchlib import revisions as revision_log
from researchlib import theses as thesis_map
from researchlib.ask import PRESETS, retry, start
from researchlib.boilerplate import clean_article
from researchlib.citecheck import check_answer
from researchlib.db import connect, ensure_search_session
from researchlib.digest import digest_due, schedule_digests
from researchlib.digest import needs_rewrite as digest_needs_rewrite
from researchlib.doctree import arrange_tree, filter_library_tree, load_tree_rows, mark_open
from researchlib.duplicates import separate as separate_duplicate
from researchlib.duplicates import similarity as duplicate_similarity
from researchlib.empty_answer import is_suppressed
from researchlib.feedback import add_missing, toggle_irrelevant
from researchlib.feedback import marks as feedback_marks
from researchlib.health import notices as health_notices
from researchlib.llm import active_label, active_model_id, llm_configured
from researchlib.mdlinks import finish_html, load_md_index, prepare_body_html
from researchlib.mentions import DEPTH_LABELS, DEPTHS, CompanyMatcher, company_on_card, load_company_rows
from researchlib.ocr import refresh_article_ocr
from researchlib.portfolio import Portfolio, load_portfolio
from researchlib.render import flow_ocr_quote_body, inline_image_ocr, sanitize_html
from researchlib.retrieval import full_match_counts, search_articles
from researchlib.snippets import answer_html, highlight_full, highlight_snippet, highlight_title, mention_snippet
from researchlib.sources import enabled_sources, load_sources, sync_sources, timeline_sources
from researchlib.textutil import argumentative_excerpt
from researchlib.translate.pair import bilingual_pairs
from researchlib.views import FILTERS as VIEW_FILTERS
from researchlib.views import count_pending as count_view_pending
from researchlib.views import filter_timeline, quote_repeats, same_period
from researchlib.views import queue_for_company as queue_views
from researchlib.views import timeline as view_timeline
from researchlib.web import queries
from researchlib.web.daybrief import compose_day_brief
from researchlib.web.present import (
    FEED_ZONE,
    chart_ticks,
    company_chart,
    current_month,
    day_key,
    day_label,
    depth_chips,
    format_clock,
    format_date,
    format_iso,
    group_days,
    market_label,
    month_series,
    order_cards,
    page_window,
    plan_feed_page,
    recent_blurb,
    relative_label,
    resolve_feed_instant,
    stale_sources,
    weekday_label,
)
from researchlib.web.quoting import mark_quoted
from researchlib.web.scoping import parse_day, resolve_scope
from researchlib.wechat_dialogue import present_stored_ocr
from researchlib.zsxq_title import repairs_source, title_needs_repair, zsxq_display_title

router = APIRouter()
SEARCH_PAGE_SIZE = 20
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))
TEMPLATES.env.filters["answer_html"] = answer_html
TEMPLATES.env.filters["highlight_full"] = highlight_full
TEMPLATES.env.filters["highlight_snippet"] = lambda text, terms, radius=110: highlight_snippet(
    text, "", radius=radius, terms=list(terms or [])
)
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
    # paused 的来源（如段永平）整源先不展示：导航、作者、筛选都不出现。
    show_paused = os.environ.get("RESEARCH_SHOW_PAUSED", "").strip() == "1"
    return [s for s in enabled_sources(request.app.state.sources) if show_paused or not s.paused]


def active_portfolio(request: Request) -> Portfolio:
    """持仓名单按文件修改时间缓存。JSON 的 portfolio 字段读它，改完再请求就生效。"""
    path = request.app.state.settings.portfolio_path
    if path is None or not path.exists():
        return Portfolio()
    mtime = path.stat().st_mtime
    if getattr(request.app.state, "portfolio_mtime", None) != mtime:
        request.app.state.portfolio = load_portfolio(path)
        request.app.state.portfolio_mtime = mtime
    return request.app.state.portfolio


def _latest_item(row, by_id, settings) -> dict | None:
    if not row:
        return None
    source = by_id.get(row["source_id"])
    return {
        "id": row["id"],
        "title": row["title"],
        "date": format_date(row["published_at"], settings.display_timezone),
        "relative": relative_label(row["published_at"], settings.display_timezone),
        "author": source.author if source else "",
    }


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


def _source_href(source) -> str:
    if source.timeline:
        return "/?source=" + quote(source.id)
    return f"/authors/{quote(source.author)}?source={quote(source.id)}"


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
        excerpt_text = argumentative_excerpt(row["body_text"], title=row["title"], limit=180)
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
        "source_href": _source_href(source),
        "author": source.author,
        "timeline": source.timeline,
        # 来源名和作者同名时（例如「乘风破浪」）只显示一次。
        "show_author": source.author != source.name,
        "guest": False,
        "excerpt": excerpt_text or "",
        "summary": "",
        "points": [],
        "tags": [],
        "body_text": row.get("body_text") or "",
    }
    if extra:
        card.update(extra)
    if row.get("poster_role") == "guest":
        card["author"] = (row.get("poster_name") or "").strip() or "星球读者"
        card["guest"] = True
        card["show_author"] = True
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


def _digest_points(digest, limit: int = 2) -> list[str]:
    raw = digest["points"] or []
    if isinstance(raw, str):
        return []
    return [str(item).strip() for item in raw if str(item).strip()][:limit]


def _repair_zsxq_titles(conn, cards: list[dict]) -> None:
    """方伟星球卡片：空标题和以省略号结尾的标题换成一句完整的话。不改库里的原文。"""
    needy = [
        card
        for card in cards
        if repairs_source(card.get("source_id") or "") and title_needs_repair(str(card.get("plain_title") or ""))
    ]
    if not needy:
        return
    materials = queries.title_materials(conn, [card["id"] for card in needy])
    for card in needy:
        extra = materials.get(card["id"]) or {}
        repaired = zsxq_display_title(
            str(card.get("plain_title") or ""),
            body=card.get("body_text") or extra.get("body_text") or "",
            summary=card.get("summary") or "",
            ocr=present_stored_ocr(extra.get("ocr_text") or ""),
            when=card.get("date") or "",
        )
        card["plain_title"] = repaired
        card["title"] = repaired


def _decorate(conn, cards: list[dict], settings) -> list[dict]:
    """给卡片挂上公司标签和 LLM 摘要。缺摘要的排进后台，不在这次请求里等模型。"""
    ids = [card["id"] for card in cards]
    tags = queries.article_tags(conn, ids, per_article=12)
    digests = queries.digests_for(conn, ids)
    pending: list[tuple[int, str]] = []
    for card in cards:
        card["tags"] = _visible_tags(card, tags.get(card["id"], []))
        digest = digests.get(card["id"])
        if digest and not card.get("summary"):
            card["summary"] = digest["summary"]
            card["points"] = _digest_points(digest)
            if digest_needs_rewrite(digest):
                # 旧版提示词写的、或当时只读了长文开头的摘要先照常显示，后台重写。
                pending.append((card["id"], card.get("author") or ""))
        elif not card.get("summary"):
            pending.append((card["id"], card.get("author") or ""))
    _repair_zsxq_titles(conn, cards)
    for card in cards:
        card.pop("body_text", None)
    if pending:
        schedule_digests(settings, pending)
    return cards


def _feed_days(conn, rows, by_id, settings):
    cards = [_card(row, by_id[row["source_id"]], settings, zone=FEED_ZONE) for row in rows]
    return group_days(order_cards(_decorate(conn, cards, settings)))


def _query_string(**params) -> str:
    return urlencode({key: value for key, value in params.items() if value not in (None, "", 0)})


def _page_arg(request: Request) -> int:
    raw = request.query_params.get("page", "1")
    try:
        return max(1, int(raw))
    except ValueError:
        return 1


def _load_feed(
    conn, source_ids: list[str], settings, page: int, *, include_off_timeline: bool = True, owner_only: bool = False
):
    counts = queries.feed_counts(conn, source_ids, include_off_timeline=include_off_timeline, owner_only=owner_only)
    plan = plan_feed_page(
        counts["days"],
        counts["undated"],
        page,
        settings.days_per_page,
        settings.page_size,
    )
    if plan["mode"] == "undated":
        rows = queries.list_undated_articles(
            conn,
            source_ids,
            plan["limit"],
            plan["offset"],
            include_off_timeline=include_off_timeline,
            owner_only=owner_only,
        )
    else:
        rows = queries.list_day_page(
            conn,
            source_ids,
            plan["limit"],
            plan["offset"],
            include_off_timeline=include_off_timeline,
            owner_only=owner_only,
        )
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
        feed = timeline_sources(sources)
        ids = [source.id for source in feed]
        by_id = _source_map(feed)
        chosen = source_arg if source_arg in by_id else None
        if source_arg and source_arg not in by_id and source_arg != "all":
            raise HTTPException(status_code=404)
        scoped = [chosen] if chosen else ids
        jump = parse_day(request.query_params.get("date"))
        if jump is not None:
            return _jump_to_day(conn, scoped, jump, settings, "/", source=chosen or "")
        # 没日期、标了 off_timeline 的条目不进首页按天。作者页仍要。
        counts, plan, rows = _load_feed(
            conn, scoped, settings, _page_arg(request), include_off_timeline=False
        )
        banner = queries.sample_state(conn, ids)
        days = _feed_days(conn, rows, by_id, settings)
        stale = []
        notices: list[str] = []
        if plan["page"] == 1:
            # 备份、导入、Grok 这些出了问题才有；正常时是空的。
            notices = health_notices(settings.data_dir, settings.display_timezone)
        if plan["page"] == 1 and settings.stale_banner_enabled:
            watched = [by_id[chosen]] if chosen else feed
            stale = stale_sources(
                watched,
                queries.recent_publish_times(conn, [source.id for source in watched]),
                queries.last_imports(conn, [source.id for source in watched]),
                settings.display_timezone,
            )
        holdings = []
        # 首页顶部按最近一条观点排。展开用库里已有的导读，不在这次请求里调模型。
        if not chosen and plan["page"] == 1:
            all_ids = [source.id for source in sources]
            ranked = queries.recent_viewed_companies(conn, all_ids, limit=8)
            recent = queries.holdings_recent(conn, [row["id"] for row in ranked], all_ids)
            all_by_id = _source_map(sources)
            for row in ranked:
                info = recent.get(row["id"], {})
                latest = _latest_item(info.get("latest"), all_by_id, settings)
                view = _latest_view(conn, row["id"], all_by_id, settings, latest)
                holdings.append(
                    {
                        "id": row["id"],
                        "name": row["name_zh"],
                        "recent": info.get("recent", 0),
                        "latest": latest,
                        "view": view,
                        "summary": "",
                    }
                )
            article_ids = [item["view"]["article_id"] for item in holdings if item.get("view")]
            digests = queries.digests_for(conn, article_ids)
            for item in holdings:
                view = item.get("view")
                digest = digests.get(view["article_id"]) if view else None
                # 首页只放已有导读。没有导读就留一行态度和篇数，不把判断原文铺开。
                item["summary"] = str((digest or {}).get("summary") or "").strip()
    if chosen:
        heading = by_id[chosen].name
        empty = "这个来源还没有文章。"
    else:
        heading = "按天"
        empty = "还没有文章。"
    return render_page(
        request,
        "home.html",
        {
            "nav": "archive",
            "title": heading,
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
            "holdings": holdings,
            "stale": stale,
            "notices": notices,
        },
    )


def _latest_view(conn, company_id: int, sources_by_id: dict, settings, latest: dict | None) -> dict | None:
    """首页最近观点：最近一条判断和态度。比最近一篇深入文章旧的不用，那篇的观点还在整理。"""
    items = view_timeline(conn, company_id, sources_by_id, settings)
    if not items:
        return None
    view = items[0]
    if latest is not None and latest.get("date") and view["date"] < latest["date"]:
        return None
    return {
        **view,
        "relative": relative_label(view["published_at"], settings.display_timezone) if view["published_at"] else "",
    }


def _jump_to_day(conn, source_ids: list[str], day, settings, path: str, *, owner_only: bool = False, **params):
    """把「跳到某天」换成那一天所在的页，带上锚点。没有那天就落到更早的最近一天。"""
    found = queries.nearest_day(conn, source_ids, day, owner_only=owner_only)
    if found is None:
        query = _query_string(**params)
        return RedirectResponse(path + (f"?{query}" if query else ""), status_code=303)
    page = queries.day_offset(conn, source_ids, found, owner_only=owner_only) // settings.days_per_page + 1
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
        # 文档库（飞书这类没有发布时间的）不排时间线：直接进按原目录嵌套的文档树。
        # 作者同时有公众号、星球时，时间线只排这些有日期的来源，文档库从顶上的目录入口进。
        libraries = [by_id[item] for item in scoped if not by_id[item].timeline and by_id[item].kind == "feishu"]
        dated = [item for item in scoped if by_id[item].timeline]
        built = [item for item in libraries if _has_tree(conn, item.id)]
        if built and not _has_articles(conn, dated):
            return RedirectResponse(f"/sources/{built[0].id}/tree", status_code=302)
        scoped = dated or scoped
        jump = parse_day(request.query_params.get("date"))
        if jump is not None:
            return _jump_to_day(
                conn, scoped, jump, settings, f"/authors/{author}", owner_only=True, source=chosen or ""
            )
        counts, plan, rows = _load_feed(conn, scoped, settings, _page_arg(request), owner_only=True)
        stats = queries.source_stats(conn, scoped)
        companies = queries.top_companies(conn, scoped, 12)
        banner = queries.sample_state(conn, [source.id for source in sources])
        days = _feed_days(conn, rows, by_id, settings)
    firsts = [row["first_at"] for row in stats.values() if row["first_at"]]
    lasts = [row["last_at"] for row in stats.values() if row["last_at"]]
    shown_sources = [by_id[item_id] for item_id in scoped]
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
            "link_days": any(item.timeline for item in shown_sources),
        },
    )



def _has_articles(conn, source_ids: list[str]) -> bool:
    if not source_ids:
        return False
    row = conn.execute(
        "SELECT 1 FROM articles WHERE source_id = ANY(%s) AND duplicate_of IS NULL LIMIT 1", (source_ids,)
    ).fetchone()
    return row is not None


def _has_tree(conn, source_id: str) -> bool:
    """目录树建过（research-doc-tree）才跳过去；没建过还是列文章。"""
    row = conn.execute(
        "SELECT 1 FROM doc_tree t JOIN articles a ON a.id = t.article_id WHERE a.source_id = %s LIMIT 1",
        (source_id,),
    ).fetchone()
    return row is not None


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
        if article["duplicate_of"] is None:
            # 这一篇的图片文字若还是原始识别行，先整理再决定要不要重写摘要。不扫别的文章。
            new_hash = refresh_article_ocr(conn, article_id)
            if new_hash != (article.get("ocr_hash") or ""):
                article = {**article, "ocr_hash": new_hash}
        digest = queries.digests_for(conn, [article_id]).get(article_id)
        empty_answer = is_suppressed(article["raw_meta"])
        meta = article["raw_meta"] or {}
        suppressed_reason = str(meta.get("hidden") or meta.get("suppressed") or "")
        # 短文的图片文字变了也重写摘要。空回答不排进后台。
        if digest_due(article, digest) and not empty_answer:
            schedule_digests(settings, [(article_id, source.author)])
        ocr_texts = queries.image_ocr_texts(conn, article_id)
        quoted = None
        if chunk_arg is not None and chunk_arg.isdigit():
            wanted = int(chunk_arg)
            quoted = next((row for row in queries.article_chunks(conn, article_id) if row["ord"] == wanted), None)
            if quoted is not None and (quoted.get("lang") or "") == "ocr":
                cleaned = flow_ocr_quote_body(present_stored_ocr(quoted["body"]))
                if cleaned.strip():
                    quoted = {**quoted, "body": cleaned}
        banner = queries.sample_state(conn, ids)
        translation = queries.get_translation(conn, article_id)
        elsewhere = [
            {"id": row["id"], "source": by_id[row["source_id"]].name}
            for row in queries.same_article_elsewhere(conn, article_id, article["duplicate_of"], ids)
            if row["source_id"] in by_id
        ]
        revised = revision_log.label(revision_log.summary(conn, [article_id]).get(article_id))
        others = same_period(conn, article_id, None, by_id, settings) if article["duplicate_of"] is None else []
        parent = str((article["raw_meta"] or {}).get("parent") or "")
        zh_blob = (translation or {}).get("body_html") or ""
        link_index = None
        if ".md" in (article["body_md"] or "").lower() or ".md" in zh_blob.lower():
            link_index = load_md_index(conn, article["source_id"])
        # 正文显示前去掉归档头、引流尾巴、二维码和文末招募。库里的 body_md 不改。HTML 同样洗。
        shown_md = clean_article(conn, article_id, article["source_id"], article["body_md"])
        display_title = article["title"]
        if repairs_source(source.id) and title_needs_repair(display_title or ""):
            material = queries.title_materials(conn, [article_id]).get(article_id) or {}
            display_title = zsxq_display_title(
                display_title or "",
                body=article.get("body_text") or material.get("body_text") or "",
                summary=(digest or {}).get("summary") or "",
                ocr=present_stored_ocr(material.get("ocr_text") or ""),
                when=format_date(article["published_at"], settings.display_timezone),
            )
    mapping = {row["original_ref"]: f"/media/{row['storage_path']}" for row in images}
    fmt = (article["raw_meta"] or {}).get("format")
    # render_body 之后、stamp_paragraph_ids 之前改相对 .md 链接。prepare/finish 里就是这个顺序。
    original_html = inline_image_ocr(
        prepare_body_html(shown_md, article["title"], mapping, fmt, link_index, parent),
        ocr_texts,
    )

    has_translation = bool(translation and translation.get("body_html"))
    # 原文已改、译文 hash 对不上时仍可展示旧译文，但标 incomplete
    translation_complete = bool(
        translation
        and translation.get("complete")
        and translation.get("content_hash") == article.get("content_hash")
    )
    # 译文是模型写的 HTML，和原文一样过一遍白名单。
    zh_html = finish_html(sanitize_html(translation["body_html"]), link_index, parent) if has_translation else None
    if zh_html:
        zh_html = inline_image_ocr(zh_html, ocr_texts)
    # 引用的那一块：在正文里找到它覆盖的段落，高亮并滚过去；找不到才用顶上的框。
    quoted_lang = (quoted or {}).get("lang") or ""
    quoted_marked = False
    if quoted is not None and quoted_lang == "":
        original_html, quoted_marked = mark_quoted(original_html, quoted["body"])
    elif quoted is not None and quoted_lang == "zh" and zh_html:
        zh_html, quoted_marked = mark_quoted(zh_html, quoted["body"])
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
    # 高亮的段落这一页上看得见才算：只看中文时，原文里标的那段不显示。
    if quoted_marked and body_language != "pairs":
        quoted_marked = (body_language == "zh") == (quoted_lang == "zh")

    meta = article["raw_meta"] or {}
    shown, known = resolve_feed_instant(
        article["published_at"],
        clock_hint=meta.get("create_time"),
        date_source=meta.get("date_source") or "",
        time_known=meta.get("time_known"),
        zone=settings.display_timezone,
    )
    return render_page(
        request,
        "article.html",
        {
            "nav": "archive",
            "title": display_title,
            "display_title": display_title,
            "article": article,
            "source": source,
            "guest": (article["raw_meta"] or {}).get("poster_role") == "guest",
            "poster_name": (article["raw_meta"] or {}).get("poster_name") or "",
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
            "quoted": quoted,
            "quoted_marked": quoted_marked,
            # 点进文章先看总结：有摘要就放在正文前面。
            "digest_summary": (digest or {}).get("summary") or "",
            "digest_points": _digest_points(digest, limit=6) if digest else [],
            "empty_answer": empty_answer,
            "suppressed_reason": suppressed_reason,
            "elsewhere": elsewhere,
            "revised": revised,
            "same_period": others,
            "tree_back": f"/sources/{source.id}/tree/{article['id']}" if source.kind == "feishu" else "",
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


def _tree_body(conn, article, source_id: str):
    images = queries.article_images(conn, article["id"])
    mapping = {row["original_ref"]: f"/media/{row['storage_path']}" for row in images}
    fmt = (article["raw_meta"] or {}).get("format")
    parent = str((article["raw_meta"] or {}).get("parent") or "")
    link_index = load_md_index(conn, source_id) if ".md" in (article["body_md"] or "").lower() else None
    shown_md = clean_article(conn, article["id"], source_id, article["body_md"])
    html = prepare_body_html(shown_md, article["title"], mapping, fmt, link_index, parent)
    return Markup(inline_image_ocr(html, queries.image_ocr_texts(conn, article["id"])))


def _source_tree(request: Request, source_id: str, article_id: int | None):
    """飞书目录：左边可折叠的树，右边正文。树要先跑 research-doc-tree。"""
    with database(request) as conn:
        sources = active_sources(request, conn)
        source = _source_map(sources).get(source_id)
        if source is None:
            raise HTTPException(status_code=404)
        rows = load_tree_rows(conn, source_id)
        parents = {row["parent_id"] for row in rows if row["parent_id"] is not None}
        root, orphans = arrange_tree(rows)
        if source.kind == "feishu":
            root, orphans = filter_library_tree(root, orphans)
        current = article_id
        if current is None and root is not None:
            current = root["id"]
        article = None
        body_html = None
        if current is not None:
            article = queries.get_article(conn, current, [source_id])
            if article is None:
                raise HTTPException(status_code=404)
            body_html = _tree_body(conn, article, source_id)
        if root is not None:
            mark_open(root, current)
        for node in orphans:
            mark_open(node, current)
    return render_page(
        request,
        "tree.html",
        {
            "nav": "tree",
            "title": article["title"] if article else source.name,
            "source": source,
            "sources": sources,
            "active_source": source.id,
            "root": root,
            "orphans": orphans,
            "article": article,
            "body_html": body_html,
            "has_children": current in parents,
        },
    )


@router.get("/sources/{source_id}/tree")
def source_tree(request: Request, source_id: str):
    return _source_tree(request, source_id, None)


@router.get("/sources/{source_id}/tree/{article_id}")
def source_tree_article(request: Request, source_id: str, article_id: int):
    return _source_tree(request, source_id, article_id)


@router.get("/articles/{article_id}/revisions")
def article_revisions(request: Request, article_id: int):
    """这篇的各个版本：改稿前的旧版，和后来导入时拦下的删稿、截短版，每版和它后面那版比。"""
    settings = request.app.state.settings
    with database(request) as conn:
        sources = active_sources(request, conn)
        ids = [source.id for source in sources]
        article = queries.get_article(conn, article_id, ids)
        if article is None:
            raise HTTPException(status_code=404)
        rows = revision_log.versions(conn, article)
        banner = queries.sample_state(conn, ids)
    current = rows[0]
    previous = [row for row in rows[1:] if row["kind"] == "previous"]
    held = [row for row in rows[1:] if row["kind"] == "held"]
    zone = settings.display_timezone
    entries = []
    for row in held:
        reason = revision_log.REASONS.get(row["reason"], "后来的版本")
        entries.append(
            {
                "label": f"{reason}（{format_date(row['created_at'], zone)} 导入时拦下，没有替换）",
                "blocks": revision_log.diff_blocks(current["body_text"], row["body_text"]),
                "title_change": (current["title"], row["title"]) if row["title"] != current["title"] else None,
            }
        )
    newer = current
    for row in previous:
        entries.append(
            {
                "label": f"{format_date(row['created_at'], zone)} 被替换前的旧版 → 替换后",
                "blocks": revision_log.diff_blocks(row["body_text"], newer["body_text"]),
                "title_change": (row["title"], newer["title"]) if row["title"] != newer["title"] else None,
            }
        )
        newer = row
    return render_page(
        request,
        "revisions.html",
        {
            "nav": "archive",
            "title": f"改动：{article['title']}",
            "article": article,
            "entries": entries,
            "sources": sources,
            "sample_banner": banner,
        },
    )


DUPLICATES_PAGE_SIZE = 30


@router.get("/duplicates")
def duplicates_page(request: Request):
    """认出来的重复：主版本和指向它的几份，重合比例，认错了点「不是同一篇」。"""
    settings = request.app.state.settings
    raw_article = request.query_params.get("article") or ""
    only = int(raw_article) if raw_article.isdigit() else None
    with database(request) as conn:
        sources = active_sources(request, conn)
        ids = [source.id for source in sources]
        by_id = _source_map(sources)
        total = queries.count_duplicate_groups(conn, ids, only)
        page, pages, offset = page_window(total, _page_arg(request), DUPLICATES_PAGE_SIZE)
        groups = queries.duplicate_groups(conn, ids, DUPLICATES_PAGE_SIZE, offset, only)
        banner = queries.sample_state(conn, ids)
    zone = settings.display_timezone
    shown = []
    for group in groups:
        canonical = group["canonical"]
        items = []
        for row in group["duplicates"]:
            overlap = duplicate_similarity(canonical["body_text"], row["body_text"])
            items.append(
                {
                    "id": row["id"],
                    "title": row["title"],
                    "source": by_id[row["source_id"]].name if row["source_id"] in by_id else row["source_id"],
                    "date": format_date(row["published_at"], zone),
                    "marked": format_date(row["duplicate_marked_at"], zone) if row["duplicate_marked_at"] else "",
                    "overlap": round(overlap["containment"] * 100),
                    "length": round(overlap["length_ratio"] * 100),
                }
            )
        shown.append(
            {
                "id": canonical["id"],
                "title": canonical["title"],
                "source": by_id[canonical["source_id"]].name if canonical["source_id"] in by_id else "",
                "date": format_date(canonical["published_at"], zone),
                "items": items,
            }
        )
    return render_page(
        request,
        "duplicates.html",
        {
            "nav": "archive",
            "title": "重复文章",
            "groups": shown,
            "only": only,
            "page": page,
            "pages": pages,
            "total": total,
            "sources": sources,
            "sample_banner": banner,
        },
    )


@router.post("/duplicates/{article_id}/separate")
def duplicates_separate(request: Request, article_id: int):
    """认错了：这篇不是同一篇。以后不再参与认重复，段落块和提及恢复。"""
    if not _same_origin(request):
        raise HTTPException(status_code=403)
    back = request.query_params.get("back") or ""
    with database(request) as conn:
        sources = active_sources(request, conn)
        if queries.get_article(conn, article_id, [source.id for source in sources]) is None:
            raise HTTPException(status_code=404)
        separate_duplicate(conn, article_id, CompanyMatcher(load_company_rows(conn)))
    target = f"/duplicates?article={back}" if back.isdigit() else "/duplicates"
    return RedirectResponse(target, status_code=303)



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
        page = _page_arg(request)
        found = None
        count_map: dict[str, int] = {}
        semantic_note = ""
        if query:
            found = search_articles(
                conn, query, scope, sources, limit=SEARCH_PAGE_SIZE, offset=(page - 1) * SEARCH_PAGE_SIZE
            )
            if found.query and found.query.terms:
                count_map = full_match_counts(conn, found.query, facet_scope)
            else:
                count_map = dict(found.counts)
            # 有具体的词才走语义检索；没用上（没配、没算向量、服务连不上）或向量没算完时说一句。
            if found.query and found.query.content_terms:
                semantic_note = embeddings.status_note(conn)
        entity_companies = queries.matching_companies(conn, query) if query and page == 1 else []
        results = []
        terms = found.terms if found else []
        for hit in found.hits if found else []:
            best = hit.passages[0] if hit.passages else None
            row = {"id": hit.article_id, "title": hit.title, "published_at": hit.published_at}
            results.append(
                _card(
                    row,
                    by_id[hit.source_id],
                    settings,
                    title=highlight_title(hit.title, query, terms=terms),
                    extra={
                        "snippets": [highlight_snippet(p.body, query, terms=terms) for p in hit.passages[:2]],
                        "heading": best.heading if best else "",
                        "chunk": best.ord if best else None,
                        "anchor": best.anchor if best else "",
                        "partial": not hit.full,
                        "semantic": hit.semantic or bool(best and best.semantic_hit),
                    },
                )
            )
        results = _decorate(conn, results, settings)
    total_all = sum(count_map.values())
    total = found.total if found else 0
    available = found.available if found else 0
    page, pages, _offset = page_window(available, page, SEARCH_PAGE_SIZE)
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
    first_partial = next((index for index, item in enumerate(results) if item.get("partial")), None)
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
            "first_partial": first_partial,
            "partial_total": found.partial if found else 0,
            "semantic_note": semantic_note,
            "search_terms": terms,
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


COMPANY_SORTS = {"deep": "深入篇数", "recent": "最近提及", "mentions": "全部提及"}


@router.get("/companies")
def companies_page(request: Request):
    settings = request.app.state.settings
    show_all = request.query_params.get("all") == "1"
    sort = request.query_params.get("sort") or "deep"
    if sort not in COMPANY_SORTS:
        sort = "deep"
    market = request.query_params.get("market") or ""
    with database(request) as conn:
        sources = active_sources(request, conn)
        ids = [source.id for source in sources]
        rows = queries.list_companies(conn, ids)
        banner = queries.sample_state(conn, ids)
    others = []
    hidden = 0
    markets: dict[str, int] = {}
    for row in rows:
        if not show_all and not row["mentions"]:
            hidden += 1
            continue
        markets[row["market"]] = markets.get(row["market"], 0) + 1
        if market and row["market"] != market:
            continue
        aliases = list(row["aliases"] or [])
        if row["name_en"]:
            aliases.append(row["name_en"])
        if row["ticker"]:
            aliases.append(row["ticker"])
        others.append(
            {
                "id": row["id"],
                "name": row["name_zh"],
                "ticker": row["ticker"] or "未上市",
                "market": market_label(row["market"]),
                "mentions": row["mentions"],
                "deep": row["deep"],
                "last": format_date(row["last_at"], settings.display_timezone) if row["last_at"] else "",
                "last_iso": format_iso(row["last_at"], settings.display_timezone) if row["last_at"] else "",
                "last_at": row["last_at"],
                "filter": " ".join([row["name_zh"], row["name_en"] or "", row["ticker"] or "", *aliases]),
            }
        )
    if sort == "recent":
        others.sort(key=lambda item: (item["last_at"] is not None, item["last_at"]), reverse=True)
    elif sort == "mentions":
        others.sort(key=lambda item: (-item["mentions"], -item["deep"], item["name"]))
    else:
        others.sort(key=lambda item: (-item["deep"], -item["mentions"], item["name"]))
    peak = max([item["deep"] for item in others] + [1])
    for item in others:
        item["bar"] = round(item["deep"] / peak * 100)
    market_order = ["HK", "US", "A", "PRIVATE"]
    market_tabs = [
        {"key": key, "label": market_label(key), "count": markets[key]} for key in market_order if key in markets
    ]
    return render_page(
        request,
        "companies.html",
        {
            "nav": "companies",
            "title": "公司",
            "others": others,
            "show_all": show_all,
            "hidden": hidden,
            "sort": sort,
            "sorts": COMPANY_SORTS,
            "market": market,
            "market_tabs": market_tabs,
            "others_total": sum(markets.values()),
            "list_query": _query_string(all="1" if show_all else "", sort=sort if sort != "deep" else ""),
            "sources": sources,
            "sample_banner": banner,
        },
    )


@router.get("/companies/{company_id}")
def company_page(request: Request, company_id: int):
    """一家公司：谁在什么时候深入谈它、原文怎么说。默认只看深入提及，一带而过的点一下再看。"""
    settings = request.app.state.settings
    depth_arg = request.query_params.get("depth") or ""
    if depth_arg in DEPTHS:
        depth_filter = depth_arg
    elif depth_arg == "all":
        depth_filter = None
    else:
        depth_arg = "deep"
        depth_filter = "deep"
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
        source_stats = list(queries.company_source_stats(conn, company_id, all_ids))
        depths = {row["depth"]: int(row["n"]) for row in queries.company_depth_counts(conn, company_id, ids)}
        months = list(queries.company_month_depths(conn, company_id, ids))
        total = queries.count_company_articles(conn, company_id, ids, depth_filter)
        page, pages, offset = page_window(total, _page_arg(request), settings.page_size)
        articles = queries.company_articles(conn, company_id, ids, depth_filter, settings.page_size, offset)
        banner = queries.sample_state(conn, all_ids)
        views_block = _company_views(request, conn, company, by_id, author)
        matcher = CompanyMatcher([company])
        rows = _decorate(
            conn,
            [
                _card(
                    row,
                    by_id[row["source_id"]],
                    settings,
                    extra={
                        "depth": row["depth"],
                        "depth_label": DEPTH_LABELS[row["depth"]],
                        "hits": row["hit_count"],
                        "snippet": mention_snippet(
                            row["body_text"] or "",
                            matcher.document(row["title"], row["body_text"] or "").spans(row["body_text"] or ""),
                        ),
                    },
                )
                for row in articles
            ],
            settings,
        )
    for card in rows:
        card["tags"] = [tag for tag in card["tags"] if tag["id"] != company_id]
    by_year: list[dict] = []
    for card in rows:
        year = card["day"][:4] if card.get("day") and card["day"][:1].isdigit() else "日期不详"
        if not by_year or by_year[-1]["year"] != year:
            by_year.append({"year": year, "items": []})
        by_year[-1]["items"].append(card)
    authors: dict[str, dict] = {}
    for row in source_stats:
        source = by_id.get(row["source_id"])
        if source is None:
            continue
        item = authors.setdefault(source.author, {"name": source.author, "deep": 0, "total": 0, "last": None})
        item["deep"] += row["deep"]
        item["total"] += row["total"]
        if row["last_deep"] and (item["last"] is None or row["last_deep"] > item["last"]):
            item["last"] = row["last_deep"]
    author_rows = sorted(authors.values(), key=lambda item: (-item["deep"], -item["total"], item["name"]))
    peak = max([item["deep"] for item in author_rows] + [1])
    for item in author_rows:
        item["bar"] = round(item["deep"] / peak * 100)
        item["last_label"] = format_date(item["last"], settings.display_timezone) if item["last"] else ""
    deep_total = sum(item["deep"] for item in author_rows)
    all_total = sum(item["total"] for item in author_rows)
    last_deep = max((item["last"] for item in author_rows if item["last"]), default=None)
    series = month_series(months, last=current_month(settings.display_timezone)) if months else []
    filter_query = _query_string(author=author, depth=depth_arg if depth_arg != "deep" else "")
    return render_page(
        request,
        "company.html",
        {
            "nav": "companies",
            "title": company["name_zh"],
            "company": company,
            "market": market_label(company["market"]),
            "chips": depth_chips(depths),
            "deep_count": sum(count for key, count in depths.items() if key != "passing"),
            "all_count": sum(depths.values()),
            "chart": Markup(company_chart(series)),
            "chart_ticks": chart_ticks(series),
            "first_month": series[0]["ym"] if series else "",
            "active_depth": depth_arg,
            "active_author": author,
            "author_rows": author_rows,
            "deep_total": deep_total,
            "all_total": all_total,
            "author_total": len([item for item in author_rows if item["total"]]),
            "last_deep": format_date(last_deep, settings.display_timezone) if last_deep else "",
            "last_deep_relative": relative_label(last_deep, settings.display_timezone) if last_deep else "",
            "filter_query": filter_query,
            "by_year": by_year,
            "page": page,
            "pages": pages,
            "total": total,
            "views": views_block,
            "sources": sources,
            "sample_banner": banner,
        },
    )


VIEWS_PAGE_SIZE = 20


def _recent_lead(conn, items: list[dict], settings) -> dict | None:
    """公司页「最近」一行。展开是导读；没有导读就用这条判断，避免空展开。"""
    if not items:
        return None
    latest = items[0]
    digest = queries.digests_for(conn, [latest["article_id"]]).get(latest["article_id"])
    text = recent_blurb(latest, digest)
    if not text:
        return None
    published = latest.get("published_at")
    return {
        "article_id": latest["article_id"],
        "title": latest["title"],
        "author": latest["author"],
        "date": latest["date"],
        "relative": relative_label(published, settings.display_timezone) if published else "",
        "stance": latest.get("stance") or "",
        "stance_label": latest.get("stance_label") or "",
        "chunk": latest.get("chunk"),
        "summary": text,
    }


def _company_views(request: Request, conn, company, by_id: dict, author: str) -> dict | None:
    """任何公司的观点时间线。缺的在后台排队，不挡住页面。不按持仓过滤。"""
    settings = request.app.state.settings
    kind = request.query_params.get("views") or ""
    company_id = int(company["id"])
    items = view_timeline(conn, company_id, by_id, settings)
    # 「还有几篇没整理」按当前筛的作者算；排队仍然按全部来源。
    scope_ids = [sid for sid, source in by_id.items() if not author or source.author == author]
    pending = count_view_pending(conn, company_id, scope_ids)
    if count_view_pending(conn, company_id, list(by_id)) if author else pending:
        queue_views(conn, settings, company_id, by_id)
    # 论点地图：观点归成的几条论点。缺了或过期了在后台重算，不挡页面。
    theses = thesis_map.present(thesis_map.stored(conn, company_id), items, company["name_zh"])
    if settings.theses_auto_enabled and thesis_map.needs_build(conn, company_id, by_id, settings):
        thesis_map.schedule(settings, company, by_id)
    thesis = next((item for item in theses if item["key"] == kind), None)
    if kind not in VIEW_FILTERS and thesis is None:
        kind = ""
    scoped = filter_timeline(items, "", author)
    chosen = thesis_map.mask_unrelated_quotes(
        filter_timeline(items, kind, author, thesis["articles"] if thesis else None),
        thesis,
    )
    raw_page = request.query_params.get("vpage", "1")
    page, pages, offset = page_window(len(chosen), int(raw_page) if raw_page.isdigit() else 1, VIEWS_PAGE_SIZE)
    base = _query_string(author=author, depth=request.query_params.get("depth") or "")
    filters = [
        {"key": "", "label": "全部", "count": len(scoped)},
        *[
            {"key": key, "label": label, "count": len(filter_timeline(items, key, author))}
            for key, label in VIEW_FILTERS.items()
        ],
    ]
    if thesis is not None:
        filters.append({"key": thesis["key"], "label": thesis["title"], "count": len(chosen)})
    shown = chosen[offset : offset + VIEWS_PAGE_SIZE]
    for item in shown:
        # 摘句和判断说的是同一件事就不再重复显示。
        item["quote_repeats"] = quote_repeats(item.get("quote") or "", item.get("judgment") or "")
    return {
        "items": shown,
        "kind": kind,
        "filters": filters,
        "theses": theses,
        "pending": pending,
        "pending_author": author,
        "page": page,
        "pages": pages,
        "base": base,
        # 跟当前筛选走。筛到某一条论点时，不要把没进这组的判断露在展开行里。
        "lead": _recent_lead(conn, chosen, settings),
    }


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
    """从时间轴点进某一天：先给汇总，再按作者列出这一天的文章。只含有发布时间的来源。

    日汇总本身不算模型。缺摘要的卡片在后台补，不挡住这一页。
    """
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
        feed = timeline_sources(sources)
        scope, info = resolve_scope(params, feed)
        by_id = _source_map(feed)
        rows = queries.articles_on_day(conn, scope)
        cards = order_cards(
            _decorate(
                conn,
                [_card(row, by_id[row["source_id"]], settings, zone=FEED_ZONE) for row in rows],
                settings,
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
    tree = None
    if info.get("tree"):
        tree = queries.get_article(conn, info["tree"], [source.id for source in sources])
    preset = request.query_params.get("preset") or ""
    question = request.query_params.get("q") or PRESETS.get(preset, "")
    return {
        "question": question[:500],
        "filters": info,
        "company": company,
        "article": article,
        "tree": tree,
        "authors": list(_author_groups(sources)[1]),
        "llm_ready": llm_configured(request.app.state.settings),
        "llm_label": active_label(request.app.state.settings),
        "model": active_model_id(request.app.state.settings),
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
    """先检索原文、登记整合，马上跳到回答页；模型在后台写，回答页自己刷新。"""
    if not _same_origin(request):
        raise HTTPException(status_code=403, detail="跨站请求被拒绝")
    form = await request.form()
    question = str(form.get("q") or "").strip()[:500]
    keys = ("author", "source", "company", "from", "to", "article", "tree")
    params = {key: str(form.get(key) or "") for key in keys}
    with database(request) as conn:
        sources = active_sources(request, conn)
        scope, info = resolve_scope(params, sources)
        if not question:
            return RedirectResponse("/ask?" + _query_string(**params), status_code=303)
        result = start(conn, request.app.state.settings, question, scope, sources)
        if result["id"] is not None:
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
            "tree": scope.get("tree_id"),
        }
        context = _ask_context(request, sources, info, conn)
        context["question"] = row["question"]
        banner = queries.sample_state(conn, [source.id for source in sources])
        labels = feedback_marks(conn, answer_id)
    citations = row["citations"] or []
    result = {
        "id": row["id"],
        "question": row["question"],
        "answer": row["answer"],
        "error": row["error"],
        "status": row["status"],
        "model": row["model"],
        "citations": citations,
        "terms": row["terms"] or [],
        "created": format_date(row["created_at"], request.app.state.settings.display_timezone),
        "checks": (
            check_answer(row["answer"], citations, {source.author for source in sources})
            if row["status"] == "done" and row["answer"]
            else []
        ),
        "irrelevant": [f"{article_id}:{chunk}" for article_id, chunk in labels["irrelevant"]],
        "missing": labels["missing"],
        "feedback_error": FEEDBACK_ERRORS.get(request.query_params.get("feedback_error") or "", ""),
        "failed_batches": list(((row.get("stats") or {}).get("failed_batches")) or []),
        "batches": int(((row.get("stats") or {}).get("batches")) or 0),
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


FEEDBACK_ERRORS = {"ref": "贴文章链接或编号，例如 /articles/123", "absent": "库里没有这篇文章"}


@router.post("/ask/{answer_id}/feedback")
async def ask_feedback(request: Request, answer_id: int):
    """回答页上的标注：某段原文不相关（再点一次取消），或补一篇漏了的文章。"""
    if not _same_origin(request):
        raise HTTPException(status_code=403, detail="跨站请求被拒绝")
    form = await request.form()
    label = str(form.get("label") or "")
    with database(request) as conn:
        if queries.get_answer(conn, answer_id) is None:
            raise HTTPException(status_code=404)
        if label == "irrelevant":
            try:
                article_id = int(str(form.get("article") or ""))
                chunk_raw = str(form.get("chunk") or "")
                chunk = int(chunk_raw) if chunk_raw.isdigit() else None
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="缺少文章") from exc
            toggle_irrelevant(conn, answer_id, article_id, chunk)
            anchor = str(form.get("n") or "")
            return RedirectResponse(f"/ask/{answer_id}" + (f"#c-{anchor}" if anchor.isdigit() else ""), status_code=303)
        if label == "missing":
            try:
                add_missing(conn, answer_id, str(form.get("ref") or ""), str(form.get("note") or ""))
            except ValueError as exc:
                code = "absent" if str(exc).startswith("库里没有") else "ref"
                return RedirectResponse(f"/ask/{answer_id}?feedback_error={code}#missing", status_code=303)
            return RedirectResponse(f"/ask/{answer_id}#missing", status_code=303)
    raise HTTPException(status_code=400, detail="label 只能是 irrelevant 或 missing")


@router.post("/ask/{answer_id}/retry")
async def ask_retry(request: Request, answer_id: int):
    if not _same_origin(request):
        raise HTTPException(status_code=403, detail="跨站请求被拒绝")
    with database(request) as conn:
        if queries.get_answer(conn, answer_id) is None:
            raise HTTPException(status_code=404)
        retry(conn, request.app.state.settings, answer_id)
    return RedirectResponse(f"/ask/{answer_id}", status_code=303)


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


@router.get("/pilot")
def pilot_page(request: Request):
    """新抽取规则的样卡和首页展开总结样稿，给复核用。只读 DATA_DIR/pilot 下的 JSON，不碰线上观点。"""
    import json as _json
    from pathlib import Path as _Path

    from researchlib.pilot import SUMMARY_FILE, VIEWS_FILE, pilot_cards

    settings = request.app.state.settings
    base = _Path(settings.data_dir) / "pilot"
    views = _json.loads((base / VIEWS_FILE).read_text()) if (base / VIEWS_FILE).exists() else {"articles": {}}
    summaries = _json.loads((base / SUMMARY_FILE).read_text()) if (base / SUMMARY_FILE).exists() else {}
    labels = {"positive": "看好", "negative": "看淡", "mixed": "两面", "neutral": "中性"}
    names = [("腾讯", 1), ("贵州茅台", 49), ("泡泡玛特", 2990)]
    groups = []
    for name, company_id in names:
        cards = pilot_cards(views, company_id, 20)
        for card in cards:
            card["stance_label"] = labels.get(card["stance"], "")
            siblings = [c for c in views["articles"].get(str(card["article_id"]), []) if c is not card and c["about"]]
            card["siblings"] = [c["company"] for c in siblings]
        dropped = [
            c
            for cards_ in views["articles"].values()
            for c in cards_
            if c["company_id"] == company_id and not c["about"]
        ]
        groups.append({"name": name, "id": company_id, "cards": cards, "dropped": dropped,
                       "summary": summaries.get(name)})
    with database(request) as conn:
        sources = active_sources(request, conn)
    return render_page(
        request, "pilot.html",
        {"nav": "", "title": "样卡复核", "groups": groups, "updated": views.get("updated_at", ""),
         "sources": sources, "sample_banner": None},
    )


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
    return PlainTextResponse("User-agent: *\nAllow: /\n")


@router.get("/favicon.svg")
def favicon():
    return Response(content=FAVICON, media_type="image/svg+xml")
