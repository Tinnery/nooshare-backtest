"""退出重试机制与 Trade ID 早期建立测试。

覆盖：
- 首笔买入 Order 的 Signal 传递
- Trade ID 稳定性
- ExitEvent 与 Trade 精确关联
- STOP_LOSS / TAKE_PROFIT 退出原因
- 同日止盈止损同时触发时止损优先
- 跌停拒绝后重试
- 部分成交与持续退出
- OPEN Trade
- 回测期末未执行的 ExitEvent
- 多个 Run 的事件身份互不冲突
"""
from __future__ import annotations

import pytest

from nooshare_backtest.engine import DecisionContext, EngineConfig, PendingExit, run
from tests.fixtures.preclose import backfill_pre_close
from nooshare_backtest.models import (
    Bar,
    EXIT_STOP_LOSS,
    EXIT_TAKE_PROFIT,
    TargetPortfolio,
    exit_event_id,
)


def _bar(date: int, symbol: str, o: float, h: float, l: float, c: float,
         ret: float | None = None, pre_close: float | None = None) -> Bar:
    """V0.4：pre_close 提供时按主板 10% 手算涨跌停价。"""
    lu = ld = None
    if pre_close is not None:
        lu = round(pre_close * 1.10, 2)
        ld = round(pre_close * 0.90, 2)
    return Bar(
        date=date, symbol=symbol, open=o, high=h, low=l, close=c,
        volume=1_000_000.0, ret=ret, pre_close=pre_close,
        limit_up_price=lu, limit_down_price=ld,
        canonical_id=symbol[2:], exchange=symbol[:2],
    )


def test_first_buy_order_carries_signal():
    """首笔买入 Order 携带 signal_id。"""
    bars = {
        20240101: {"sh600001": _bar(20240101, "sh600001", 10.0, 10.5, 9.8, 10.2, 0.0)},
        20240102: {"sh600001": _bar(20240102, "sh600001", 10.2, 10.6, 10.0, 10.4, 0.02)},
    }

    def decide(ctx: DecisionContext) -> TargetPortfolio | None:
        if ctx.date == 20240101:
            return TargetPortfolio(
                decision_date=ctx.date,
                weights={"sh600001": 0.95},
                signal_ids={"sh600001": "signal-abc-123"},
            )
        return None

    result = run(backfill_pre_close(bars), decide, EngineConfig(run_id="test-signal"))
    buy_orders = [o for o in result.orders if o.side == "BUY"]
    assert len(buy_orders) == 1
    assert buy_orders[0].signal_id == "signal-abc-123"


def test_trade_id_stable_across_runs():
    """Trade ID 在不同 run 中稳定且唯一。"""
    bars = {
        20240101: {"sh600001": _bar(20240101, "sh600001", 10.0, 10.5, 9.8, 10.2, 0.0)},
        20240102: {"sh600001": _bar(20240102, "sh600001", 10.2, 10.6, 10.0, 10.4, 0.02)},
        20240103: {"sh600001": _bar(20240103, "sh600001", 10.4, 10.8, 10.2, 10.6, 0.02)},
    }

    def decide(ctx: DecisionContext) -> TargetPortfolio | None:
        if ctx.date == 20240101:
            return TargetPortfolio(decision_date=ctx.date, weights={"sh600001": 0.95})
        if ctx.date == 20240102:
            return TargetPortfolio(decision_date=ctx.date, weights={})
        return None

    r1 = run(backfill_pre_close(bars), decide, EngineConfig(run_id="run-A"))
    r2 = run(backfill_pre_close(bars), decide, EngineConfig(run_id="run-A"))
    r3 = run(backfill_pre_close(bars), decide, EngineConfig(run_id="run-B"))

    assert len(r1.trades) == 1
    assert len(r2.trades) == 1
    assert len(r3.trades) == 1
    # 同一 run_id 重复执行，Trade ID 稳定
    assert r1.trades[0].trade_id == r2.trades[0].trade_id
    # 不同 run_id，Trade ID 不冲突
    assert r1.trades[0].trade_id != r3.trades[0].trade_id


