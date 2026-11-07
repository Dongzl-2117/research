"""外链图片下载以后，已经译过的文章照常逐段对照。

译文回执按译制时的 canonical 原文核验，原文里的图片也算在内。外链图片（Stratechery 的
i0.wp.com）以前一张都没下下来，原文里是「[外部图片已省略]」；下下来以后同一处换成了 ``<img>``，
canonical 变了，回执对不上，对照页就退成「保留两份全文」。所以译制和核验都用不带下载来的图的
那一份原文（``plain``），配对和显示用现在的原文（``full``）：译文里的占位按顺序换成原文现在的图，
和图在译制前就下好了的译文一样配对。占位个数对不上时不换，对照照旧显示占位，「原文」页仍有图。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass

# render.rewrite_markdown_images 给没下载的外链图写 ``*[外部图片已省略]*``，Markdown 转出来是这个样子。
PLACEHOLDER = "<em>[外部图片已省略]</em>"
# 占位在英文段落中间时整段送模型，括号可能被写成全角。
_TRANSLATED_PLACEHOLDER = re.compile(r"<em>\s*[\[【]\s*外部图片已省略\s*[\]】]\s*</em>")
# 下载来的图存在 data/images/remote/，页面地址是 /media/remote/…。
_SLOT = re.compile(re.escape(PLACEHOLDER) + r'|<img\b[^>]*\bsrc="/media/remote/[^"]*"[^>]*>', re.I)


@dataclass(frozen=True)
class PairSources:
    check: str  # 核验回执用的 canonical 原文
    original: str  # 配对用的 canonical 原文
    translated: str  # 配对和「中文」页用的译文


def _translated_against_plain(full: str, plain: str, translated: str, provenance: dict | None) -> bool:
    """译文是照着不带下载图的原文译的。

    有回执时看回执里存的译制原文有没有下载来的图；核验本身留给 article_pairs，这里不再算一遍
    （一篇长文核验一次要一两百毫秒）。旧稿没有回执，看译文里的占位个数。
    """
    if provenance:
        return "/media/remote/" not in str(provenance.get("source_snapshot") or "")
    holes = len(_TRANSLATED_PLACEHOLDER.findall(translated))
    return holes == plain.count(PLACEHOLDER) and holes > full.count(PLACEHOLDER)


def pair_sources(full: str, plain: str, translated: str, provenance: dict | None) -> PairSources:
    """``full`` 带下载来的图，``plain`` 不带。两份一样时照常配对。"""
    if plain == full or not _translated_against_plain(full, plain, translated, provenance):
        return PairSources(full, full, translated)
    slots = [match.group(0) for match in _SLOT.finditer(full)]
    holes = _TRANSLATED_PLACEHOLDER.findall(translated)
    if len(slots) != plain.count(PLACEHOLDER) or len(holes) != len(slots):
        # 有不是从占位换来的图（正文 HTML 里的 <img>），或者译文少了占位：位置对不上，对照照旧用占位那一份。
        return PairSources(plain, plain, translated)
    shown = iter(slots)
    return PairSources(plain, full, _TRANSLATED_PLACEHOLDER.sub(lambda _match: next(shown), translated))


def mappings(images: Sequence[dict]) -> tuple[dict[str, str], dict[str, str] | None]:
    """article_images 的行 → (全部图的映射, 去掉下载来的图的映射)。没有下载来的图时第二项是 None。"""
    full = {row["original_ref"]: f"/media/{row['storage_path']}" for row in images}
    if not any(row.get("remote") for row in images):
        return full, None
    return full, {row["original_ref"]: f"/media/{row['storage_path']}" for row in images if not row.get("remote")}


def bilingual_sources(
    render: Callable[[dict[str, str]], str], images: Sequence[dict], translated: str, provenance: dict | None,
) -> tuple[str, PairSources]:
    """``render(mapping)`` 出 canonical 原文。返回 (「原文」页用的原文, 对照的输入)。"""
    full_mapping, plain_mapping = mappings(images)
    full = render(full_mapping)
    if plain_mapping is None or not translated:
        return full, PairSources(full, full, translated)
    return full, pair_sources(full, render(plain_mapping), translated, provenance)
