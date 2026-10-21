"""对照 inbox 里的 Markdown 和库里还看得见的条目，找源有实质回答、库里没有的缺口。"""

from __future__ import annotations

from pathlib import Path

from researchlib.empty_answer import suppression_for
from researchlib.ingest.duanyongping import SOURCE_ID
from researchlib.textutil import split_frontmatter


def source_voice(body_md: str) -> str:
    front, _body = split_frontmatter(body_md or "")
    voice = str((front or {}).get("author_voice") or "").strip()
    if voice:
        return voice
    if "## 段永平" in (body_md or ""):
        return "段永平"
    return "all"


def source_is_substantive(body_md: str) -> bool:
    """源文件里这段该出现在列表里：不是导航壳、没有立场的短帖，也不是英文粘贴。"""
    voice = source_voice(body_md)
    return suppression_for(body_md, {"author_voice": voice}, source_id=SOURCE_ID) == ""


def missing_visible(docs: list[tuple[str, str, str]], visible_ids: set[str]) -> list[dict[str, str]]:
    """docs 是 (外部 id, 标题, 正文)。visible_ids 是库里没搁下的外部 id。"""
    missing: list[dict[str, str]] = []
    for external_id, title, body in docs:
        if not external_id or not source_is_substantive(body):
            continue
        if external_id not in visible_ids:
            missing.append({"id": external_id, "title": title})
    return missing


def load_markdown_docs(folder: Path) -> list[tuple[str, str, str]]:
    docs: list[tuple[str, str, str]] = []
    if not folder.is_dir():
        return docs
    for path in sorted(folder.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        front, _body = split_frontmatter(text)
        external_id = str((front or {}).get("id") or path.stem).strip()
        title = str((front or {}).get("title") or "").strip()
        docs.append((external_id, title, text))
    return docs
