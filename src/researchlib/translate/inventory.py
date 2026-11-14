"""中英对照盘点：英文文章各在哪一步。只读，不调模型、不写库。

分成：从没译过、没译完、原文改过译文旧了、已译完（这一版回执 / 旧版回执 / 没回执的旧稿）；
这一版回执里还有不是现在这一版提示词译的段的（old-prompt）；
没译完的里现在就能进队列的、冷却中的、同一版本拒满两次停住的、只有图没有要译的字的；
已译完的页面能不能逐段对照（和 research-bilingual-audit 同一套判断）、对不上是什么原因；
分段粗、``--recheck`` 会挑的；数字待核多少段；串段的篇（misplaced）。各类按来源分篇数、数英文字符，估费用用。
网站不显示的网友帖、「图片」评论壳单列，不算进上面各类：待译队列也不译它们。
各类的 id 清单写进 DATA_DIR/status/translate-inventory.json，后面点名补译用。

在 Mac mini 上（只读，几分钟）：

    cd ~/research && docker compose exec -T app research-translate-inventory

清单里某一类可以直接交给译制：``research-translate --from-inventory unmapped --workers 4``
（已经译完、有这一版回执、对照页逐段对得上的跳过，见 ``unfinished``）。
``--from-inventory old-prompt`` 把还有旧提示词译的段的篇按现在的提示词重译：回执逐段记了版本的只重译旧段，
没逐段记的（11-14 以前签的）整篇重译一次；段段都是这一版的跳过。
``--from-inventory misplaced`` 只重译串段的那一片，别的段照旧复用（provenance.reusable_targets）。

还没上线时，可以把某个提交里的这个文件直接喂给容器里的 python（容器里的代码要有它 import 的名字）：

    cd ~/research && curl -fsSL \\
      https://raw.githubusercontent.com/Dongzl-2117/research/<提交>/src/researchlib/translate/inventory.py \\
      | docker compose exec -T app python -
"""

from __future__ import annotations

import json
import sys
import time
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from researchlib.config import Settings
from researchlib.db import connect
from researchlib.translate.audit import classify
from researchlib.translate.provenance import (
    ALIGNMENT_VERSION,
    PROMPT_VERSION,
    SCHEMA_VERSION,
    copied_slots,
    echoed_slots,
    pipeline_matches,
    prompt_versions,
    source_manifest,
)
from researchlib.translate.remote_media import pair_sources
from researchlib.translate.segments import translatable_blocks
from researchlib.translate.service import (
    COARSE_SCAN_CAP,
    HAN_OUTSIDE,
    REJECT_RETRY_BUDGET,
    SITE_VISIBLE,
    _render_original,
    list_coarse_translations,
    list_pending_english,
)
from researchlib.visibility import visible_sql

# 和 list_pending_english 同一条语言规则、同样不算重复和隐藏的。网站不显示的另算（site_visible）。
ENGLISH_SQL = f"""
    SELECT a.id, a.source_id, s.enabled, {SITE_VISIBLE} AS site_visible,
           length(coalesce(nullif(a.body_text, ''), a.body_md, '')) AS chars,
           t.article_id IS NOT NULL AS translated,
           coalesce(t.complete, false) AS complete,
           coalesce(t.content_hash = a.content_hash, false) AS current,
           coalesce(t.provenance->>'schema', '') AS receipt_schema,
           t.provenance->'pipeline' AS pipeline,
           t.provenance->'prompt_versions' AS prompt_versions,
           coalesce(a.body_md, '') ~* '(^|\n) {0,3}######[ \t]|<h6[ >]' AS h6,
           CASE WHEN jsonb_typeof(t.provenance->'number_review') = 'array'
                THEN jsonb_array_length(t.provenance->'number_review') ELSE 0 END AS review,
           CASE WHEN jsonb_typeof(t.provenance->'untranslated') = 'array'
                THEN jsonb_array_length(t.provenance->'untranslated') ELSE 0 END AS gaps,
           CASE WHEN jsonb_typeof(t.provenance->'segments') = 'array'
                THEN jsonb_array_length(t.provenance->'segments') ELSE 0 END AS receipt_segments
    FROM articles a
    JOIN sources s ON s.id = a.source_id
    CROSS JOIN LATERAL (
        SELECT length(regexp_replace(head, %s, '', 'g')) AS han,
               regexp_count(head, '[A-Za-z]') AS latin
        FROM (SELECT left(coalesce(nullif(a.body_text, ''), a.body_md, ''), 400) AS head) h
        OFFSET 0
    ) lang
    LEFT JOIN article_translations t ON t.article_id = a.id
    WHERE a.duplicate_of IS NULL AND {visible_sql("a")}
      AND CASE a.raw_meta->>'language'
            WHEN 'zh' THEN false
            WHEN 'en' THEN true
            ELSE NOT (lang.han > 0 AND lang.han * 2 >= lang.latin)
          END
    ORDER BY a.published_at DESC NULLS LAST, a.id DESC
"""

