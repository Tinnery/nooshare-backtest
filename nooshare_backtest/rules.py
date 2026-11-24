"""A 股交易规则（按执行日适用）。

首期规则：
- T+1：当日买入的证券当日不可卖出；
- 停牌（volume=0 或 is_suspended）不可买、不可卖；
- 涨停不可买、跌停不可卖；
- ST 不可买（卖出不受限）；
- 假跳空防御：|ret| > fake_gap_limit 的当日不参与成交（与 Strategy 基线 21% 口径一致）。

规则只输出「可否 + 原因」，不改动数据。原因字符串进入 Order.reason / TargetDeviation.reason，
保证未成交可解释。
"""
from __future__ import annotations

from dataclasses import dataclass

from .models import Bar

ALLOW = ""
R_T_PLUS_1 = "T_PLUS_1"
R_SUSPENDED = "SUSPENDED"
R_LIMIT_UP = "LIMIT_UP"
R_LIMIT_DOWN = "LIMIT_DOWN"
R_ST = "ST"
R_FAKE_GAP = "FAKE_GAP"
R_NO_DATA = "NO_DATA"


@dataclass(frozen=True)
class RuleSet:
    fake_gap_limit: float = 0.21
    enable_t_plus_1: bool = True
    enable_st_filter: bool = True

    def _sane(self, bar: Bar) -> bool:
        """假跳空防御：异常收益日不参与成交。"""
        if bar.ret is None:
            return True
        return abs(bar.ret) <= self.fake_gap_limit

    def can_buy(self, bar: Bar | None) -> str:
        if bar is None:
            return R_NO_DATA
        if not self._sane(bar):
            return R_FAKE_GAP
        if bar.is_suspended or (bar.volume is not None and bar.volume <= 0):
            return R_SUSPENDED
        if bar.is_limit_up:
            return R_LIMIT_UP
        if self.enable_st_filter and bar.is_st:
            return R_ST
        return ALLOW

    def can_sell(self, bar: Bar | None, bought_today: bool) -> str:
        if bar is None:
            return R_NO_DATA
        if not self._sane(bar):
            return R_FAKE_GAP
        if self.enable_t_plus_1 and bought_today:
            return R_T_PLUS_1
        if bar.is_suspended or (bar.volume is not None and bar.volume <= 0):
            return R_SUSPENDED
        if bar.is_limit_down:
            return R_LIMIT_DOWN
        return ALLOW
