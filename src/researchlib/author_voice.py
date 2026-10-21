"""一篇里同时有读者提问和作者本人的话时，观点和摘要只读作者那一节。

段永平的问答整条入库：读者的问题和别人的回复留在正文里，检索、作者页、公司页看得到。
交给模型抽观点、写摘要时只留他本人写的段落，避免把提问者的看法算成他的判断。
"""

from __future__ import annotations

import json

# 有日期的进首页按天。没日期的文章带上这个标记，首页按天不收，作者、公司、检索仍收。
OFF_TIMELINE = "off_timeline"
# "all"：整篇都是作者的话。其它值：只取这个 Markdown 章节标题下的段落。
AUTHOR_VOICE = "author_voice"
VOICE_ALL = "all"
# 抽观点、写摘要时附在提示里，只在收窄到作者本人时加上。
VOICE_NOTE = "\n（下面只保留段永平本人写的段落。读者提问和别人的回复没有放进来，不要当成他的判断。）"


def raw_meta_of(conn, row) -> dict:
    """文章的 raw_meta。查询没带这一列时再读一次。"""
    keys = row.keys() if hasattr(row, "keys") else ()
    if "raw_meta" in keys:
        meta = row["raw_meta"]
    elif conn is not None and row.get("id"):
        found = conn.execute("SELECT raw_meta FROM articles WHERE id = %s", (row["id"],)).fetchone()
        meta = found["raw_meta"] if found else None
    else:
        meta = None
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except ValueError:
            return {}
    return meta if isinstance(meta, dict) else {}


def restrict(
    body_text: str, chunks: list[tuple[str, str]], raw_meta: dict | None
) -> tuple[str, list[tuple[str, str]], bool]:
    """返回 (交给模型的正文, 对应的段落块, 是否只留了作者本人)。

    author_voice 缺省或 all：整篇。否则只留章节标题等于这个值的块。
    """
    voice = str((raw_meta or {}).get(AUTHOR_VOICE) or "").strip()
    if not voice or voice == VOICE_ALL:
        return body_text or "", list(chunks), False
    picked = [(heading, text) for heading, text in chunks if (heading or "").strip() == voice and (text or "").strip()]
    text = "\n\n".join(piece for _heading, piece in picked)
    return text, picked, True
