"""持仓和观察名单（config/portfolio.yaml）。按中文名对应公司表。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

HOLDING = "holding"
WATCH = "watch"
ROLE_LABELS = {HOLDING: "持仓", WATCH: "观察"}


@dataclass(frozen=True)
class Portfolio:
    holdings: tuple[str, ...] = ()
    watch: tuple[str, ...] = ()
    errors: tuple[str, ...] = field(default_factory=tuple)

    @property
    def names(self) -> tuple[str, ...]:
        return self.holdings + tuple(name for name in self.watch if name not in self.holdings)

    def role(self, name_zh: str) -> str:
        if name_zh in self.holdings:
            return HOLDING
        if name_zh in self.watch:
            return WATCH
        return ""


def _names(raw, key: str, errors: list[str]) -> tuple[str, ...]:
    value = raw.get(key) or []
    if not isinstance(value, list):
        errors.append(f"{key} 要是列表")
        return ()
    names: list[str] = []
    for item in value:
        name = str(item or "").strip()
        if name and name not in names:
            names.append(name)
    return tuple(names)


def load_portfolio(path: Path) -> Portfolio:
    """文件不存在就当没有持仓；格式不对不报错退出，把问题放进 errors 让页面提示。"""
    if not path.exists():
        return Portfolio()
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        return Portfolio(errors=(f"portfolio.yaml 读不了：{exc}",))
    if not isinstance(raw, dict):
        return Portfolio(errors=("portfolio.yaml 顶层要是 holdings / watch 两个列表",))
    errors: list[str] = []
    holdings = _names(raw, "holdings", errors)
    watch = tuple(name for name in _names(raw, "watch", errors) if name not in holdings)
    return Portfolio(holdings=holdings, watch=watch, errors=tuple(errors))
