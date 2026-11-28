"""决策回调模式测试。

内核契约：收盘后回调收到引擎真实状态（持仓视图/现金/总资产），
回调返回的目标组合在下一交易日开盘执行；返回 None 表示当日无决策。
路径依赖退出规则（止盈止损）的语义属于策略层，此处只验证引擎侧契约。
"""
from __future__ import annotations

import pytest

from nooshare_backtest.engine import DecisionContext, EngineConfig, run
from tests.builders.preclose import backfill_pre_close
from nooshare_backtest.models import Bar, TargetPortfolio


def _bar(date: int, symbol: str, o: float, h: float, l: float, c: float, ret: float | None = None) -> Bar:
    prev_close = c if ret is None else c / (1 + ret)
    return Bar(
        date=date, symbol=symbol, open=o, high=h, low=l, close=c,
        volume=1_000_000.0, ret=ret,
        canonical_id=symbol[2:], exchange=symbol[:2],
    )


def _bars() -> dict[int, dict[str, Bar]]:
    # 四个交易日；ret 均在 ±21% 内，不触发假跳空防御
    return {
        20240101: {"sh600001": _bar(20240101, "sh600001", 10.0, 10.5, 9.8, 10.2, 0.0)},
        20240102: {"sh600001": _bar(20240102, "sh600001", 10.2, 10.6, 10.0, 10.4, 0.0196)},
        20240103: {"sh600001": _bar(20240103, "sh600001", 10.4, 10.5, 8.5, 8.8, -0.1538)},
        20240104: {"sh600001": _bar(20240104, "sh600001", 8.8, 9.0, 8.6, 8.9, 0.0114)},
    }


def test_callback_receives_engine_state():
    """回调在每个收盘时点收到引擎真实状态。"""
    seen: list[DecisionContext] = []

    def decide(ctx: DecisionContext) -> TargetPortfolio | None:
        seen.append(ctx)
        if ctx.date == 20240101:
            assert ctx.positions == ()
            assert abs(ctx.total_asset - 1_000_000.0) < 1e-6
            # T0 收盘决策：全仓买入
            return TargetPortfolio(decision_date=ctx.date, weights={"sh600001": 0.95})
        return None

    result = run(backfill_pre_close(_bars()), decide, EngineConfig(run_id="cb-state"))
    # T1 开盘买入成交（fill.price 为含滑点的实际成交价：open * (1 + slippage_bp)）
    buys = [f for f in result.fills if f.side == "BUY"]
    assert len(buys) == 1
    assert buys[0].date == 20240102
    assert buys[0].price == pytest.approx(10.2 * (1 + 0.0005))
    # 回调共 4 次（每个交易日收盘一次）
    assert [c.date for c in seen] == [20240101, 20240102, 20240103, 20240104]


def test_callback_position_view_after_fill():
    """买入次日收盘，持仓视图暴露建仓批次（不含费用价/买入日/可卖数）。"""

    def decide(ctx: DecisionContext) -> TargetPortfolio | None:
        if ctx.date == 20240101:
            return TargetPortfolio(decision_date=ctx.date, weights={"sh600001": 0.95})
        if ctx.date == 20240102:
            # T1 买入当日：批次存在，但 T+1 规则下当日不可卖
            assert len(ctx.positions) == 1
            view = ctx.positions[0]
            assert view.symbol == "sh600001"
            assert view.shares > 0
            assert view.first_entry_date == 20240102
            # 存活最早批次的不含费用买入价 = 实际成交价（含滑点）
            assert view.first_entry_price == pytest.approx(10.2 * (1 + 0.0005))
            assert view.sellable_shares == 0
        if ctx.date == 20240103:
            # T2 起 T+1 解锁
            assert ctx.positions[0].sellable_shares == ctx.positions[0].shares
        return None

    run(backfill_pre_close(_bars()), decide, EngineConfig(run_id="cb-view"))