# 这一版检查（ALIGNMENT_VERSION）、这一份原文下的尝试。
ATTEMPTS_SQL = """
    SELECT ta.article_id, ta.status, ta.reason
    FROM translation_attempts ta
    JOIN articles a ON a.id = ta.article_id
    WHERE ta.alignment_version = %s AND ta.content_hash = a.content_hash
    ORDER BY ta.article_id, ta.created_at, ta.id
"""

STATES = ("none", "partial", "stale", "receipt", "old-receipt", "legacy")
NOT_DONE = ("none", "partial", "stale")
# 这一版回执里还有段不是现在这一版提示词译的（整篇是旧版的，或者混着几版）。按这一类重译才换成现在的写法：
# 待译队列不会拿它们（译完了），--from-inventory 别的类也把它们算译完。混着几版的以前不算（以为是个别段重译失败），
# 其实整篇重译被欠费、断线打断时也是这样，后面几批全是旧译文（审核 T2）。回执逐段记了版本以后，再译只问旧段。
OLD_PROMPT = "old-prompt"
# 已译完、页面照样逐段对得上的篇里有串段（一段的中文写进了别的段）：
# 有段交回的还是英文（provenance.echoed_slots），多半是模型把它的中文一起写进了前一段，那一对只有英文；
# 或者相近两段英文不同、中文一字不差（provenance.copied_slots），同一次回答里的译文整体错开了一格。
# 按这一类再译只补那一片（provenance.reusable_targets）。
MISPLACED = "misplaced"
DISPLAY_NAMES = {
    "mapped": "逐段对上",
    "lossless-candidate": "能从回执恢复",
    "legacy-structural": "结构对得上、没核验",
    "unmapped": "分开展示",
}
# 页面对不上的原因（provenance.verify），给人看的说法。
REASON_NAMES = {
    "missing-provenance": "没回执的旧稿",
    "source-fingerprint": "原文清洗规则改过、回执过期",
    "target-receipt": "译文和回执对不上",
    "pipeline-version": "回执是旧版流水线签的",
    "invalid-receipt": "回执读不了",
    "display-notice": "回执对得上、文章页仍分开展示",
}
# 清单里有、但不交给译制的几类。
NOT_FOR_TRANSLATION = {
    "site-hidden": "网站不显示（网友帖、「图片」评论壳），不译",
    "no-text": "只有图、没有要译的字，译制会直接跳过",
    "disabled-source": "来源已停用，不译",
}


def _versions(row) -> list[int]:
    """这一行回执里的段用过哪几版提示词。行里只取了回执的 pipeline 和 prompt_versions 两项。"""
    return prompt_versions({"pipeline": row["pipeline"], "prompt_versions": row["prompt_versions"]})


def _page(conn, article_id: int, where) -> tuple[str, str, int, int]:
    """和 research-bilingual-audit 一样分类：(mapped / lossless-candidate / legacy-structural / unmapped, 原因,
    回执里交回英文的段数, 回执里和相近一段英文不同、中文一样的段数)。

    ``where`` 是 Settings 时，回执对得上的再按文章页那条路（清洗、换图、白名单、盖段落号）拼一遍、配一遍对：
    页面在核验回执以后还有几处会整页退成两份全文，只看回执会把这种篇算成逐段对上（审核 T10）。
    只给 data_dir 时只看回执。
    """
    data_dir = getattr(where, "data_dir", where)
    row = conn.execute(
        "SELECT a.*, t.body_html AS translated_html, coalesce(to_jsonb(t)->'provenance', '{}'::jsonb) AS provenance"
        " FROM articles a JOIN article_translations t ON t.article_id = a.id WHERE a.id = %s",
        (article_id,),
    ).fetchone()
    if row is None:
        return "missing", "", 0, 0
    source, _raw_hash = _render_original(conn, row, data_dir)
    with_images, _raw_hash = _render_original(conn, row, data_dir, remote=True)
    source = pair_sources(with_images, source, row["translated_html"], row["provenance"]).check
    found = classify(source, row["translated_html"], row["provenance"])
    provenance = row["provenance"]
    category, reason = found["category"], str(found.get("reason") or "")
    if category == "mapped" and hasattr(where, "sources_path"):
        from researchlib.translate.display import article_pairs
        from researchlib.translate.qa import _display_options, load_article_texts

        texts = load_article_texts(conn, where, article_id)
        if texts is not None and article_pairs(texts.en_html, texts.zh_html, **_display_options(texts)).notice:
            category, reason = "unmapped", "display-notice"
    return category, reason, len(echoed_slots(provenance)), len(copied_slots(provenance))


