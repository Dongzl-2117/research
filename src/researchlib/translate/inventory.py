"""中英对照盘点：英文文章各在哪一步。只读，不调模型、不写库。

分成：从没译过、没译完、原文改过译文旧了、已译完（这一版回执 / 旧版回执 / 没回执的旧稿）；
没译完的里现在就能进队列的、冷却中的、同一版本拒满两次停住的；已译完的页面能不能逐段对照
（和 research-bilingual-audit 同一套判断）；分段粗、``--recheck`` 会挑的；数字待核多少段。
各类的 id 清单写进 DATA_DIR/status/translate-inventory.json，后面点名补译用。

在 Mac mini 上（只读，几分钟）：

    cd ~/research && docker compose exec -T app python -m researchlib.translate.inventory

还没上线时，可以把某个提交里的这个文件直接喂给容器里的 python：

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

from researchlib.config import Settings
from researchlib.db import connect
from researchlib.translate.audit import classify
from researchlib.translate.provenance import ALIGNMENT_VERSION, PIPELINE, SCHEMA_VERSION
from researchlib.translate.remote_media import pair_sources
from researchlib.translate.service import (
    COARSE_SCAN_CAP,
    HAN_OUTSIDE,
    REJECT_RETRY_BUDGET,
    _render_original,
    list_coarse_translations,
    list_pending_english,
)
from researchlib.visibility import visible_sql

# 和 list_pending_english 同一条语言规则、同样不算重复和隐藏的。
ENGLISH_SQL = f"""
    SELECT a.id, a.source_id, s.enabled,
           length(coalesce(nullif(a.body_text, ''), a.body_md, '')) AS chars,
           t.article_id IS NOT NULL AS translated,
           coalesce(t.complete, false) AS complete,
           coalesce(t.content_hash = a.content_hash, false) AS current,
           coalesce(t.provenance->>'schema', '') AS receipt_schema,
           t.provenance->'pipeline' AS pipeline,
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
DISPLAY_NAMES = {
    "mapped": "逐段对上",
    "lossless-candidate": "能从回执恢复",
    "legacy-structural": "结构对得上、没核验",
    "unmapped": "分开展示",
}


def _display_category(conn, article_id: int, data_dir) -> str:
    """和 research-bilingual-audit 一样：mapped / lossless-candidate / legacy-structural / unmapped。"""
    row = conn.execute(
        "SELECT a.*, t.body_html AS translated_html, coalesce(to_jsonb(t)->'provenance', '{}'::jsonb) AS provenance"
        " FROM articles a JOIN article_translations t ON t.article_id = a.id WHERE a.id = %s",
        (article_id,),
    ).fetchone()
    if row is None:
        return "missing"
    source, _raw_hash = _render_original(conn, row, data_dir)
    with_images, _raw_hash = _render_original(conn, row, data_dir, remote=True)
    source = pair_sources(with_images, source, row["translated_html"], row["provenance"]).check
    return classify(source, row["translated_html"], row["provenance"])["category"]


