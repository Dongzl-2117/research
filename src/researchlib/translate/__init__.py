"""英文文章站内 Grok 译制与中英对照。"""

from researchlib.translate.language import is_chinese
from researchlib.translate.pair import bilingual_pairs
from researchlib.translate.service import translate_article

__all__ = ["bilingual_pairs", "is_chinese", "translate_article"]