def _has_text(conn, article_id: int, data_dir) -> bool:
    """译制会不会拿这一篇调模型：和 translate_article 一样渲染原文、找要译的段。只有图的帖子没有。"""
    row = conn.execute("SELECT * FROM articles WHERE id = %s", (article_id,)).fetchone()
    if row is None:
        return False
    html, _raw_hash = _render_original(conn, row, data_dir)
    return bool(translatable_blocks(source_manifest(html)["source_snapshot"]))


def _each(conn, ids: list[int], label: str, check, progress) -> dict[int, object]:
    """逐篇只读地查，一篇出错不影响别的篇（记下是哪一类错）。每 100 篇报一次进度。"""
    found: dict[int, object] = {}
    started = time.monotonic()
    for number, article_id in enumerate(ids, 1):
        try:
            conn.execute("SET TRANSACTION READ ONLY")
            found[article_id] = check(article_id)
        except Exception as exc:  # 只读盘点：一篇出错不影响别的篇，出错的单列
            found[article_id] = exc
        finally:
            conn.rollback()
        if progress and number % 100 == 0:
            progress(f"  {label} {number}/{len(ids)}，{time.monotonic() - started:.0f} 秒")
    return found


def collect(conn, settings, *, pages: bool = True, progress: Callable[[str], None] | None = None) -> dict:
    """盘点。每一段查询都在只读事务里，查完回滚。``pages`` 为假时不逐篇渲染（页面分类和只有图的都不查）。"""
    try:
        conn.execute("SET TRANSACTION READ ONLY")
        rows = conn.execute(ENGLISH_SQL, (HAN_OUTSIDE,)).fetchall()
        attempts: dict[int, list[tuple[str, str]]] = {}
        for item in conn.execute(ATTEMPTS_SQL, (ALIGNMENT_VERSION,)).fetchall():
            attempts.setdefault(int(item["article_id"]), []).append((item["status"], item["reason"]))
        pending_now = set(list_pending_english(conn, limit=1_000_000))
        coarse = list_coarse_translations(conn, limit=1_000_000)
    finally:
        conn.rollback()

    categories = (*STATES, OLD_PROMPT, MISPLACED, "pending-now", "cooldown", "stopped", "no-text", *NOT_FOR_TRANSLATION)
    ids: dict[str, list[int]] = {key: [] for key in categories}
    sources: Counter[str] = Counter()
    # 每一类按来源的篇数和英文字符数，网站不显示的也在里面（site-hidden）。
    state_sources: dict[str, Counter[str]] = {}
    state_chars: dict[str, Counter[str]] = {}
    chars_of: dict[int, int] = {}
    review: dict[int, int] = {}
    review_segments = receipt_segments = gap_segments = mixed_prompts = h6_articles = 0
    last_reasons: Counter[str] = Counter()
    for row in rows:
        article_id = int(row["id"])
        source_id = row["source_id"]
        chars_of[article_id] = int(row["chars"] or 0)
        sources[source_id] += 1
        if not row["enabled"]:
            ids["disabled-source"].append(article_id)
        if not row["site_visible"]:
            state = "site-hidden"
        elif row["translated"] and row["current"] and row["complete"]:
            if not row["receipt_schema"]:
                state = "legacy"
            elif row["receipt_schema"] == str(SCHEMA_VERSION) and pipeline_matches(row["pipeline"]):
                state = "receipt"
            else:
                state = "old-receipt"
            receipt_segments += int(row["receipt_segments"] or 0)
            h6_articles += bool(row["h6"])
            if row["review"]:
                review_segments += int(row["review"])
                review[article_id] = int(row["review"])
        elif not row["translated"]:
            state = "none"
        elif not row["current"]:
            state = "stale"
        else:
            state = "partial"
            gap_segments += int(row["gaps"] or 0)
        keys = [state]
        if state == "receipt":
            versions = _versions(row)
            if versions != [PROMPT_VERSION]:
                keys.append(OLD_PROMPT)
            if PROMPT_VERSION in versions and len(versions) > 1:
                mixed_prompts += 1
        for key in keys:
            ids[key].append(article_id)
            state_sources.setdefault(key, Counter())[source_id] += 1
            state_chars.setdefault(key, Counter())[source_id] += chars_of[article_id]
        if state not in NOT_DONE:
            continue
        tried = attempts.get(article_id, [])
        if article_id in pending_now:
            ids["pending-now"].append(article_id)
        elif sum(status == "rejected" for status, _reason in tried) >= REJECT_RETRY_BUDGET:
            ids["stopped"].append(article_id)
        else:
            ids["cooldown"].append(article_id)
        if tried:
            last_reasons[tried[-1][1]] += 1

    display: dict[str, list[int]] = {}
    display_reasons: dict[str, Counter[str]] = {}
    display_chars: Counter[str] = Counter()
    echo_segments = copied_segments = 0
    if pages:
        waiting = [article_id for state in NOT_DONE for article_id in ids[state]]
        for article_id, has in _each(
            conn, waiting, "没译完的", lambda article_id: _has_text(conn, article_id, settings.data_dir), progress,
        ).items():
            if has is False:
                ids["no-text"].append(article_id)
        finished = ids["receipt"] + ids["old-receipt"] + ids["legacy"]
        for article_id, found in _each(
            conn, finished, "页面", lambda article_id: _page(conn, article_id, settings), progress,
        ).items():
            category, reason, echoes, copies = (
                found if isinstance(found, tuple) else (f"error:{type(found).__name__}", "", 0, 0)
            )
            display.setdefault(category, []).append(article_id)
            if echoes or copies:
                ids[MISPLACED].append(article_id)
                echo_segments += echoes
                copied_segments += copies
            display_chars[category] += chars_of.get(article_id, 0)
            if reason:
                display_reasons.setdefault(category, Counter())[reason] += 1
    return {
        "generatedAt": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "alignmentVersion": ALIGNMENT_VERSION,
        "english": len(rows),
        "sources": dict(sources),
        "stateSources": {state: dict(found) for state, found in state_sources.items()},
        "stateChars": {state: dict(found) for state, found in state_chars.items()},
        "reviewSegments": review_segments,
        "receiptSegments": receipt_segments,
        "gapSegments": gap_segments,
        "promptVersion": PROMPT_VERSION,
        "mixedPrompts": mixed_prompts,
        "h6Articles": h6_articles,
        "lastReasons": dict(last_reasons),
        "ids": ids,
        "display": display,
        "displayReasons": {category: dict(found) for category, found in display_reasons.items()},
        "displayChars": dict(display_chars),
        "echoSegments": echo_segments if pages else None,
        "copiedSegments": copied_segments if pages else None,
        "coarse": coarse,
        "review": review,
        "readOnly": True,
        "modelCalls": 0,
    }


