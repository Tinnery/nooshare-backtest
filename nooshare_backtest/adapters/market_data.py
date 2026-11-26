"""市场数据适配：把外部记录归一化为 Bar 并按交易日索引。"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

from ..models import Bar


def _as_date(value: Any) -> int:
    if isinstance(value, int):
        return value
    return int(str(value).replace("-", "").replace("/", ""))


def normalize_bars(source: Mapping[int, Any] | Iterable[Any]) -> dict[int, dict[str, Bar]]:
    """支持：{date:{symbol:Bar}}、Bar 列表、记录 dict 列表。"""
    if isinstance(source, Mapping):
        out: dict[int, dict[str, Bar]] = {}
        for date, day in source.items():
            d = _as_date(date)
            out.setdefault(d, {})
            for symbol, bar in day.items():
                out[d][symbol] = bar if isinstance(bar, Bar) else Bar(**dict(bar))
        return out

    out = {}
    for item in source:
        bar = item if isinstance(item, Bar) else Bar(
            date=_as_date(item["date"]),
            symbol=str(item["symbol"]),
            open=float(item["open"]), high=float(item["high"]),
            low=float(item["low"]), close=float(item["close"]),
            volume=(float(item["volume"]) if item.get("volume") is not None else None),
            ret=(float(item["ret"]) if item.get("ret") is not None else None),
            mark_price=(float(item["mark_price"]) if item.get("mark_price") is not None else None),
            pre_close=(float(item["pre_close"]) if item.get("pre_close") is not None else None),
            limit_up_price=(float(item["limit_up_price"]) if item.get("limit_up_price") is not None else None),
            limit_down_price=(float(item["limit_down_price"]) if item.get("limit_down_price") is not None else None),
            no_limit=bool(item.get("no_limit", False)),
            trading_days_since_list=(int(item["trading_days_since_list"]) if item.get("trading_days_since_list") is not None else None),
            list_date=(int(item["list_date"]) if item.get("list_date") is not None else None),
            is_suspended=bool(item.get("is_suspended", False)),
            is_limit_up=bool(item.get("is_limit_up", False)),
            is_limit_down=bool(item.get("is_limit_down", False)),
            is_st=bool(item.get("is_st", False)),
            canonical_id=str(item.get("canonical_id", "")),
            exchange=str(item.get("exchange", "")),
        )
        out.setdefault(bar.date, {})[bar.symbol] = bar
    return out
