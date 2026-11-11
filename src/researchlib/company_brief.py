"""公司页「概览」和公司列表用的归拢：把观点时间线按作者、按月数出来，画成态度图。

只计数和排版，不调模型，不写库。输入是 ``views.timeline`` 的结果（新的在前），每条至少有
``author``、``date``（``YYYY-MM-DD`` 或「日期不详」）、``stance``。

态度图是按月的分向柱：看好画在基线上面，看淡画在下面，两面和中性合成中间一段、跨在基线上。
方向和颜色说的是同一件事，分不清红绿也读得出；具体条数写在图下面的文字里。
"""

from __future__ import annotations

import re
from html import escape
from urllib.parse import urlencode

from markupsafe import Markup

STANCE_KEYS = ("positive", "mixed", "negative", "neutral")
STANCE_WORDS = {"positive": "看好", "mixed": "两面", "negative": "看淡", "neutral": "中性"}
# 图上最少画一年，最多两年：再早的判断照样算进条数，只是不画。
AXIS_LEAST = 12
AXIS_MOST = 24
INDEX_MONTHS = 12
# 概览里一位作者列几条最近的判断；判断不到 MAJOR 条的作者收进「其他作者」一行。
LEAD = 3
SOLO_LEAD = 5
MAJOR = 3
MAIN_AUTHORS = 4
# 「判断」一页多少条。一条一行，比以前整张卡片摊开时的 20 条多放一倍。
PAGE_SIZE = 40
STANCE_FILTERS = (("positive", "看好"), ("mixed", "两面"), ("negative", "看淡"), ("neutral", "中性"))
TABS = ("overview", "views", "articles")


def month_of(item: dict) -> str:
    """这条判断的月份 ``YYYY-MM``；没有日期（飞书文库）返回空串。"""
    date = str(item.get("date") or "")
    return date[:7] if date[:4].isdigit() and len(date) >= 7 else ""


def _back(ym: str, count: int) -> str:
    total = int(ym[:4]) * 12 + int(ym[5:7]) - 1 - count
    return f"{total // 12:04d}-{total % 12 + 1:02d}"


def _next(ym: str) -> str:
    return _back(ym, -1)


def month_axis(items: list[dict], last: str, *, least: int = AXIS_LEAST, most: int = AXIS_MOST) -> list[str]:
    """态度图的横轴：从最早一条带日期的判断那个月到 last（当前月），至少 least 个月，最多 most 个月。"""
    months = [month for month in (month_of(item) for item in items) if month and month <= last]
    start = max(min(months), _back(last, most - 1)) if months else last
    start = min(start, _back(last, least - 1))
    axis = []
    current = start
    while current <= last and len(axis) < most:
        axis.append(current)
        current = _next(current)
    return axis


def stance_counts(items: list[dict]) -> dict[str, int]:
    counts = dict.fromkeys(STANCE_KEYS, 0)
    for item in items:
        stance = item.get("stance") or ""
        if stance in counts:
            counts[stance] += 1
    return counts


def stance_tally(counts: dict[str, int]) -> list[dict]:
    """和态度图一一对应的三个数：看好、两面或中性、看淡。图上中间那段是两面加中性，这里也合在一起。"""
    middle = counts.get("mixed", 0) + counts.get("neutral", 0)
    return [
        {"key": "positive", "label": "看好", "short": "看好", "count": counts.get("positive", 0)},
        {"key": "mixed", "label": "两面或中性", "short": "两面", "count": middle},
        {"key": "negative", "label": "看淡", "short": "看淡", "count": counts.get("negative", 0)},
    ]


def monthly(items: list[dict], axis: list[str]) -> list[dict]:
    """按横轴数每个月各种态度的条数。不在横轴里的（太早的、没日期的）不进图。"""
    series = {ym: {"ym": ym, **dict.fromkeys(STANCE_KEYS, 0)} for ym in axis}
    for item in items:
        row = series.get(month_of(item))
        stance = item.get("stance") or ""
        if row is not None and stance in STANCE_KEYS:
            row[stance] += 1
    return [series[ym] for ym in axis]