def _by_source(counts: dict | None) -> str:
    """（stratechery 700，fangwei-zsxq 150）。只有一个来源、或者没有的时候不写。"""
    found = Counter(counts or {})
    if len(found) < 2:
        return ""
    return "（" + "，".join(f"{name} {n:,}" for name, n in found.most_common()) + "）"


def summary(report: dict) -> list[str]:
    """给人看的几行。"""
    ids = report["ids"]
    count = {key: len(value) for key, value in ids.items()}
    by_state = report.get("stateSources") or {}
    chars_by_state = report.get("stateChars") or {}
    lines = [
        f"英文文章 {report['english']} 篇（"
        + "，".join(f"{name} {n}" for name, n in Counter(report["sources"]).most_common())
        + "）"
    ]
    if count["site-hidden"]:
        hidden_sources = Counter(by_state.get("site-hidden") or {})
        lines.append(
            f"  网站不显示的网友帖、「图片」评论壳 {count['site-hidden']} 篇（"
            + "，".join(f"{name} {n}" for name, n in hidden_sources.most_common())
            + "），不算进下面各类，也不译"
        )
    if count["disabled-source"]:
        lines.append(f"  其中来源已停用的 {count['disabled-source']} 篇")
    done = count["receipt"] + count["old-receipt"] + count["legacy"]
    segments = report["receiptSegments"]
    share = f"，约 {report['reviewSegments'] / segments:.1%}" if segments else ""
    lines.append(
        f"  已译完 {done}：这一版回执 {count['receipt']}、旧版回执 {count['old-receipt']}、"
        f"没回执的旧稿 {count['legacy']}；有回执的 {segments} 段里数字待核 {report['reviewSegments']} 段{share}，"
        f"涉及 {len(report['review'])} 篇"
    )
    old = count.get(OLD_PROMPT, 0)
    version = report.get("promptVersion", PROMPT_VERSION)
    if old or count["legacy"]:
        old_chars = sum((chars_by_state.get(OLD_PROMPT) or {}).values())
        legacy_chars = sum((chars_by_state.get("legacy") or {}).values())
        lines.append(
            f"  其中第 {version} 版提示词以前译的：有回执的 {old}（old-prompt，英文 {old_chars:,} 字符），"
            f"没回执的旧稿 {count['legacy']}（legacy，英文 {legacy_chars:,} 字符）；"
            "要换成现在的写法，research-translate --from-inventory 这两类"
        )
    if report.get("mixedPrompts"):
        lines.append(
            f"  其中用第 {version} 版译过一部分、还有段是旧版的 {report['mixedPrompts']} 篇（整篇重译被打断，"
            "或个别段重译没过核对），算在 old-prompt 里"
        )
    if report.get("h6Articles"):
        lines.append(
            f"  原文带六级标题（######）的 {report['h6Articles']} 篇：六级标题不送译，页面上是英文（审核 T11）"
        )
    lines.append(f"  没译完（partial）{count['partial']}，先放英文的段共 {report['gapSegments']} 段")
    lines.append(f"  原文改过、译文旧了 {count['stale']}")
    lines.append(f"  从没译过 {count['none']}{_by_source(by_state.get('none'))}")
    lines.append(
        f"  这三类里：现在就能进队列 {count['pending-now']}，冷却中 {count['cooldown']}，"
        f"同一版本拒满 {REJECT_RETRY_BUDGET} 次停住 {count['stopped']}"
    )
    if count["no-text"]:
        lines.append(f"  这三类里只有图、没有要译的字的 {count['no-text']} 篇：译制直接跳过，不调模型")
    chars: Counter[str] = Counter()
    for state in NOT_DONE:
        chars.update(chars_by_state.get(state) or {})
    total = sum(chars.values())
    detail = _by_source(chars).strip("（）")
    lines.append(f"  这三类的英文共 {total:,} 字符（约 {total / 6:,.0f} 词）" + (f"：{detail}" if detail else ""))
    if report["lastReasons"]:
        lines.append(
            "  这三类最近一次尝试："
            + "，".join(f"{reason} {n}" for reason, n in Counter(report["lastReasons"]).most_common(8))
        )
    lines.append(f"  分段粗、--recheck 会挑的 {len(report['coarse'])} 篇（只看最新的 {COARSE_SCAN_CAP} 篇已译完的）")
    if report.get("echoSegments") is not None:
        echoes, copies = report["echoSegments"], report.get("copiedSegments") or 0
        line = (f"  串段（一段的中文写进了别的段）{count.get(MISPLACED, 0)} 篇：交回英文的段 {echoes} 段，"
                f"相近两段英文不同、中文一字不差的 {copies} 段")
        if echoes or copies:
            line += "。research-translate --from-inventory misplaced 只重译这几处附近的段"
        lines.append(line)
    if report["display"]:
        display_chars = report.get("displayChars") or {}
        parts = []
        for category, found in sorted(report["display"].items()):
            part = f"{DISPLAY_NAMES.get(category, category)} {len(found)}"
            if category != "mapped" and display_chars.get(category):
                part += f"（英文 {display_chars[category]:,} 字符）"
            parts.append(part)
        lines.append(f"已译完的 {done} 篇页面：" + "，".join(parts))
        for category, reasons in sorted((report.get("displayReasons") or {}).items()):
            if category in ("mapped", "legacy-structural"):
                continue
            lines.append(
                f"  {DISPLAY_NAMES.get(category, category)}的原因："
                + "，".join(f"{REASON_NAMES.get(reason, reason)} {n}" for reason, n in Counter(reasons).most_common())
            )
    return lines


