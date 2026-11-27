"""精确版止盈止损：开盘越过 vs 盘中触及的成交价格验证。

精确版逻辑（用户裁决 2026-11-27）：
- 开盘价 >= 止盈价 → 以执行日开盘价成交（trigger_type="gap"）
- 盘中最高价 >= 止盈价，但开盘未越过 → 以止盈价成交（trigger_type="touch"）
- 止损同理：开盘价 <= 止损价 → gap；最低价 <= 止损价但开盘未越过 → touch
"""
import pytest
from nooshare_backtest.engine import run, EngineConfig
from nooshare_backtest.models import Bar, TargetPortfolio


SYM = "sh600001"


def _bar(date, open, high, low, close):
    return Bar(
        date=date, symbol=SYM, open=open, high=high, low=low, close=close,
        volume=1_000_000,
        canonical_id="CAN001", exchange="SSE",
    )


def test_take_profit_gap_open_above_threshold():
    """T 日收盘判定止盈触发，T+1 开盘价 > 止盈价 → 以 T+1 开盘价成交。"""
    # 建仓价 10.0，止盈 10%，阈值 11.0
    # T1: 买入
    # T2: 最高 10.5 < 11.0，不触发
    # T3: 最高 11.5 >= 11.0，开盘 10.8 < 11.0 → touch
    # T4: 执行日，开盘 12.0 > 11.0 → 以 12.0 成交（gap 语义：开盘越过）
    # 等等，这里有个问题：trigger_type 是在决策日（T3）判定的，
    # 但成交价格是在执行日（T4）决定的。
    # 让我重新理清逻辑：
    # T3 收盘判定：开盘 10.8 < 11.0（未越过），最高 11.5 >= 11.0（触及）→ trigger_type="touch"
    # T4 执行：trigger_type="touch" → 以阈值价 11.0 成交
    bars = {
        1: {SYM: _bar(1, 10.0, 10.5, 9.8, 10.2)},
        2: {SYM: _bar(2, 10.2, 10.5, 10.0, 10.3)},  # 不触发
        3: {SYM: _bar(3, 10.8, 11.5, 10.5, 11.2)},  # 触发：开盘 10.8 < 11.0，最高 11.5 >= 11.0 → touch
        4: {SYM: _bar(4, 12.0, 12.5, 11.8, 12.2)},  # 执行日
    }

    def targets(ctx):
        if ctx.date == 1:
            return TargetPortfolio(decision_date=1, weights={SYM: 0.5})
        if ctx.date == 3:
            # 模拟 decision.py 的精确版逻辑
            pos = ctx.positions[0]
            ref = pos.first_entry_price  # ~10.0
            tp_px = ref * 1.10  # 11.0
            bar = ctx.bars[SYM]
            hit_take_gap = bar.open >= tp_px
            hit_take_touch = (not hit_take_gap) and (bar.high >= tp_px)
            if hit_take_touch:
                return TargetPortfolio(
                    decision_date=3, weights={},
                    exit_signals={SYM: ("TAKE_PROFIT", ref, 0.0, tp_px, "touch")},
                )
        return None

    result = run(bars, targets, EngineConfig(run_id="test_tp_touch"))
    sell_fills = [f for f in result.fills if f.side == "SELL"]
    assert len(sell_fills) == 1
    # touch: 以阈值价 11.0 成交，不是 T+1 开盘价 12.0
    assert sell_fills[0].price == pytest.approx(11.0, abs=0.01)


def test_take_profit_gap_open_at_threshold():
    """T+1 开盘价 >= 止盈价 → 以 T+1 开盘价成交（gap 语义）。"""
    # T3: 最高 11.5 >= 11.0，开盘 11.2 >= 11.0 → gap（开盘越过）
    # T4: 执行日 → 以 T4 开盘价成交
    bars = {
        1: {SYM: _bar(1, 10.0, 10.5, 9.8, 10.2)},
        2: {SYM: _bar(2, 10.2, 10.5, 10.0, 10.3)},
        3: {SYM: _bar(3, 11.2, 11.5, 10.5, 11.2)},  # 触发：开盘 11.2 >= 11.0 → gap
        4: {SYM: _bar(4, 11.8, 12.0, 11.5, 11.9)},  # 执行日
    }

    def targets(ctx):
        if ctx.date == 1:
            return TargetPortfolio(decision_date=1, weights={SYM: 0.5})
        if ctx.date == 3:
            pos = ctx.positions[0]
            ref = pos.first_entry_price
            tp_px = ref * 1.10
            bar = ctx.bars[SYM]
            hit_take_gap = bar.open >= tp_px
            if hit_take_gap:
                return TargetPortfolio(
                    decision_date=3, weights={},
                    exit_signals={SYM: ("TAKE_PROFIT", ref, 0.0, tp_px, "gap")},
                )
        return None

    result = run(bars, targets, EngineConfig(run_id="test_tp_gap"))
    sell_fills = [f for f in result.fills if f.side == "SELL"]
    assert len(sell_fills) == 1
    # gap: 以 T+1 开盘价 11.8 成交
    assert sell_fills[0].price == pytest.approx(11.8, abs=0.01)