def chart_peaks(groups: list[list[dict]]) -> tuple[float, float]:
    """几张图共用的上下高度：(基线以上最高, 基线以下最高)，单位是条。并排的几位作者才比得出谁说得多。"""
    up = down = 0.0
    for series in groups:
        for row in series:
            middle = (row["mixed"] + row["neutral"]) / 2
            up = max(up, row["positive"] + middle)
            down = max(down, row["negative"] + middle)
    return up, down


def _tip(row: dict) -> str:
    parts = [f"{STANCE_WORDS[key]} {row[key]}" for key in STANCE_KEYS if row[key]]
    return f"{row['ym']}：" + ("，".join(parts) if parts else "没有判断")


def stance_chart(
    series: list[dict],
    *,
    peaks: tuple[float, float] | None = None,
    height: int = 64,
    slot: int = 12,
    bar: int = 8,
    label: str = "",
) -> str:
    """按月的分向柱。SVG 横向拉伸填满容器，柱宽跟着变，高度不变；月份另用 HTML 标在图下。"""
    if not series:
        return ""
    up, down = peaks if peaks is not None else chart_peaks([series])
    total = up + down
    width = len(series) * slot
    pad = 2
    plot = height - pad * 2
    # 全是看好时基线贴底，全是看淡时贴顶；什么都没有时放中间。
    base = pad + (plot * up / total if total else plot / 2)
    unit = plot / total if total else 0.0
    name = escape(label or "按月的判断：向上看好，向下看淡")
    parts = [
        f'<svg class="stance-chart" viewBox="0 0 {width} {height}" preserveAspectRatio="none" role="img" '
        f'aria-label="{name}">',
        f'<line class="axis" x1="0" y1="{base:.1f}" x2="{width}" y2="{base:.1f}"/>',
    ]
    for index, row in enumerate(series):
        x = index * slot + (slot - bar) / 2
        middle = (row["mixed"] + row["neutral"]) * unit
        positive = row["positive"] * unit
        negative = row["negative"] * unit
        parts.append(f"<g><title>{escape(_tip(row))}</title>")
        parts.append(f'<rect class="hit" x="{index * slot}" y="0" width="{slot}" height="{height}"/>')
        # 段和段之间留 1px 底色，叠在一起也分得开。
        gap = 1.0
        if middle:
            parts.append(
                f'<rect class="bar-mid" x="{x:.1f}" y="{base - middle / 2:.1f}" width="{bar}" '
                f'height="{max(middle, 1.5):.1f}"/>'
            )
        if positive:
            top = base - middle / 2 - positive
            parts.append(
                f'<rect class="bar-pos" x="{x:.1f}" y="{top:.1f}" width="{bar}" '
                f'height="{max(positive - (gap if middle else 0), 1.5):.1f}"/>'
            )
        if negative:
            top = base + middle / 2 + (gap if middle else 0)
            parts.append(
                f'<rect class="bar-neg" x="{x:.1f}" y="{top:.1f}" width="{bar}" '
                f'height="{max(negative - (gap if middle else 0), 1.5):.1f}"/>'
            )
        parts.append("</g>")
    parts.append("</svg>")
    return "".join(parts)


def axis_ticks(axis: list[str]) -> list[dict]:
    """图下的月份：第一个月、每年一月、最后一个月的位置（百分比）。挨得太近的让给后一个。"""
    if not axis:
        return []
    ticks: list[dict] = []
    count = len(axis)
    for index, ym in enumerate(axis):
        edge = index in (0, count - 1)
        if not edge and not ym.endswith("-01"):
            continue
        left = round((index + 0.5) / count * 100, 2)
        label = f"{ym[:4]}年{int(ym[5:7])}月" if index == 0 or ym.endswith("-01") else f"{int(ym[5:7])}月"
        if ticks and left - ticks[-1]["left"] < 16:
            if index == count - 1 and len(ticks) > 1:
                ticks.pop()
            else:
                continue
        ticks.append({"label": label, "left": left})
    return ticks