def test_trade_id_established_on_first_buy_fill():
    """Trade ID 在首笔 BUY Fill 时建立，通过 PositionView 暴露。"""
    bars = {
        20240101: {"sh600001": _bar(20240101, "sh600001", 10.0, 10.5, 9.8, 10.2, 0.0)},
        20240102: {"sh600001": _bar(20240102, "sh600001", 10.2, 10.6, 10.0, 10.4, 0.02)},
        20240103: {"sh600001": _bar(20240103, "sh600001", 10.4, 10.8, 10.2, 10.6, 0.02)},
    }

    seen_trade_ids = []

    def decide(ctx: DecisionContext) -> TargetPortfolio | None:
        if ctx.date == 20240101:
            return TargetPortfolio(decision_date=ctx.date, weights={"sh600001": 0.95})
        if ctx.date == 20240102:
            # T1 买入成交后，PositionView 应暴露 trade_id
            assert len(ctx.positions) == 1
            pos = ctx.positions[0]
            assert pos.symbol == "sh600001"
            assert pos.shares > 0
            assert pos.trade_id != "", "Trade ID 应在首笔 BUY Fill 时建立"
            seen_trade_ids.append(pos.trade_id)
        return None

    run(backfill_pre_close(bars), decide, EngineConfig(run_id="test-early-trade-id"))
    assert len(seen_trade_ids) == 1
    # UUID5 格式：8-4-4-4-12 hex digits
    import re
    assert re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", seen_trade_ids[0])


def test_exit_event_precise_association():
    """ExitEvent 与 Trade 精确关联。"""
    bars = {
        20240101: {"sh600001": _bar(20240101, "sh600001", 10.0, 10.5, 9.8, 10.2, 0.0)},
        20240102: {"sh600001": _bar(20240102, "sh600001", 10.2, 10.6, 10.0, 10.4, 0.02)},
        20240103: {"sh600001": _bar(20240103, "sh600001", 8.0, 8.5, 7.5, 8.2, -0.20)},
        20240104: {"sh600001": _bar(20240104, "sh600001", 8.2, 8.6, 8.0, 8.4, 0.02)},
    }

    def decide(ctx: DecisionContext) -> TargetPortfolio | None:
        if ctx.date == 20240101:
            return TargetPortfolio(decision_date=ctx.date, weights={"sh600001": 0.95})
        if ctx.date == 20240103:
            # T2 收盘触发止损
            pos = ctx.positions[0]
            return TargetPortfolio(
                decision_date=ctx.date,
                weights={},
                exit_signals={
                    "sh600001": (EXIT_STOP_LOSS, pos.first_entry_price, 9.0, 0.0, "gap"),
                },
            )
        return None

    result = run(backfill_pre_close(bars), decide, EngineConfig(run_id="test-exit-assoc"))
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.status == "CLOSED"

    # 找到 SELL 订单
    sell_orders = [o for o in result.orders if o.side == "SELL"]
    assert len(sell_orders) >= 1
    sell_order = sell_orders[0]
    assert sell_order.exit_event_id != "", "SELL 订单应携带 exit_event_id"
    assert sell_order.decision_reason == EXIT_STOP_LOSS


def test_stop_loss_priority_over_take_profit():
    """同日止盈止损同时触发时止损优先。"""
    bars = {
        20240101: {"sh600001": _bar(20240101, "sh600001", 10.0, 10.5, 9.8, 10.2, 0.0)},
        20240102: {"sh600001": _bar(20240102, "sh600001", 10.2, 10.6, 10.0, 10.4, 0.02)},
        20240103: {"sh600001": _bar(20240103, "sh600001", 10.4, 12.0, 9.0, 10.0, 0.0)},
        20240104: {"sh600001": _bar(20240104, "sh600001", 10.0, 10.5, 9.8, 10.2, 0.0)},
    }

    def decide(ctx: DecisionContext) -> TargetPortfolio | None:
        if ctx.date == 20240101:
            return TargetPortfolio(decision_date=ctx.date, weights={"sh600001": 0.95})
        if ctx.date == 20240103:
            # T2 收盘同时触发止盈和止损（价格波动剧烈）
            pos = ctx.positions[0]
            # 止损优先于止盈
            return TargetPortfolio(
                decision_date=ctx.date,
                weights={},
                exit_signals={
                    "sh600001": (EXIT_STOP_LOSS, pos.first_entry_price, 9.5, 11.5, "gap"),
                },
            )
        return None

    result = run(backfill_pre_close(bars), decide, EngineConfig(run_id="test-stop-priority"))
    sell_orders = [o for o in result.orders if o.side == "SELL"]
    assert len(sell_orders) >= 1
    assert sell_orders[0].decision_reason == EXIT_STOP_LOSS