def inventory_path(data_dir) -> Path:
    return Path(data_dir) / "status" / "translate-inventory.json"


def listed(data_dir, key: str) -> tuple[list[int], str]:
    """清单里某一类的 id（按清单里的先后，新的在前）和清单是什么时候盘的。

    key 是 ``ids`` 里的一类（stopped、legacy、none……）或者页面的一类（unmapped、legacy-structural……）。
    只有图、没有要译的字的篇不在里面。没有清单、没有这一类、这一类不该译（网站不显示的、只有图的、
    来源停用的）时抛 LookupError，说明该怎么办。分段粗的用 ``--recheck``，它每次现查。
    """
    if key in NOT_FOR_TRANSLATION:
        raise LookupError(f"「{key}」这一类{NOT_FOR_TRANSLATION[key]}")
    path = inventory_path(data_dir)
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise LookupError(f"还没有盘点清单 {path}：先跑 research-translate-inventory") from None
    ids = report.get("ids") or {}
    display = report.get("display") or {}
    if key in ids:
        found = ids[key]
    elif key in DISPLAY_NAMES or key in display:
        found = display.get(key) or []
    else:
        known = "、".join(sorted({*ids, *DISPLAY_NAMES, *display} - set(NOT_FOR_TRANSLATION)))
        hint = {
            "coarse": "；分段粗的用 research-translate --recheck",
            OLD_PROMPT: "；这份清单是分出这一类以前盘的，先重跑 research-translate-inventory",
            MISPLACED: "；这份清单是分出这一类以前盘的，先重跑 research-translate-inventory",
        }.get(key, "")
        raise LookupError(f"盘点清单里没有「{key}」这一类，有：{known}{hint}")
    skip = {int(article_id) for article_id in ids.get("no-text") or []}
    found = [int(article_id) for article_id in found]
    return [article_id for article_id in found if article_id not in skip], str(report.get("generatedAt") or "")