def author_briefs(
    items: list[dict], axis: list[str], *, lead: int = LEAD, major: int = MAJOR, cap: int = MAIN_AUTHORS
) -> dict:
    """概览「现在怎么看」：每位作者一张卡（态度图、各态度条数、最近几条判断）。

    图上这段时间里说得多的排前面。判断不到 major 条的作者只占一行；一位都不到时，每位都给一张卡。
    带日期的判断在图上这段时间里不到 major 条的不画图：一两根柱子读不出走向，条数写在字里就够了。
    返回 {"main": [...], "minor": [...], "total": 条数, "ticks": 横轴刻度, "charted": 有没有画图}；
    main 里的图共用一个高度。
    """
    by_author: dict[str, list[dict]] = {}
    for item in items:
        by_author.setdefault(item.get("author") or "", []).append(item)
    window = set(axis)
    rows = []
    for name, own in by_author.items():
        dated = sorted(item["date"] for item in own if month_of(item))
        counts = stance_counts(own)
        rows.append(
            {
                "name": name,
                "count": len(own),
                "recent": sum(1 for item in own if month_of(item) in window),
                "counts": counts,
                "tally": [part for part in stance_tally(counts) if part["count"]],
                "first": dated[0] if dated else "",
                "last": dated[-1] if dated else "",
                "series": monthly(own, axis),
                "latest": own[:lead],
            }
        )
    rows.sort(key=lambda row: (-row["recent"], -row["count"], row["name"]))
    main = ([row for row in rows if row["count"] >= major] or rows)[:cap]
    if len(main) == 1:
        # 只有一位作者够一张卡：卡片横着铺满，右边多列两条。
        main[0]["latest"] = by_author[main[0]["name"]][:SOLO_LEAD]
    chosen = {row["name"] for row in main}
    minor = [row for row in rows if row["name"] not in chosen]
    drawn = [row for row in main if row["recent"] >= major]
    peaks = chart_peaks([row["series"] for row in drawn])
    for row in main:
        row["chart"] = ""
    for row in drawn:
        row["chart"] = stance_chart(row["series"], peaks=peaks, label=f"{row['name']}按月的判断：向上看好，向下看淡")
    return {
        "main": main,
        "minor": minor,
        "total": len(items),
        "ticks": axis_ticks(axis),
        "charted": bool(drawn),
    }


def month_groups(items: list[dict]) -> list[dict]:
    """判断列表按月分组，顺序不动（新的在前）。没日期的归到「日期不详」。"""
    groups: list[dict] = []
    for item in items:
        key = month_of(item)
        if not groups or groups[-1]["key"] != key:
            label = f"{key[:4]} 年 {int(key[5:7])} 月" if key else "日期不详"
            groups.append({"key": key, "label": label, "items": []})
        groups[-1]["items"].append(item)
    return groups


def has_detail(item: dict) -> bool:
    """这条判断点开有没有东西看：理由、风险、推翻条件、验证指标、原句。都没有就不做成可展开的。"""
    return bool(
        item.get("mechanism")
        or item.get("risks")
        or item.get("falsifiers")
        or item.get("metrics")
        or (item.get("quote") and not item.get("quote_repeats"))
    )


def index_marks(rows: list[dict], last: str, *, months: int = INDEX_MONTHS) -> dict[int, dict]:
    """公司列表上每家公司的一格：各态度条数、近 months 个月的小图。

    rows：[{company_id, stance, date}]，全部下了判断的观点。小图各家各自定高，只看自己的起伏。
    """
    axis = [_back(last, offset) for offset in range(months - 1, -1, -1)]
    by_company: dict[int, list[dict]] = {}
    for row in rows:
        by_company.setdefault(int(row["company_id"]), []).append(row)
    found = {}
    for company_id, own in by_company.items():
        counts = stance_counts(own)
        found[company_id] = {
            "count": len(own),
            "tally": stance_tally(counts),
            "chart": stance_chart(monthly(own, axis), height=26, slot=7, bar=5, label="近一年按月的判断"),
        }
    return found


# —— 页面上下文：公司页三页各自要的东西。路由只管取数，怎么归拢在这里，预览和测试也用同一份 ——


def company_href(company_id: int, tab: str = "", **params) -> str:
    """公司页的地址。概览是默认页，不带 tab；空的参数不写。"""
    query = [("tab", tab)] if tab and tab != "overview" else []
    query += [(key, value) for key, value in params.items() if value not in ("", None)]
    return f"/companies/{company_id}" + (f"?{urlencode(query)}" if query else "")