def test_stop_loss_touch():
    """盘中最低价 <= 止损价，但开盘未越过 → 以止损价成交。"""
    # 建仓价 10.0，止损 5%，阈值 9.5
    # T3: 最低 9.3 <= 9.5，开盘 9.8 > 9.5 → touch
    # T4: 执行日 → 以 9.5 成交
    bars = {
        1: {SYM: _bar(1, 10.0, 10.5, 9.8, 10.2)},
        2: {SYM: _bar(2, 10.2, 10.5, 10.0, 10.3)},
        3: {SYM: _bar(3, 9.8, 10.0, 9.3, 9.6)},  # 触发：开盘 9.8 > 9.5，最低 9.3 <= 9.5 → touch
        4: {SYM: _bar(4, 9.2, 9.5, 9.0, 9.1)},   # 执行日
    }

    def targets(ctx):
        if ctx.date == 1:
            return TargetPortfolio(decision_date=1, weights={SYM: 0.5})
        if ctx.date == 3:
            pos = ctx.positions[0]
            ref = pos.first_entry_price
            sl_px = ref * 0.95
            bar = ctx.bars[SYM]
            hit_stop_gap = bar.open <= sl_px
            hit_stop_touch = (not hit_stop_gap) and (bar.low <= sl_px)
            if hit_stop_touch:
                return TargetPortfolio(
                    decision_date=3, weights={},
                    exit_signals={SYM: ("STOP_LOSS", ref, sl_px, 0.0, "touch")},
                )
        return None

    result = run(bars, targets, EngineConfig(run_id="test_sl_touch"))
    sell_fills = [f for f in result.fills if f.side == "SELL"]
    assert len(sell_fills) == 1
    # touch: 以阈值价 9.5 成交，不是 T+1 开盘价 9.2
    assert sell_fills[0].price == pytest.approx(9.5, abs=0.01)


def test_stop_loss_gap():
    """开盘价 <= 止损价 → 以 T+1 开盘价成交。"""
    # T3: 开盘 9.3 <= 9.5 → gap
    # T4: 执行日 → 以 T4 开盘价成交
    bars = {
        1: {SYM: _bar(1, 10.0, 10.5, 9.8, 10.2)},
        2: {SYM: _bar(2, 10.2, 10.5, 10.0, 10.3)},
        3: {SYM: _bar(3, 9.3, 9.8, 9.0, 9.5)},   # 触发：开盘 9.3 <= 9.5 → gap
        4: {SYM: _bar(4, 9.1, 9.3, 8.8, 9.0)},   # 执行日
    }

    def targets(ctx):
        if ctx.date == 1:
            return TargetPortfolio(decision_date=1, weights={SYM: 0.5})
        if ctx.date == 3:
            pos = ctx.positions[0]
            ref = pos.first_entry_price
            sl_px = ref * 0.95
            bar = ctx.bars[SYM]
            hit_stop_gap = bar.open <= sl_px
            if hit_stop_gap:
                return TargetPortfolio(
                    decision_date=3, weights={},
                    exit_signals={SYM: ("STOP_LOSS", ref, sl_px, 0.0, "gap")},
                )
        return None

    result = run(bars, targets, EngineConfig(run_id="test_sl_gap"))
    sell_fills = [f for f in result.fills if f.side == "SELL"]
    assert len(sell_fills) == 1
    # gap: 以 T+1 开盘价 9.1 成交
    assert sell_fills[0].price == pytest.approx(9.1, abs=0.01)
