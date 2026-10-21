"""把保留下来的条目写成现有 Markdown 导入器能吃的 frontmatter。"""

from __future__ import annotations

import json
from pathlib import Path

from researchlib.empty_answer import substantive_voice
from researchlib.ingest.duanyongping.segment import Entry


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def uses_sections(entry: Entry) -> bool:
    """有读者提问或别人的回复时，段永平的话单独成一节，观点和摘要只读这一节。"""
    return bool(entry.question.strip() or entry.others.strip())


def _his_own_essay(entry: Entry) -> bool:
    """没有读者提问的博文，正文就是他本人的话。转载不算。"""
    return bool(entry.author.strip()) and not uses_sections(entry) and not entry.repost


def render_markdown(entry: Entry) -> str:
    # 问答只读「段永平」那一节。他自己的博文整篇也标成他，避免长文被当成没有判断。
    voice = "段永平" if uses_sections(entry) or _his_own_essay(entry) else "all"
    lines = [
        "---",
        f"id: {_quote(entry.external_id)}",
        f"title: {_quote(entry.title)}",
        f"author_voice: {_quote(voice)}",
    ]
    if entry.published:
        lines.append(f"date: {_quote(entry.published)}")
    else:
        lines.append("off_timeline: true")
    if entry.off_views:
        # 补名单里的公司库里还没有，留着检索，但不进观点。
        lines.append("off_views: true")
    lines.append("---")
    lines.append("")
    if uses_sections(entry):
        if entry.question.strip():
            lines.extend(["## 读者问", "", entry.question.strip(), ""])
        # 回答末尾的「回本文开头」不写进正文。
        lines.extend(["## 段永平", "", substantive_voice(entry.author).strip(), ""])
        others = substantive_voice(entry.others).strip()
        if others:
            lines.extend(["## 其他人", "", others, ""])
    elif _his_own_essay(entry):
        lines.extend(["## 段永平", "", substantive_voice(entry.author).strip(), ""])
    else:
        lines.extend([entry.author.strip(), ""])
    return "\n".join(lines).rstrip() + "\n"


def write_markdown(entries: list[Entry], out: Path) -> list[Path]:
    """只写保留的条目。文件名是外部 id，不带日期，避免没日期的被文件名补上时间。"""
    out.mkdir(parents=True, exist_ok=True)
    for stale in out.glob("dyp-*.md"):
        stale.unlink()
    written: list[Path] = []
    kept = [entry for entry in entries if entry.keep]
    kept.sort(key=lambda entry: (entry.published or "9999", entry.external_id))
    for entry in kept:
        path = out / f"{entry.external_id}.md"
        path.write_text(render_markdown(entry), encoding="utf-8")
        written.append(path)
    return written


def report_payload(entries: list[Entry], *, source: str, origin: str, mode: str, posts: int) -> dict:
    kept = [entry for entry in entries if entry.keep]
    skipped = [entry for entry in entries if not entry.keep]

    def item(entry: Entry) -> dict:
        return {
            "id": entry.external_id,
            "title": entry.title,
            "post_title": entry.post_title,
            "date": entry.published,
            "feed": entry.feed,
            "keep": entry.keep,
            "kind": "问答" if uses_sections(entry) else "博文",
            "reason": entry.reason,
            "companies": entry.companies,
            "page": entry.page,
        }

    return {
        "source": source,
        "origin": origin,
        "mode": mode,
        "posts": posts,
        "seen": len(entries),
        "kept": len(kept),
        "skipped": len(skipped),
        "dated": sum(1 for entry in kept if entry.published),
        "undated": sum(1 for entry in kept if not entry.published),
        "items": [item(entry) for entry in entries],
    }


def render_report(payload: dict) -> str:
    lines = [
        f"来源: {payload['source']}",
        f"读取: {payload['origin']}（切分 {payload['mode']}，博文 {payload['posts']} 篇）",
        f"候选 {payload['seen']} 条，保留 {payload['kept']}，跳过 {payload['skipped']}",
        f"有日期 {payload['dated']}（进按天），无日期 {payload['undated']}（只进作者、公司、检索）",
        "",
        "保留:",
    ]
    kept = [item for item in payload["items"] if item["keep"]]
    skipped = [item for item in payload["items"] if not item["keep"]]
    if not kept:
        lines.append("  （无）")
    for item in kept:
        when = item["date"] or "无日期"
        where = "按天" if item["feed"] else "不进按天"
        companies = "、".join(item["companies"])
        lines.append(f"- {when}  {item['title']}  {item['kind']}  {where}  {companies}")
    lines.extend(["", "跳过:"])
    if not skipped:
        lines.append("  （无）")
    for item in skipped:
        title = item["post_title"] or item["title"] or "未命名"
        lines.append(f"- {title}  {item['reason']}")
    return "\n".join(lines)


def write_report(payload: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
