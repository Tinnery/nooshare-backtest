"""A 股 NEXT_OPEN 执行规则。

价格限制只根据 Raw open、交易所涨跌停参考价（除权除息日需调整）和调用方解析的执行日 PIT 比例判断。
收盘 is_limit_up/is_limit_down 标记不代表当日开盘不可成交；HFQ return 也不参与执行拒绝。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from math import isfinite

from .models import Bar

ALLOW = ""
R_T_PLUS_1 = "T_PLUS_1"
R_SUSPENDED = "SUSPENDED"
R_LIMIT_UP = "OPEN_LIMIT_UP"
R_LIMIT_DOWN = "OPEN_LIMIT_DOWN"
R_OPEN_LIMIT_UP = R_LIMIT_UP
R_OPEN_LIMIT_DOWN = R_LIMIT_DOWN
R_ST = "ST"
R_FAKE_GAP = "FAKE_GAP"  # Legacy reason constant; no longer emitted.
R_NO_DATA = "NO_MARKET_DATA"
R_NO_RAW_OPEN = "NO_RAW_OPEN"
R_NO_PRE_CLOSE = "NO_RAW_PRE_CLOSE"
R_UNRESOLVED_LIMIT_RULE = "UNRESOLVED_LIMIT_RULE"


def _positive_finite(value: float | None) -> bool:
    try:
        return value is not None and isfinite(float(value)) and float(value) > 0
    except (TypeError, ValueError):
        return False


def _money(value: float) -> Decimal:
    """Exchange price precision: round to the nearest fen using half-up rounding."""
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _valid_ratio(value: float | None) -> bool:
    try:
        return value is not None and isfinite(float(value)) and 0 < float(value) < 1
    except (TypeError, ValueError):
        return False


@dataclass(frozen=True)
class RuleSet:
    # Kept as a deprecated constructor argument for old Strategy callers. It is ignored.
    # Price-gap data quality checks must not be used as exchange execution rules.
    fake_gap_limit: float = 0.21
    enable_t_plus_1: bool = True
    enable_st_filter: bool = True

    def _open_limit_reason(self, bar: Bar, side: str) -> str:
        if not bar.limit_rule_source:
            return R_UNRESOLVED_LIMIT_RULE
        if bar.no_limit:
            return ALLOW
        if not _positive_finite(bar.pre_close):
            return R_NO_PRE_CLOSE
        if not _valid_ratio(bar.limit_up_ratio) or not _valid_ratio(bar.limit_down_ratio):
            return R_UNRESOLVED_LIMIT_RULE
        try:
            up = _money(float(bar.pre_close) * (1.0 + float(bar.limit_up_ratio)))
            down = _money(float(bar.pre_close) * (1.0 - float(bar.limit_down_ratio)))
            opening = _money(float(bar.open))
        except (InvalidOperation, TypeError, ValueError):
            return R_UNRESOLVED_LIMIT_RULE

        if side == "BUY" and opening >= up:
            return R_OPEN_LIMIT_UP
        if side == "SELL" and opening <= down:
            return R_OPEN_LIMIT_DOWN
        return ALLOW

    def can_buy(self, bar: Bar | None) -> str:
        if bar is None:
            return R_NO_DATA
        if not _positive_finite(bar.open):
            return R_NO_RAW_OPEN
        if bar.is_suspended or (bar.volume is not None and bar.volume <= 0):
            return R_SUSPENDED
        limit_reason = self._open_limit_reason(bar, "BUY")
        if limit_reason:
            return limit_reason
        if self.enable_st_filter and bar.is_st:
            return R_ST
        return ALLOW

    def can_sell(self, bar: Bar | None, bought_today: bool) -> str:
        if bar is None:
            return R_NO_DATA
        if self.enable_t_plus_1 and bought_today:
            return R_T_PLUS_1
        if not _positive_finite(bar.open):
            return R_NO_RAW_OPEN
        if bar.is_suspended or (bar.volume is not None and bar.volume <= 0):
            return R_SUSPENDED
        limit_reason = self._open_limit_reason(bar, "SELL")
        if limit_reason:
            return limit_reason
        return ALLOW