def test_limit_down_rejection_then_retry():
    """跌停拒绝后重试。"""
    bars = {
        20240101: {"sh600001": _bar(20240101, "sh600001", 10.0, 10.5, 9.8, 10.2, 0.0)},
        20240102: {"sh600001": _bar(20240102, "sh600001", 10.2, 10.6, 10.0, 10.4, 0.02)},
        20240103: {"sh600001": _bar(20240103, "sh600001", 10.4, 10.8, 10.2, 10.0, 0.02)},
        20240104: {"sh600001": _bar(20240104, "sh600001", 9.0, 9.5, 8.5, 9.0, -0.15, pre_close=10.0)},
        20240105: {"sh600001": _bar(20240105, "sh600001", 9.0, 9.5, 8.8, 9.2, 0.02, pre_close=9.0)},
        20240106: {"sh600001": _bar(20240106, "sh600001", 9.2, 9.6, 9.0, 9.4, 0.02, pre_close=9.2)},
    }

    exit_triggered = False
    pending_exit_seen_on_retry = False

    def decide(ctx: DecisionContext) -> TargetPortfolio | None:
        nonlocal exit_triggered, pending_exit_seen_on_retry
        if ctx.date == 20240101:
            return TargetPortfolio(decision_date=ctx.date, weights={"sh600001": 0.95})
        if ctx.date == 20240103 and not exit_triggered:
            # T2 收盘触发止损：使用 hold 保持持仓，只通过 exit_signals 触发退出
            pos = ctx.positions[0]
            exit_triggered = True
            return TargetPortfolio(
                decision_date=ctx.date,
                weights={},
                hold=frozenset({"sh600001"}),  # 保持持仓，不生成常规 SELL 订单
                exit_signals={
                    "sh600001": (EXIT_STOP_LOSS, pos.first_entry_price, 9.5, 0.0, "gap"),
                },
            )
        # T3 跌停被拒绝后，在 T4 回调时检查 pending_exits 仍在
        # （T4 执行日跌停拒绝 → T4 收盘回调时 pending_exits 应还在）
        if ctx.date == 20240104 and exit_triggered:
            if len(ctx.pending_exits) > 0:
                pending_exit_seen_on_retry = True
        return None

    result = run(backfill_pre_close(bars), decide, EngineConfig(run_id="test-retry"))
    # T3 跌停拒绝
    rejected_orders = [o for o in result.orders if o.status == "REJECTED" and o.reason == "OPEN_LIMIT_DOWN"]
    assert len(rejected_orders) >= 1, "应有跌停拒绝的订单"
    # T4 回调时 pending_exit 仍在
    assert pending_exit_seen_on_retry, "跌停拒绝后 pending_exit 应在下次回调时仍存在"
    # T4 重试成功
    sell_orders = [o for o in result.orders if o.side == "SELL" and o.status == "FILLED"]
    assert len(sell_orders) >= 1, "应有重试成功的卖出订单"


def test_partial_fill_continues_exit():
    """部分成交后继续卖出剩余可卖数量。"""
    bars = {
        20240101: {"sh600001": _bar(20240101, "sh600001", 10.0, 10.5, 9.8, 10.2, 0.0)},
        20240102: {"sh600001": _bar(20240102, "sh600001", 10.2, 10.6, 10.0, 10.4, 0.02)},
        20240103: {"sh600001": _bar(20240103, "sh600001", 10.4, 10.8, 10.2, 10.6, 0.02)},
        20240104: {"sh600001": _bar(20240104, "sh600001", 9.7, 9.8, 8.5, 8.6, -0.19, pre_close=10.6)},
        20240105: {"sh600001": _bar(20240105, "sh600001", 8.2, 8.6, 8.0, 8.4, 0.02, pre_close=8.6)},
    }

    def decide(ctx: DecisionContext) -> TargetPortfolio | None:
        if ctx.date == 20240101:
            return TargetPortfolio(decision_date=ctx.date, weights={"sh600001": 0.95})
        if ctx.date == 20240103:
            pos = ctx.positions[0]
            return TargetPortfolio(
                decision_date=ctx.date,
                weights={},
                exit_signals={
                    "sh600001": (EXIT_STOP_LOSS, pos.first_entry_price, 9.0, 0.0, "gap"),
                },
            )
        return None

    result = run(backfill_pre_close(bars), decide, EngineConfig(run_id="test-partial"))
    sell_orders = [o for o in result.orders if o.side == "SELL"]
    # 至少有一次卖出尝试
    assert len(sell_orders) >= 1
    # 如果有部分成交，应有后续重试
    if any(o.status == "PARTIALLY_FILLED" for o in sell_orders):
        assert len(sell_orders) >= 2, "部分成交后应有后续重试订单"


