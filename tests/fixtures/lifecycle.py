"""可手工计算的交易生命周期 Fixture（P2/P3/P9）。

覆盖：涨停拒绝、资金不足部分买入、部分卖出、多批 Entry+T+1、
完整 Trade Lifecycle、开放持仓、最后交易日决策。
"""
from __future__ import annotations

from nooshare_backtest.models import Bar

D1, D2, D3, D4, D5, D6 = 20240102, 20240103, 20240104, 20240105, 20240108, 20240109


def _bar(date, symbol, o, c, *, volume=1_000_000, ret=0.0,
         is_limit_up=False, is_limit_down=False, is_suspended=False,
         pre_close=None, canonical_id="", exchange="") -> Bar:
    if pre_close is None:
        pre_close = o / 1.10 if is_limit_up else (o / 0.90 if is_limit_down else o)
    return Bar(date=date, symbol=symbol, open=o, high=max(o, c) * 1.01,
               low=min(o, c) * 0.99, close=c, volume=volume, ret=ret,
               pre_close=pre_close, limit_up_ratio=0.10, limit_down_ratio=0.10,
               limit_rule_source="fixture_pit",
               is_limit_up=is_limit_up, is_limit_down=is_limit_down,
               is_suspended=is_suspended,
               canonical_id=canonical_id or symbol[-6:],
               exchange=exchange or ("sh" if symbol.startswith("sh") else "sz"))


def normal_bars() -> dict[int, dict[str, Bar]]:
    """D1 收盘决策买入，D2 开盘买入，D3 开盘卖出：一次完整正常交易。"""
    s = "sh600519"
    return {
        D1: {s: _bar(D1, s, 10.0, 10.0)},
        D2: {s: _bar(D2, s, 10.0, 10.2)},
        D3: {s: _bar(D3, s, 10.2, 10.4)},
        D4: {s: _bar(D4, s, 10.4, 10.4)},
    }


def limit_up_bars() -> dict[int, dict[str, Bar]]:
    s = "sh600519"
    return {
        D1: {s: _bar(D1, s, 10.0, 11.0)},
        D2: {s: _bar(D2, s, 11.0, 11.0, is_limit_up=True)},
        D3: {s: _bar(D3, s, 11.0, 11.0)},
    }


def multi_lot_bars() -> dict[int, dict[str, Bar]]:
    """D1 买 1000，D2 买 500，D3 卖 1000（只能卖 D1 批次），验证 T+1 批次。"""
    s = "sh600519"
    return {
        D1: {s: _bar(D1, s, 10.0, 10.0)},
        D2: {s: _bar(D2, s, 10.0, 10.0)},
        D3: {s: _bar(D3, s, 10.0, 10.0)},
        D4: {s: _bar(D4, s, 10.0, 10.0)},
        D5: {s: _bar(D5, s, 10.0, 10.0)},
    }


def open_position_bars() -> dict[int, dict[str, Bar]]:
    s = "sh600519"
    return {
        D1: {s: _bar(D1, s, 10.0, 10.0)},
        D2: {s: _bar(D2, s, 10.0, 10.0)},
        D3: {s: _bar(D3, s, 10.0, 10.0)},
    }