def unfinished(conn, ids: list[int], where, *, prompt: bool = False, misplaced: bool = False) -> list[int]:
    """这几篇里还要译的，按原来的先后：库里还在、还没有「译完、原文没改过、这一版回执、对照页逐段对得上」。

    点名重译旧稿、停住的、页面分开展示的篇时用：译完一篇它就不在这里了，中途停下再跑同一条命令接着译剩下的。
    已经有这一版回执的，还要渲染一遍页面看对不对得上：清洗规则改过以后原文指纹变了，回执就过期了，
    页面分开展示。现在这一版提示词译的再译一遍时没变的段照旧复用，原文一个字没变就一次模型也不调，只重签回执；
    旧提示词译的段按现在的提示词重译（service.translate_article）。
    ``prompt`` 为真时（``--from-inventory old-prompt``），回执里还有段不是现在这一版提示词译的也算没译完，
    译制只重译那几段（回执没逐段记版本的整篇重译一次）。
    ``misplaced`` 为真时（``--from-inventory misplaced``），回执里还有串段的也算没译完；再译时只补那一片。
    """
    if not ids:
        return []
    rows = conn.execute(
        """
        SELECT a.id,
               coalesce(t.complete AND t.content_hash = a.content_hash, false) AS current,
               coalesce(t.provenance->>'schema', '') AS receipt_schema,
               t.provenance->'pipeline' AS pipeline,
               t.provenance->'prompt_versions' AS prompt_versions
        FROM articles a
        LEFT JOIN article_translations t ON t.article_id = a.id
        WHERE a.id = ANY(%s)
        """,
        (list(ids),),
    ).fetchall()
    left = set()
    for row in rows:
        article_id = int(row["id"])
        signed = (row["current"] and row["receipt_schema"] == str(SCHEMA_VERSION)
                  and pipeline_matches(row["pipeline"]))
        if not signed or (prompt and _versions(row) != [PROMPT_VERSION]):
            left.add(article_id)
            continue
        try:
            category, _reason, echoes, copies = _page(conn, article_id, where)
        except Exception:  # 页面渲染不了的交给译制，出错会记下来
            category, echoes, copies = "error", 0, 0
        if category != "mapped" or (misplaced and (echoes or copies)):
            left.add(article_id)
    return [article_id for article_id in ids if article_id in left]


def main() -> int:
    settings = Settings.from_env()
    conn = connect(settings)
    started = time.monotonic()
    try:
        print(
            "逐篇看没译的有没有要译的字、已译完的页面能不能逐段对照，一篇零点几秒，每 100 篇报一次进度…",
            file=sys.stderr, flush=True,
        )
        report = collect(conn, settings, progress=lambda text: print(text, file=sys.stderr, flush=True))
    finally:
        conn.close()
    for line in summary(report):
        print(line)
    path = inventory_path(settings.data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"清单写在 {path}，共用了 {time.monotonic() - started:.0f} 秒")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
