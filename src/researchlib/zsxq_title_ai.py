"""知识星球标题：用站内 DeepSeek flash 起题（空/壳/省略号/无标题）。

只在入库后回写、增量抓取后补跑时调用；卡片现算仍走 zsxq_title 的确定性规则。
明显错字在标题里一并改正，不改观点和口语。
"""

from __future__ import annotations

import re

from researchlib.config import Settings
from researchlib.llm import LLMError, LLMUnavailable, chat, llm_configured
from researchlib.zsxq_title import (
    TITLE_LIMIT,
    polish_title_typos,
    strip_narrator,
    title_needs_repair,
    unwrap_title,
)

PROMPT = """根据下面这篇知识星球帖子的正文、图片里的字和导读，写一个中文标题。
只输出一行标题，不要引号、不要前后缀、不要解释。

要求：
- 一般不超过 40 个字，概括这篇在讲什么
- 改正明显错字和语病（例如「很新都东西」写成「很新的东西」）；不改观点，不改口语语气
- 不要空标题，不要「无标题」「未命名」「图片」「见图」「短帖」
- 不要以省略号（…或...）开头或结尾
- 不要只写公司名或话题名加冒号（例如「泡泡玛特：」「西部矿业：」）
- 不要把评论列表的界面文字写进标题（例如「评论（1）」「 - 王小明（2026-10-28」）
- 不要复述「正文为空」「文中未给」「作者开篇判断」这类套话
- 用帖子自己的口吻直接写出判断，不要写成「作者认为」「方伟判断」「作者以……为背景，主张……」这种转述
"""

_LABEL = re.compile(r"^(?:标题|title)\s*[：:=]\s*", re.I)


def generate_ai_title(
    settings: Settings,
    *,
    body: str = "",
    ocr: str = "",
    digest: str = "",
    when: str = "",
    old_title: str = "",
) -> str | None:
    """调站内 LLM 起题。失败或不合格返回 None，由调用方退回确定性规则。"""
    if not llm_configured(settings):
        return None
    material = _material(body=body, ocr=ocr, digest=digest, when=when, old_title=old_title)
    if not material.strip():
        return None
    try:
        text, _model = chat(
            settings,
            [
                {"role": "system", "content": PROMPT},
                {"role": "user", "content": material},
            ],
            temperature=0.1,
            exact_max_tokens=512,
        )
    except (LLMUnavailable, LLMError):
        return None
    return accept_ai_title(text)


def accept_ai_title(raw: str) -> str | None:
    """校验模型输出：≤40 字、无占位、无省略号壳、无公司名冒号壳。转述的引子（「作者认为」）去掉再用。"""
    line = (raw or "").strip().splitlines()[0].strip() if (raw or "").strip() else ""
    line = _LABEL.sub("", line)
    # 只去包住整行的那一对。以前两头的符号一律去掉，「《穷查理宝典》读后感」会剩下落单的「》」。
    line = unwrap_title(line)
    line = polish_title_typos(line)
    line = re.sub(r"\s+", " ", line).strip().rstrip("。.")
    line = strip_narrator(line)
    if not line or len(line) > TITLE_LIMIT:
        return None
    if title_needs_repair(line):
        return None
    return line


def _material(*, body: str, ocr: str, digest: str, when: str, old_title: str) -> str:
    parts: list[str] = []
    if when.strip():
        parts.append(f"日期：{when.strip()}")
    if old_title.strip():
        parts.append(f"原标题：{old_title.strip()}")
    if digest.strip():
        parts.append("导读：\n" + digest.strip()[:800])
    if body.strip():
        parts.append("正文：\n" + body.strip()[:4000])
    if ocr.strip():
        parts.append("图片里的字：\n" + ocr.strip()[:4000])
    return "\n\n".join(parts)
