"""不调模型重签中英对照回执，报出要调模型才补得上的篇（审核 T1、T3）。

回执过期不只因为改了清洗规则：同一来源的统计（重复出现的图、文末模板行）变了，旧文章的原文指纹也跟着变，
对照页悄悄退成「保留两份全文」。夜间下到新图、导入一批新文章都会这样，以前没有任务发现。
11-11 译完的那批译文里，AT&T、R&D 存成了「AT&T;」，也要重写一遍。

这条命令逐篇看已经有这一版回执的译文：页面对不上（和 research-translate-inventory 同一套判断）、
存着「&T;」这种认不得的实体、或者有段被当成标签吞掉了字的，就按 ``translate_article(offline=True)`` 重签：
要译的段全部能从回执复用、标题也有才写，一次模型也不调；有一段要调模型就不写，列出来。
结果写进 DATA_DIR/status/translate-resign.json：还有要调模型才补得上的篇时记成失败，research-status 会报，
由人决定花不花钱（``research-translate --article-id …``）。

    docker compose exec -T app research-translate-resign --dry-run   # 只列，不写
    docker compose exec -T app research-translate-resign             # 夜间补齐里每晚跑一次
"""

from __future__ import annotations

import argparse
import sys
import time

from researchlib.config import Settings
from researchlib.db import connect, ensure_search_session
from researchlib.health import deploy_waiting, write_status
from researchlib.htmlentities import has_broken_entity
from researchlib.translate.cli import LOCK_NAME
from researchlib.translate.inventory import _page
from researchlib.translate.provenance import SCHEMA_VERSION, pipeline_matches
from researchlib.translate.segments import unknown_elements
from researchlib.translate.service import SITE_VISIBLE, translate_article
from researchlib.visibility import visible_sql

TASK = "translate-resign"

# 有这一版回执、原文没改过的译文（没译完的也算：先放英文的段照旧放英文，别的段重签）。
CANDIDATES_SQL = f"""
    SELECT a.id, t.provenance->'pipeline' AS pipeline, coalesce(t.provenance->>'schema', '') AS receipt_schema
    FROM articles a
    JOIN article_translations t ON t.article_id = a.id
    WHERE t.content_hash = a.content_hash
      AND a.duplicate_of IS NULL AND {visible_sql("a")} AND {SITE_VISIBLE}
      AND coalesce(t.provenance->>'schema', '') <> ''
    ORDER BY a.published_at DESC NULLS LAST, a.id DESC
"""


def swallowed(provenance: dict | None) -> bool:
    """回执里有段多出原文没有的、不是 HTML 元素的标签：原文写着的「&lt;…&gt;」被当成标签吞掉了一截字。"""
    for item in (provenance or {}).get("segments") or []:
        try:
            if set(unknown_elements(item["target"])) - set(unknown_elements(item["source"])):
                return True
        except (KeyError, TypeError):
            continue
    return False


def why(conn, article_id: int, settings) -> str:
    """这一篇为什么要重签；不用重签返回空字符串。"""
    row = conn.execute(
        "SELECT body_html, provenance FROM article_translations WHERE article_id = %s", (article_id,)
    ).fetchone()
    if row is None:
        return ""
    if has_broken_entity(row["body_html"] or ""):
        return "entities"
    if swallowed(row["provenance"]):
        return "swallowed"
    category, reason, _echoes, _copies = _page(conn, article_id, settings)
    if category != "mapped":
        return f"unmapped:{reason or category}"
    return ""


def _try_lock(conn, article_id: int) -> bool:
    row = conn.execute(
        "SELECT pg_try_advisory_lock(hashtext(%s), %s) AS locked", (LOCK_NAME, article_id)
    ).fetchone()
    conn.commit()
    return bool(row and row["locked"])


def _unlock(conn, article_id: int) -> None:
    try:
        conn.execute("SELECT pg_advisory_unlock(hashtext(%s), %s)", (LOCK_NAME, article_id))
        conn.commit()
    except Exception:  # noqa: BLE001 — 连接坏了锁随会话一起没了
        conn.rollback()


