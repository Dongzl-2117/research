"""译制闭环里数字这一关怎么处理。

结构问题（链接、图片、代码、条数、空段）照旧直接拒绝。数字不靠词表一票通过，
也不靠模型自己说通过。检查器仍然要跑：

- 能证明的冲突（数量级变了、译文里出现原文没有的硬数字、两边都写成了时段但单位不同）
  先把原文、译文和事实列表给模型修一次。修好并通过同一套检查才留下；修不好就拒绝，不入库。
- 对不齐、但又证明不了数字被改错（说法不同、抽取出的种类不同、顺序不同）同样只修一次。
  修好并通过检查才留下；否则记成 doubt，不入库，也不覆盖已经写好的译文。

模型只负责改一稿。过不过由 ``leaf_problem`` 决定。
"""

from __future__ import annotations

from pathlib import Path

from researchlib.translate.llm import LLMTransportError, output_tokens
from researchlib.translate.numbers import _extract
from researchlib.translate.prompts import SYSTEM_BODY
from researchlib.translate.provenance import leaf_problem, numeric_anchors

_REPAIR_NOTE = (
    "这一段没有通过数字对齐。不要自己宣布通过。"
    "只有数字、单位、数量级或先后确实和原文不一致时才改译文；"
    "若只是说法不同，把译文原样放回 t。"
    "只输出 JSON，t 里只能有这一段。"
)


def _atoms(html: str, *, hard_only: bool) -> list[str]:
    """硬事实里的数。时段带上单位；比例拆成两个数。软事实只在供给一侧使用。"""
    found: list[str] = []
    for fact in _extract(html):
        if hard_only and not fact.hard:
            continue
        if fact.kind == "duration":
            unit, _, count = fact.key.partition(":")
            if count:
                found.append(f"{unit}:{count}")
                found.append(count)
            continue
        if fact.kind == "ratio" and "/" in fact.key:
            found.extend(part for part in fact.key.split("/") if part)
            continue
        if fact.kind in {"qty", "literal", "pct", "soft", "year"}:
            found.append(fact.key)
    return found


def _unit_clash(source: str, target: str) -> bool:
    """两边都写成了时段，个数相同、单位不同。裸数字对上时段不算已证明的单位错误。"""
    source_units: dict[str, set[str]] = {}
    for fact in _extract(source):
        if fact.hard and fact.kind == "duration" and ":" in fact.key:
            unit, count = fact.key.split(":", 1)
            source_units.setdefault(count, set()).add(unit)
    for fact in _extract(target):
        if not (fact.hard and fact.kind == "duration" and ":" in fact.key):
            continue
        unit, count = fact.key.split(":", 1)
        units = source_units.get(count)
        if units and unit not in units:
            return True
    return False


def _missing_hard_number(source: str, target: str) -> bool:
    """译文的硬数字在原文的硬事实和软事实里都找不到。"""
    supply = _atoms(source, hard_only=False)
    for atom in _atoms(target, hard_only=True):
        if atom in supply:
            supply.remove(atom)
            continue
        if ":" in atom:
            count = atom.split(":", 1)[1]
            if count in supply:
                continue
        return True
    return False


def numeric_verdict(source: str, target: str) -> str:
    """``ok`` 检查通过；``scale`` / ``conflict`` 是能证明的数值错误；``doubt`` 只是对不齐。"""
    problem = leaf_problem(source, target)
    if problem is None:
        return "ok"
    if problem != "numbers":
        return "conflict"
    # 看得见的数字串一样，但带单位的金额对不上：10 billion 写成 10亿。
    anchors = numeric_anchors(source)
    if anchors and anchors == numeric_anchors(target):
        source_qty = [fact.key for fact in _extract(source) if fact.hard and fact.kind == "qty"]
        target_qty = [fact.key for fact in _extract(target) if fact.hard and fact.kind == "qty"]
        if source_qty != target_qty and (source_qty or target_qty):
            return "scale"
    if _unit_clash(source, target) or _missing_hard_number(source, target):
        return "conflict"
    return "doubt"


def fact_brief(source: str, target: str) -> str:
    """给日志和修复请求的事实列表。只留种类和键，不把整段正文再抄进错误码。"""

    def brief(html: str) -> str:
        bits = []
        for fact in _extract(html):
            mark = "" if fact.hard else "~"
            bits.append(f"{mark}{fact.kind}:{fact.key}")
            if len(bits) >= 12:
                break
        return " ".join(bits)

    return f"{brief(source)} => {brief(target)}"[:240]


def repair_slot(source: str, draft: str, data_dir: Path) -> str:
    """带着这一段的原文、译文和事实列表再要一稿。模型输出不合格就留着原译文，交给检查器。"""
    user = {
        "segments": [source],
        "repair": _REPAIR_NOTE + "\n事实: " + fact_brief(source, draft) + "\n译文:\n" + draft[:4000],
    }
    # 走 service 上的 chat_json，和第一次生成同一条入口，测试替换也覆盖这一次。
    import researchlib.translate.service as translate_service

    result = translate_service.chat_json(
        system=SYSTEM_BODY,
        user=user,
        purpose="translate_body",
        subject="batch",
        temperature=0.3,
        max_tokens=min(output_tokens(), max(8000, len(draft) * 2 + 800)),
        data_dir=data_dir,
    )
    raw = result.data.get("t") if isinstance(result.data, dict) else None
    if not isinstance(raw, list) or len(raw) != 1 or not isinstance(raw[0], str) or not raw[0].strip():
        return draft
    return raw[0]


def settle_numbers(source: str, draft: str, data_dir: Path) -> tuple[str, str | None]:
    """返回 (留下的译文, 仍要拒绝的错误码)。错误码为空表示检查通过。"""
    if leaf_problem(source, draft) is None:
        return draft, None
    try:
        revised = repair_slot(source, draft, data_dir)
    except LLMTransportError:
        raise
    if leaf_problem(source, revised) is None:
        return revised, None
    verdict = numeric_verdict(source, revised)
    if verdict == "ok":
        return revised, None
    code = "numbers-scale" if verdict == "scale" else ("numbers" if verdict == "conflict" else "numbers-doubt")
    return revised, code
