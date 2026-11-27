"""手工可计算的小型 Fixture：少量交易日、两只股票、简单目标仓位。"""
from __future__ import annotations

from nooshare_backtest.models import Bar

D1, D2, D3, D4 = 20240102, 20240103, 20240104, 20240105


def sample_bars() -> dict[int, dict[str, Bar]]:
    raw = {
        D1: {"AAA": (10.0, 10.2, 9.9, 10.1, 0.01), "BBB": (20.0, 20.1, 19.9, 20.0, 0.0)},
        D2: {"AAA": (10.2, 10.5, 10.1, 10.4, 0.02), "BBB": (20.2, 20.5, 20.1, 20.4, 0.02)},
        D3: {"AAA": (10.4, 10.6, 10.2, 10.3, -0.01), "BBB": (20.4, 20.7, 20.3, 20.6, 0.01)},
        D4: {"AAA": (10.3, 10.6, 10.2, 10.5, 0.02), "BBB": (20.6, 20.9, 20.5, 20.8, 0.01)},
    }
    return {
        date: {
            sym: Bar(date=date, symbol=sym, open=o, high=h, low=l, close=c,
                     volume=1_000_000, ret=r, pre_close=o,
                     limit_up_ratio=0.10, limit_down_ratio=0.10,
                     limit_rule_source="fixture_pit")
            for sym, (o, h, l, c, r) in day.items()
        }
        for date, day in raw.items()
    }


def sample_decisions() -> dict[int, dict[str, float]]:
    return {
        D1: {"AAA": 0.5, "BBB": 0.3},   # D2 开盘执行
        D2: {"BBB": 0.3},               # D3 开盘卖出 AAA
        D3: {"AAA": 0.5, "BBB": 0.3},   # D4 开盘重新买入
        D4: {"AAA": 0.4},               # 最后一日决策，无下一交易日
    }


# 手工计算：初始现金 1,000,000；D2 开盘 10.2 / 20.2；整手 100
# 1bp = 1e-4，故佣金 2.5bp = 0.00025，滑点 5bp = 0.0005
EXPECTED_AAA_BUY_SHARES = 49_000
EXPECTED_BBB_BUY_SHARES = 14_800
EXPECTED_AAA_BUY_CASH = -(49_000 * 10.2 * 1.0005 * 1.00025)
EXPECTED_BBB_BUY_CASH = -(14_800 * 20.2 * 1.0005 * 1.00025)