def run(conn, settings, *, ids: list[int] | None = None, dry_run: bool = False, limit: int | None = None,
        say=print) -> dict:
    """逐篇看、能重签的重签。返回各类的 id。"""
    if ids is None:
        conn.execute("SET TRANSACTION READ ONLY")
        rows = conn.execute(CANDIDATES_SQL).fetchall()
        conn.rollback()
        ids = [int(row["id"]) for row in rows
               if row["receipt_schema"] == str(SCHEMA_VERSION) and pipeline_matches(row["pipeline"])]
    found: dict[str, list] = {"resigned": [], "needs-model": [], "failed": [], "busy": []}
    reasons: dict[int, str] = {}
    checked = 0
    started = time.monotonic()
    for number, article_id in enumerate(ids, 1):
        if limit is not None and len(found["resigned"]) + len(found["needs-model"]) >= limit:
            break
        if not dry_run and deploy_waiting(settings.data_dir):
            say("自动上线在等新提交：先停下，下次接着重签")
            found["stopped"] = True
            break
        checked += 1
        try:
            reason = why(conn, article_id, settings)
        except Exception as exc:  # noqa: BLE001 — 一篇看不了不影响别的篇
            conn.rollback()
            found["failed"].append(article_id)
            say(f"#{article_id} 看不了：{type(exc).__name__}: {exc}")
            continue
        finally:
            conn.rollback()
        if number % 200 == 0:
            say(f"  看了 {number}/{len(ids)} 篇，{time.monotonic() - started:.0f} 秒")
        if not reason:
            continue
        reasons[article_id] = reason
        if dry_run:
            found.setdefault("would", []).append(article_id)
            say(f"#{article_id} 要重签（{reason}）")
            continue
        if not _try_lock(conn, article_id):
            found["busy"].append(article_id)
            continue
        try:
            result = translate_article(conn, article_id, data_dir=settings.data_dir, offline=True)
        except Exception as exc:  # noqa: BLE001 — 一篇出错不停
            conn.rollback()
            found["failed"].append(article_id)
            say(f"#{article_id} 重签出错（{reason}）：{type(exc).__name__}: {exc}")
            continue
        finally:
            _unlock(conn, article_id)
        if result.status == "skipped":
            found["needs-model"].append(article_id)
            say(f"#{article_id} 要调模型才补得上（{reason}，{result.untranslated} 段）")
        else:
            found["resigned"].append(article_id)
            say(f"#{article_id} 重签了（{reason}）")
    found["checked"] = checked
    found["reasons"] = {str(key): value for key, value in reasons.items()}
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="不调模型重签中英对照回执；要调模型的列出来")
    parser.add_argument("--article-id", type=int, action="append", default=[], help="只看这几篇，可重复")
    parser.add_argument("--dry-run", action="store_true", help="只列出要重签的篇，不写库、不写状态文件")
    parser.add_argument("--limit", type=int, default=None, help="最多重签或列出这么多篇")
    args = parser.parse_args(argv)
    settings = Settings.from_env()
    conn = connect(settings)
    try:
        ensure_search_session(conn)
        conn.commit()
        found = run(conn, settings, ids=list(dict.fromkeys(args.article_id)) or None, dry_run=args.dry_run,
                    limit=args.limit, say=lambda text: print(text, flush=True))
    finally:
        conn.close()
    if args.dry_run:
        print(f"看了 {found['checked']} 篇，要重签 {len(found.get('would', []))} 篇")
        return 0
    needs, failed = found["needs-model"], found["failed"]
    line = (f"看了 {found['checked']} 篇：重签 {len(found['resigned'])} 篇，要调模型才补得上 {len(needs)} 篇，"
            f"出错 {len(failed)} 篇，别处正在译跳过 {len(found['busy'])} 篇")
    print(line)
    if needs:
        print("要调模型的（几段，费用很小；要补就跑）：research-translate "
              + " ".join(f"--article-id {article_id}" for article_id in needs))
    if not found.get("stopped"):
        problem = ""
        if needs:
            problem = (f"{len(needs)} 篇对照页要调模型才补得上（"
                       + "、".join(f"#{article_id}" for article_id in needs[:10])
                       + ("…" if len(needs) > 10 else "") + "），见 research-translate-resign 的输出")
        if failed:
            problem += ("；" if problem else "") + f"{len(failed)} 篇重签出错"
        write_status(settings.data_dir, TASK, not problem, problem or line,
                     needsModel=needs, failed=failed, resigned=len(found["resigned"]))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