def decorate_views(items: list[dict], repeats) -> list[dict]:
    """给每条判断挂上原文地址（对得上原句就落到那一段）、摘句是不是在重复判断、点开有没有东西看。

    repeats 是 ``views.quote_repeats``，从外面传进来，这个模块就不用连数据库那一串依赖。
    """
    for item in items:
        href = f"/articles/{item['article_id']}"
        if item.get("chunk") is not None:
            href += f"?chunk={item['chunk']}" + (f"#{item['anchor']}" if item.get("anchor") else "")
        item["href"] = href
        item["quote_repeats"] = repeats(item.get("quote") or "", item.get("judgment") or "")
        item["detail"] = has_detail(item)
    return items


# 归拢论点时观点按「[v文章编号]」喂给模型，模型常把这个编号原样写进分歧、变化和原话里（「v460认为……」）。
VIEW_REF = re.compile(r"(?<![A-Za-z0-9])[vV](\d{1,9})(?![A-Za-z0-9])")
EVIDENCE_REF = re.compile(r"^\s*[\[［【(（]?[vV](\d{1,9})[\]］】)）]?\s*[：:，,]?\s*")
REF_FILLERS = " 在于和与、的"


def _ref_link(view: dict, *, bare: bool) -> str:
    """一处「v460」换成的字：作者加日期，连到那篇文章。bare 是前面刚提过作者或在括号里，只写日期。"""
    dated = bool(month_of(view))
    author = view.get("author") or ""
    if bare or not author:
        label = view["date"] if dated else "原文"
    else:
        label = f"{author} {view['date']}" if dated else author
    title = escape(view.get("title") or "", quote=True)
    return f'<a class="ref" href="/articles/{int(view["article_id"])}" title="{title}">{escape(label)}</a>'


def name_refs(text: str, by_article: dict[int, dict]) -> Markup:
    """论点文字里的「v460」换成读得懂的出处：作者和日期，连到那篇文章。

    前面紧挨着这位作者的名字（「方伟v533承认」「方伟在v709称」）或者在括号里（「（v112、v314）」）时只写日期，
    免得名字说两遍。时间线上找不到的编号（那条观点后来没了）写成「另一篇」，不给链接。其余文字照样转义。
    """
    text = str(text or "")
    out: list[str] = []
    cursor = 0
    was_bare = False
    for match in VIEW_REF.finditer(text):
        before = text[cursor : match.start()]
        out.append(escape(before))
        view = by_article.get(int(match.group(1)))
        if view is None:
            out.append("另一篇")
            was_bare = False
        else:
            head = text[: match.start()]
            last = head[-1:] if head else ""
            named = head.rstrip(REF_FILLERS).endswith(view.get("author") or "\0")
            bare = named or (last != "" and last in "（(") or (was_bare and before == "、")
            out.append(_ref_link(view, bare=bare))
            was_bare = bare
        cursor = match.end()
    out.append(escape(text[cursor:]))
    return Markup("".join(out))


def evidence_lines(lines: list[str], by_article: dict[int, dict]) -> list[dict]:
    """论点的原话：开头的「v460：」拆出来当出处，页面上写在原话后面。"""
    shaped = []
    for line in lines or []:
        line = str(line or "")
        match = EVIDENCE_REF.match(line)
        view = by_article.get(int(match.group(1))) if match else None
        body = line[match.end() :] if match else line
        if not body.strip():
            continue
        shaped.append(
            {
                "text": name_refs(body, by_article),
                "source": Markup(_ref_link(view, bare=False)) if view else None,
            }
        )
    return shaped


