"""粗判正文语言：中文来源不进译制管线。"""

from __future__ import annotations


def is_chinese(sample: str, *, language: str | None = None) -> bool:
    if language == "zh":
        return True
    if language == "en":
        return False
    head = (sample or "")[:400]
    han = len([ch for ch in head if "\u4e00" <= ch <= "\u9fff"])
    latin = len([ch for ch in head if ("A" <= ch <= "Z") or ("a" <= ch <= "z")])
    return han > 0 and han * 2 >= latin