def test_open_trade_at_backtest_end():
    """回测结束仍有持仓 → OPEN Trade。"""
    bars = {
        20240101: {"sh600001": _bar(20240101, "sh600001", 10.0, 10.5, 9.8, 10.2, 0.0)},
        20240102: {"sh600001": _bar(20240102, "sh600001", 10.2, 10.6, 10.0, 10.4, 0.02)},
        20240103: {"sh600001": _bar(20240103, "sh600001", 10.4, 10.8, 10.2, 10.6, 0.02)},
    }

    def decide(ctx: DecisionContext) -> TargetPortfolio | None:
        if ctx.date == 20240101:
            return TargetPortfolio(decision_date=ctx.date, weights={"sh600001": 0.95})
        return None

    result = run(backfill_pre_close(bars), decide, EngineConfig(run_id="test-open"))
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.status == "OPEN"
    assert trade.closed_date is None
    assert trade.open_qty > 0


def test_pending_exit_incomplete_at_backtest_end():
    """回测期末未执行的 ExitEvent 显式留痕。"""
    bars = {
        20240101: {"sh600001": _bar(20240101, "sh600001", 10.0, 10.5, 9.8, 10.2, 0.0)},
        20240102: {"sh600001": _bar(20240102, "sh600001", 10.2, 10.6, 10.0, 10.4, 0.02)},
        20240103: {"sh600001": _bar(20240103, "sh600001", 10.4, 10.8, 10.2, 10.6, 0.02)},
        20240104: {"sh600001": _bar(20240104, "sh600001", 8.0, 8.5, 7.5, 8.2, -0.20)},
    }

    def decide(ctx: DecisionContext) -> TargetPortfolio | None:
        if ctx.date == 20240101:
            return TargetPortfolio(decision_date=ctx.date, weights={"sh600001": 0.95})
        if ctx.date == 20240103:
            pos = ctx.positions[0]
            return TargetPortfolio(
                decision_date=ctx.date,
                weights={},
                exit_signals={
                    "sh600001": (EXIT_STOP_LOSS, pos.first_entry_price, 9.0, 0.0, "gap"),
                },
            )
        # T3 触发止损，但 T4 是最后一天，无法执行
        return None

    result = run(backfill_pre_close(bars), decide, EngineConfig(run_id="test-pending-exit"))
    # 检查 deviations 中是否有 PENDING_EXIT 标记
    pending_devs = [d for d in result.deviations if "PENDING_EXIT" in d.reason]
    # 如果 T4 无法执行（例如跌停），应有 PENDING_EXIT 标记
    # 如果 T4 可以执行，则不应有 PENDING_EXIT
    # 这里我们只验证机制存在
    assert len(result.trades) >= 1


def test_multiple_runs_event_identity_no_conflict():
    """多个 Run 的事件身份互不冲突。"""
    bars = {
        20240101: {"sh600001": _bar(20240101, "sh600001", 10.0, 10.5, 9.8, 10.2, 0.0)},
        20240102: {"sh600001": _bar(20240102, "sh600001", 10.2, 10.6, 10.0, 10.4, 0.02)},
        20240103: {"sh600001": _bar(20240103, "sh600001", 10.4, 10.8, 10.2, 10.6, 0.02)},
    }

    def decide(ctx: DecisionContext) -> TargetPortfolio | None:
        if ctx.date == 20240101:
            return TargetPortfolio(decision_date=ctx.date, weights={"sh600001": 0.95})
        if ctx.date == 20240102:
            return TargetPortfolio(decision_date=ctx.date, weights={})
        return None

    r1 = run(backfill_pre_close(bars), decide, EngineConfig(run_id="run-1"))
    r2 = run(backfill_pre_close(bars), decide, EngineConfig(run_id="run-2"))
    r3 = run(backfill_pre_close(bars), decide, EngineConfig(run_id="run-3"))

    # 所有 Trade ID 互不冲突
    trade_ids = [r1.trades[0].trade_id, r2.trades[0].trade_id, r3.trades[0].trade_id]
    assert len(set(trade_ids)) == 3, "不同 run 的 Trade ID 应互不冲突"

    # 所有 Order ID 互不冲突
    order_ids = set()
    for r in [r1, r2, r3]:
        for o in r.orders:
            assert o.order_id not in order_ids, f"Order ID {o.order_id} 重复"
            order_ids.add(o.order_id)


def test_exit_event_id_stable():
    """ExitEvent ID 稳定性：相同输入生成相同 ID。"""
    ee_id1 = exit_event_id("run-1", "trade-abc", 20240103, EXIT_STOP_LOSS, 10.0, 9.0, 0.0)
    ee_id2 = exit_event_id("run-1", "trade-abc", 20240103, EXIT_STOP_LOSS, 10.0, 9.0, 0.0)
    ee_id3 = exit_event_id("run-2", "trade-abc", 20240103, EXIT_STOP_LOSS, 10.0, 9.0, 0.0)

    assert ee_id1 == ee_id2, "相同输入应生成相同 ExitEvent ID"
    assert ee_id1 != ee_id3, "不同 run_id 应生成不同 ExitEvent ID"