def overview(items: list[dict], theses: list[dict], company_id: int, last: str) -> dict:
    """概览：各位作者现在怎么看，加主要论点。theses 是 ``theses.present`` 的结果。"""
    brief = author_briefs(items, month_axis(items, last))
    for row in brief["main"] + brief["minor"]:
        row["href"] = company_href(company_id, "views", author=row["name"])
    by_article = {item["article_id"]: item for item in items}
    shaped = []
    for thesis in theses:
        dates = [
            by_article[ref]["date"]
            for ref in thesis.get("articles") or []
            if ref in by_article and month_of(by_article[ref])
        ]
        judgment = thesis.get("judgment") or ""
        shaped.append(
            {
                **thesis,
                # 判断和标题一字不差的只写标题。
                "judgment": name_refs(judgment, by_article) if judgment != thesis.get("title") else "",
                "mechanism": name_refs(thesis.get("mechanism") or "", by_article),
                "changes": name_refs(thesis.get("changes") or "", by_article),
                "disagreement": name_refs(thesis.get("disagreement") or "", by_article),
                "evidence": evidence_lines(thesis.get("evidence") or [], by_article),
                "latest": max(dates) if dates else "",
                "href": company_href(company_id, "views", thesis=thesis["key"]),
            }
        )
    return {"brief": brief, "theses": shaped}


def judgments(
    items: list[dict],
    theses: list[dict],
    company_id: int,
    *,
    author: str = "",
    stance: str = "",
    thesis_key: str = "",
    page: int = 1,
    size: int = PAGE_SIZE,
) -> dict:
    """「判断」一页：按作者、态度、论点筛，按月分组。每个筛选项上的数字都按另外几项当前的选择算。"""
    thesis = next((item for item in theses if item["key"] == thesis_key), None)
    scoped = items
    if thesis is not None:
        wanted = set(thesis.get("articles") or [])
        scoped = [item for item in items if item["article_id"] in wanted]
    known = {key for key, _label in STANCE_FILTERS}
    stance = stance if stance in known else ""
    by_stance = [item for item in scoped if not stance or item.get("stance") == stance]
    by_author = [item for item in scoped if not author or item.get("author") == author]
    chosen = [item for item in by_author if not stance or item.get("stance") == stance]
    keep = {"thesis": thesis["key"] if thesis else ""}

    def href(**params) -> str:
        return company_href(company_id, "views", **{**keep, **params})

    names: dict[str, int] = {}
    for item in by_stance:
        names[item.get("author") or ""] = names.get(item.get("author") or "", 0) + 1
    if author and author not in names:
        names[author] = 0
    authors = [{"label": "全部作者", "count": len(by_stance), "href": href(stance=stance), "current": not author}]
    for name, count in sorted(names.items(), key=lambda pair: (-pair[1], pair[0])):
        authors.append(
            {"label": name, "count": count, "href": href(author=name, stance=stance), "current": author == name}
        )
    counts = stance_counts(by_author)
    stances = [{"label": "全部", "count": len(by_author), "href": href(author=author), "current": not stance}]
    for key, label in STANCE_FILTERS:
        if counts[key] or stance == key:
            link = href(author=author, stance=key)
            stances.append({"label": label, "count": counts[key], "href": link, "current": stance == key})
    pages = max(1, -(-len(chosen) // size))
    page = min(max(1, page), pages)
    shown = chosen[(page - 1) * size : page * size]
    scope = None
    if thesis is not None:
        clear = company_href(company_id, "views", author=author, stance=stance)
        scope = {"key": thesis["key"], "title": thesis["title"], "clear": clear}
    return {
        "groups": month_groups(shown),
        "items": shown,
        "authors": authors,
        "stances": stances,
        "thesis": scope,
        "author": author,
        "stance": stance,
        "page": page,
        "pages": pages,
        "prev": href(author=author, stance=stance, vpage=page - 1 if page > 2 else "") if page > 1 else "",
        "next": href(author=author, stance=stance, vpage=page + 1) if page < pages else "",
        "total": len(chosen),
    }


def tabs(company_id: int, views_total: int, articles_total: int) -> list[dict]:
    """公司页顶上三页。一条判断都没有的公司不分页，直接是文章。"""
    if not views_total:
        return []
    return [
        {"key": "overview", "label": "概览", "count": None, "href": company_href(company_id)},
        {"key": "views", "label": "判断", "count": views_total, "href": company_href(company_id, "views")},
        {"key": "articles", "label": "文章", "count": articles_total, "href": company_href(company_id, "articles")},
    ]