def test_callback_exit_decision_executes_next_session():
    """T2 收盘回调决定清仓 → T3 开盘卖出成交并产生完整 Trade 生命周期。"""

    def decide(ctx: DecisionContext) -> TargetPortfolio | None:
        if ctx.date == 20240101:
            return TargetPortfolio(decision_date=ctx.date, weights={"sh600001": 0.95})
        if ctx.date == 20240103:
            # 路径依赖规则示例：T2 收盘判定退出（此处直接模拟策略层判定结果）
            return TargetPortfolio(decision_date=ctx.date, weights={})
        return None

    result = run(backfill_pre_close(_bars()), decide, EngineConfig(run_id="cb-exit"))
    sells = [f for f in result.fills if f.side == "SELL"]
    assert len(sells) == 1
    assert sells[0].date == 20240104
    assert sells[0].price == pytest.approx(8.8 * (1 - 0.0005))  # T3 开盘卖出，含滑点
    assert result.trades, "必须产出交易生命周期"
    assert result.trades[0].status == "CLOSED"


def test_callback_none_means_no_decision():
    """回调返回 None：不产生订单、不产生 NO_NEXT_SESSION 之外的差异。"""

    def decide(ctx: DecisionContext) -> TargetPortfolio | None:
        return None

    result = run(backfill_pre_close(_bars()), decide, EngineConfig(run_id="cb-none"))
    assert result.fills == []
    assert result.orders == []
    assert result.trades == []


def test_last_day_callback_decision_marked_no_next_session():
    """最后一天回调给出的决策显式记 NO_NEXT_SESSION，不静默丢弃。"""

    def decide(ctx: DecisionContext) -> TargetPortfolio | None:
        if ctx.date == 20240104:
            return TargetPortfolio(decision_date=ctx.date, weights={"sh600001": 0.5})
        return None

    result = run(backfill_pre_close(_bars()), decide, EngineConfig(run_id="cb-last"))
    reasons = [d.reason for d in result.deviations]
    assert "NO_NEXT_SESSION" in reasons


def test_hold_position_generates_no_orders():
    """hold 声明的持仓不生成订单：不调仓、不清仓。"""

    def decide(ctx: DecisionContext) -> TargetPortfolio | None:
        if ctx.date == 20240101:
            return TargetPortfolio(decision_date=ctx.date, weights={"sh600001": 0.95})
        if ctx.date == 20240103:
            # T2 收盘：保持持仓不动（不列 weights，仅 hold），即使价格大跌
            assert ctx.positions[0].symbol == "sh600001"
            return TargetPortfolio(decision_date=ctx.date, weights={}, hold=frozenset({"sh600001"}))
        return None

    result = run(backfill_pre_close(_bars()), decide, EngineConfig(run_id="cb-hold"))
    sells = [f for f in result.fills if f.side == "SELL"]
    assert sells == [], "hold 持仓不得被清仓"
    # T3 收盘持仓仍在（T2 未触发卖出，T3 无决策）
    final = [p for p in result.positions if p.date == 20240104]
    assert final and final[0].shares > 0


def test_hold_position_eventually_sellable_via_weights():
    """hold 后改变主意（权重 0）仍可清仓。"""

    def decide(ctx: DecisionContext) -> TargetPortfolio | None:
        if ctx.date == 20240101:
            return TargetPortfolio(decision_date=ctx.date, weights={"sh600001": 0.95})
        if ctx.date == 20240102:
            return TargetPortfolio(decision_date=ctx.date, weights={}, hold=frozenset({"sh600001"}))
        if ctx.date == 20240103:
            return TargetPortfolio(decision_date=ctx.date, weights={})  # 清仓
        return None

    result = run(backfill_pre_close(_bars()), decide, EngineConfig(run_id="cb-hold-sell"))
    sells = [f for f in result.fills if f.side == "SELL"]
    assert len(sells) == 1 and sells[0].date == 20240104
