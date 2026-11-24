"""策略决策适配：只接收目标组合，不产生策略。"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any

from ..models import TargetPortfolio


def _as_date(value: Any) -> int:
    if isinstance(value, int):
        return value
    return int(str(value).replace("-", "").replace("/", ""))


def normalize_targets(source: Mapping[int, Any] | Iterable[Any]) -> dict[int, TargetPortfolio]:
    """支持：{date: TargetPortfolio}、{date: weights}、TargetPortfolio 列表、记录 dict 列表。"""
    out: dict[int, TargetPortfolio] = {}
    if isinstance(source, Mapping):
        for date, item in source.items():
            d = _as_date(date)
            if isinstance(item, TargetPortfolio):
                out[d] = item
            elif isinstance(item, Mapping) and "weights" in item:
                out[d] = TargetPortfolio(
                    decision_date=d, weights=dict(item["weights"]),
                    version=str(item.get("version", "unknown")),
                )
            else:
                out[d] = TargetPortfolio(decision_date=d, weights=dict(item))
        return out

    for item in source:
        if isinstance(item, TargetPortfolio):
            out[item.decision_date] = item
        else:
            d = _as_date(item["decision_date"])
            out[d] = TargetPortfolio(
                decision_date=d, weights=dict(item["weights"]),
                version=str(item.get("version", "unknown")),
            )
    return out


def targets_from_callable(
    fn: Callable[[int], Mapping[str, float]],
    decision_dates: Iterable[int],
    version: str = "unknown",
) -> dict[int, TargetPortfolio]:
    """把按日调用的策略函数包装为目标组合（策略仍由外部拥有）。"""
    return {
        d: TargetPortfolio(decision_date=d, weights=dict(fn(d)), version=version)
        for d in decision_dates
    }