def collect(conn, settings, *, pages: bool = True, progress: Callable[[str], None] | None = None) -> dict:
    """盘点。每一段查询都在只读事务里，查完回滚。``pages`` 为假时不逐篇渲染页面。"""
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

    ids: dict[str, list[int]] = {key: [] for key in (*STATES, "pending-now", "cooldown", "stopped", "disabled-source")}
    sources: Counter[str] = Counter()
    chars: Counter[str] = Counter()
    review: dict[int, int] = {}
    review_segments = receipt_segments = gap_segments = 0
    last_reasons: Counter[str] = Counter()
    for row in rows:
        article_id = int(row["id"])
        sources[row["source_id"]] += 1
        if not row["enabled"]:
            ids["disabled-source"].append(article_id)
        if row["translated"] and row["current"] and row["complete"]:
            if not row["receipt_schema"]:
                state = "legacy"
            elif row["receipt_schema"] == str(SCHEMA_VERSION) and row["pipeline"] == PIPELINE:
                state = "receipt"
            else:
                state = "old-receipt"
            receipt_segments += int(row["receipt_segments"] or 0)
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
        ids[state].append(article_id)
        if state not in NOT_DONE:
            continue
        chars[state] += int(row["chars"] or 0)
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
    if pages:
        finished = ids["receipt"] + ids["old-receipt"] + ids["legacy"]
        started = time.monotonic()
        for number, article_id in enumerate(finished, 1):
            try:
                conn.execute("SET TRANSACTION READ ONLY")
                category = _display_category(conn, article_id, settings.data_dir)
            except Exception as exc:  # 一篇出错不影响别的篇，记下是哪一类错
                category = f"error:{type(exc).__name__}"
            finally:
                conn.rollback()
            display.setdefault(category, []).append(article_id)
            if progress and number % 100 == 0:
                progress(f"  页面 {number}/{len(finished)}，{time.monotonic() - started:.0f} 秒")
    return {
        "generatedAt": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "alignmentVersion": ALIGNMENT_VERSION,
        "english": len(rows),
        "sources": dict(sources),
        "chars": dict(chars),
        "reviewSegments": review_segments,
        "receiptSegments": receipt_segments,
        "gapSegments": gap_segments,
        "lastReasons": dict(last_reasons),
        "ids": ids,
        "display": display,
        "coarse": coarse,
        "review": review,
        "readOnly": True,
        "modelCalls": 0,
    }


def summary(report: dict) -> list[str]:
    """给人看的几行。"""
    ids = report["ids"]
    count = {key: len(value) for key, value in ids.items()}
    lines = [
        f"英文文章 {report['english']} 篇（"
        + "，".join(f"{name} {n}" for name, n in Counter(report["sources"]).most_common())
        + "）"
    ]
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
    lines.append(f"  没译完（partial）{count['partial']}，先放英文的段共 {report['gapSegments']} 段")
    lines.append(f"  原文改过、译文旧了 {count['stale']}")
    lines.append(f"  从没译过 {count['none']}")
    lines.append(
        f"  这三类里：现在就能进队列 {count['pending-now']}，冷却中 {count['cooldown']}，"
        f"同一版本拒满 {REJECT_RETRY_BUDGET} 次停住 {count['stopped']}"
    )
    chars = report["chars"]
    total = sum(chars.values())
    detail = "，".join(f"{name} {value:,}" for name, value in chars.items())
    lines.append(f"  这三类的英文共 {total:,} 字符（约 {total / 6:,.0f} 词）" + (f"：{detail}" if detail else ""))
    if report["lastReasons"]:
        lines.append(
            "  这三类最近一次尝试："
            + "，".join(f"{reason} {n}" for reason, n in Counter(report["lastReasons"]).most_common(8))
        )
    lines.append(f"  分段粗、--recheck 会挑的 {len(report['coarse'])} 篇（只看最新的 {COARSE_SCAN_CAP} 篇已译完的）")
    if report["display"]:
        lines.append(
            f"已译完的 {done} 篇页面："
            + "，".join(
                f"{DISPLAY_NAMES.get(category, category)} {len(found)}"
                for category, found in sorted(report["display"].items())
            )
        )
    return lines


def main() -> int:
    settings = Settings.from_env()
    conn = connect(settings)
    started = time.monotonic()
    try:
        print("逐篇看已译完的页面能不能逐段对照，一篇零点几秒，每 100 篇报一次进度…", file=sys.stderr, flush=True)
        report = collect(conn, settings, progress=lambda text: print(text, file=sys.stderr, flush=True))
    finally:
        conn.close()
    for line in summary(report):
        print(line)
    path = settings.data_dir / "status" / "translate-inventory.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"清单写在 {path}，共用了 {time.monotonic() - started:.0f} 秒")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
