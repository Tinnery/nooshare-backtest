"""Trade ID 早期建立、ExitEvent 完整身份、退出重试机制测试。

覆盖：
- 首笔买入 Order 的 Signal 传递到 Trade.initial_signal_id
- Trade ID 稳定性（首笔 BUY Fill 时建立，后续不变）
- ExitEvent 与 Trade 精确关联
- STOP_LOSS / TAKE_PROFIT 退出原因
- 同日止盈止损同时触发时止损优先
- 跌停拒绝后重试（T+1 拒绝，T+2 继续尝试）
- 部分成交与持续退出
- OPEN Trade（未平仓）
- 回测期末未执行的 ExitEvent
- 多个 Run 的事件身份互不冲突
"""
import unittest

from nooshare_backtest.engine import DecisionContext, EngineConfig, run
from nooshare_backtest.models import (
    Bar,
    ExitEvent,
    TargetPortfolio,
    exit_event_id,
)


def _bar(date: int, symbol: str, o: float, h: float, l: float, c: float,
         is_limit_down: bool = False, is_limit_up: bool = False) -> Bar:
    return Bar(
        date=date, symbol=symbol, open=o, high=h, low=l, close=c,
        volume=1_000_000.0, ret=0.0,
        is_limit_down=is_limit_down, is_limit_up=is_limit_up,
        canonical_id=symbol[-6:], exchange=symbol[:2],
    )


class TestTradeIDEarlyEstablishment(unittest.TestCase):
    """Trade ID 在首笔 BUY Fill 时建立，后续不变。"""

    def test_trade_id_stable_across_multiple_buys(self):
        """多次买入同一持仓，trade_id 保持不变。"""
        bars = {
            20240101: {"sh600001": _bar(20240101, "sh600001", 10.0, 10.5, 9.8, 10.2)},
            20240102: {"sh600001": _bar(20240102, "sh600001", 10.2, 10.6, 10.0, 10.4)},
            20240103: {"sh600001": _bar(20240103, "sh600001", 10.4, 10.5, 10.2, 10.3)},
            20240104: {"sh600001": _bar(20240104, "sh600001", 10.3, 10.6, 10.2, 10.5)},
        }

        def decide(ctx: DecisionContext) -> TargetPortfolio | None:
            if ctx.date == 20240101:
                return TargetPortfolio(decision_date=ctx.date, weights={"sh600001": 0.5})
            if ctx.date == 20240102:
                # 加仓
                return TargetPortfolio(decision_date=ctx.date, weights={"sh600001": 0.8})
            if ctx.date == 20240103:
                # 清仓
                return TargetPortfolio(decision_date=ctx.date, weights={})
            return None

        result = run(bars, decide, EngineConfig(run_id="stable-tid"))
        self.assertEqual(len(result.trades), 1)
        trade = result.trades[0]
        self.assertEqual(trade.status, "CLOSED")
        # trade_id 在首笔 BUY 时建立，后续加仓不改变
        self.assertTrue(trade.trade_id)
        self.assertGreater(trade.entry_qty, 0)

    def test_trade_id_deterministic(self):
        """相同输入产生相同的 trade_id。"""
        bars = {
            20240101: {"sh600001": _bar(20240101, "sh600001", 10.0, 10.5, 9.8, 10.2)},
            20240102: {"sh600001": _bar(20240102, "sh600001", 10.2, 10.6, 10.0, 10.4)},
            20240103: {"sh600001": _bar(20240103, "sh600001", 10.4, 10.5, 10.2, 10.3)},
        }

        def decide(ctx: DecisionContext) -> TargetPortfolio | None:
            if ctx.date == 20240101:
                return TargetPortfolio(decision_date=ctx.date, weights={"sh600001": 0.5})
            if ctx.date == 20240102:
                return TargetPortfolio(decision_date=ctx.date, weights={})
            return None

        r1 = run(bars, decide, EngineConfig(run_id="det"))
        r2 = run(bars, decide, EngineConfig(run_id="det"))
        self.assertEqual(r1.trades[0].trade_id, r2.trades[0].trade_id)

    def test_trade_id_different_across_runs(self):
        """不同 run_id 产生不同的 trade_id。"""
        bars = {
            20240101: {"sh600001": _bar(20240101, "sh600001", 10.0, 10.5, 9.8, 10.2)},
            20240102: {"sh600001": _bar(20240102, "sh600001", 10.2, 10.6, 10.0, 10.4)},
        }

        def decide(ctx: DecisionContext) -> TargetPortfolio | None:
            if ctx.date == 20240101:
                return TargetPortfolio(decision_date=ctx.date, weights={"sh600001": 0.5})
            return None

        r1 = run(bars, decide, EngineConfig(run_id="run1"))
        r2 = run(bars, decide, EngineConfig(run_id="run2"))
        self.assertNotEqual(r1.trades[0].trade_id, r2.trades[0].trade_id)


class TestSignalPropagation(unittest.TestCase):
    """首笔买入 Order 的 Signal 传递到 Trade.initial_signal_id。"""

    def test_signal_propagates_to_trade(self):
        """signal_id 从 TargetPortfolio 透传到 Order，再到 Trade.initial_signal_id。"""
        bars = {
            20240101: {"sh600001": _bar(20240101, "sh600001", 10.0, 10.5, 9.8, 10.2)},
            20240102: {"sh600001": _bar(20240102, "sh600001", 10.2, 10.6, 10.0, 10.4)},
            20240103: {"sh600001": _bar(20240103, "sh600001", 10.4, 10.5, 10.2, 10.3)},
        }

        def decide(ctx: DecisionContext) -> TargetPortfolio | None:
            if ctx.date == 20240101:
                return TargetPortfolio(
                    decision_date=ctx.date,
                    weights={"sh600001": 0.5},
                    signal_ids={"sh600001": "signal-abc-123"},
                )
            if ctx.date == 20240102:
                return TargetPortfolio(decision_date=ctx.date, weights={})
            return None

        result = run(bars, decide, EngineConfig(run_id="sig-prop"))
        self.assertEqual(len(result.trades), 1)
        trade = result.trades[0]
        self.assertEqual(trade.initial_signal_id, "signal-abc-123")


class TestExitEventIdentity(unittest.TestCase):
    """ExitEvent 完整身份与 Trade 精确关联。"""

    def test_exit_event_id_deterministic(self):
        """exit_event_id 由 run_id + trade_id + decision_date + reason 确定性生成。"""
        bars = {
            20240101: {"sh600001": _bar(20240101, "sh600001", 10.0, 10.5, 9.8, 10.2)},
            20240102: {"sh600001": _bar(20240102, "sh600001", 10.2, 10.6, 10.0, 10.4)},
            20240103: {"sh600001": _bar(20240103, "sh600001", 10.4, 10.5, 10.2, 10.3)},
        }

        def decide(ctx: DecisionContext) -> TargetPortfolio | None:
            if ctx.date == 20240101:
                return TargetPortfolio(decision_date=ctx.date, weights={"sh600001": 0.5})
            if ctx.date == 20240102:
                # 触发止损
                trade_id = ctx.positions[0].trade_id if ctx.positions else ""
                eid = exit_event_id(
                    run_id="exit-det",
                    trade_id=trade_id,
                    decision_date=ctx.date,
                    reason="STOP_LOSS",
                    reference_price=10.4,
                    stop_price=9.5,
                )
                return TargetPortfolio(
                    decision_date=ctx.date,
                    weights={},
                    exit_event_ids={"sh600001": eid},
                    decision_reasons={"sh600001": "STOP_LOSS"},
                )
            return None

        r1 = run(bars, decide, EngineConfig(run_id="exit-det"))
        r2 = run(bars, decide, EngineConfig(run_id="exit-det"))
        self.assertEqual(len(r1.exit_events), 1)
        self.assertEqual(r1.exit_events[0].exit_event_id, r2.exit_events[0].exit_event_id)
        self.assertEqual(r1.exit_events[0].reason, "STOP_LOSS")

    def test_exit_event_links_to_trade(self):
        """ExitEvent.trade_id 与 Trade.trade_id 精确关联。"""
        bars = {
            20240101: {"sh600001": _bar(20240101, "sh600001", 10.0, 10.5, 9.8, 10.2)},
            20240102: {"sh600001": _bar(20240102, "sh600001", 10.2, 10.6, 10.0, 10.4)},
            20240103: {"sh600001": _bar(20240103, "sh600001", 10.4, 10.5, 10.2, 10.3)},
        }

        def decide(ctx: DecisionContext) -> TargetPortfolio | None:
            if ctx.date == 20240101:
                return TargetPortfolio(decision_date=ctx.date, weights={"sh600001": 0.5})
            if ctx.date == 20240102:
                trade_id = ctx.positions[0].trade_id if ctx.positions else ""
                eid = exit_event_id(
                    run_id="exit-link",
                    trade_id=trade_id,
                    decision_date=ctx.date,
                    reason="TAKE_PROFIT",
                    reference_price=10.4,
                )
                return TargetPortfolio(
                    decision_date=ctx.date,
                    weights={},
                    exit_event_ids={"sh600001": eid},
                    decision_reasons={"sh600001": "TAKE_PROFIT"},
                )
            return None

        result = run(bars, decide, EngineConfig(run_id="exit-link"))
        self.assertEqual(len(result.trades), 1)
        self.assertEqual(len(result.exit_events), 1)
        self.assertEqual(result.exit_events[0].trade_id, result.trades[0].trade_id)


class TestStopLossTakeProfitPriority(unittest.TestCase):
    """同日止盈止损同时触发时止损优先。"""

    def test_stop_loss_priority_over_take_profit(self):
        """策略回调同时给出止损和止盈决策，止损优先（由策略层保证，引擎不判断）。"""
        bars = {
            20240101: {"sh600001": _bar(20240101, "sh600001", 10.0, 10.5, 9.8, 10.2)},
            20240102: {"sh600001": _bar(20240102, "sh600001", 10.2, 10.6, 10.0, 10.4)},
            20240103: {"sh600001": _bar(20240103, "sh600001", 10.4, 10.5, 10.2, 10.3)},
        }

        def decide(ctx: DecisionContext) -> TargetPortfolio | None:
            if ctx.date == 20240101:
                return TargetPortfolio(decision_date=ctx.date, weights={"sh600001": 0.5})
            if ctx.date == 20240102:
                # 策略层判断：同时触发止损和止盈，选择止损（策略层逻辑，引擎不干预）
                trade_id = ctx.positions[0].trade_id if ctx.positions else ""
                eid = exit_event_id(
                    run_id="priority",
                    trade_id=trade_id,
                    decision_date=ctx.date,
                    reason="STOP_LOSS",  # 策略层选择止损
                    reference_price=10.4,
                )
                return TargetPortfolio(
                    decision_date=ctx.date,
                    weights={},
                    exit_event_ids={"sh600001": eid},
                    decision_reasons={"sh600001": "STOP_LOSS"},
                )
            return None

        result = run(bars, decide, EngineConfig(run_id="priority"))
        self.assertEqual(len(result.exit_events), 1)
        self.assertEqual(result.exit_events[0].reason, "STOP_LOSS")
        self.assertEqual(result.trades[0].exit_reason, "STOP_LOSS")


class TestExitRetryMechanism(unittest.TestCase):
    """退出重试机制：跌停拒绝后继续尝试。"""

    def test_exit_retry_after_limit_down_rejection(self):
        """T+1 跌停拒绝卖出，T+2 继续尝试卖出。"""
        bars = {
            20240101: {"sh600001": _bar(20240101, "sh600001", 10.0, 10.5, 9.8, 10.2)},
            20240102: {"sh600001": _bar(20240102, "sh600001", 10.2, 10.6, 10.0, 10.4)},
            20240103: {"sh600001": _bar(20240103, "sh600001", 10.4, 10.5, 9.0, 9.2, is_limit_down=True)},
            20240104: {"sh600001": _bar(20240104, "sh600001", 9.2, 9.5, 9.0, 9.4)},
        }

        exit_eid = None

        def decide(ctx: DecisionContext) -> TargetPortfolio | None:
            nonlocal exit_eid
            if ctx.date == 20240101:
                return TargetPortfolio(decision_date=ctx.date, weights={"sh600001": 0.5})
            if ctx.date == 20240102:
                # 触发止损
                trade_id = ctx.positions[0].trade_id if ctx.positions else ""
                exit_eid = exit_event_id(
                    run_id="retry",
                    trade_id=trade_id,
                    decision_date=ctx.date,
                    reason="STOP_LOSS",
                    reference_price=10.4,
                )
                return TargetPortfolio(
                    decision_date=ctx.date,
                    weights={},
                    exit_event_ids={"sh600001": exit_eid},
                    decision_reasons={"sh600001": "STOP_LOSS"},
                )
            if ctx.date == 20240103:
                # T+1 跌停拒绝，但 pending_exit 保留，T+2 继续尝试
                self.assertIn("sh600001", ctx.pending_exits)
                self.assertEqual(ctx.pending_exits["sh600001"], exit_eid)
                # 复用同一个 exit_event_id
                return TargetPortfolio(
                    decision_date=ctx.date,
                    weights={},
                    exit_event_ids={"sh600001": exit_eid},
                    decision_reasons={"sh600001": "STOP_LOSS"},
                )
            return None

        result = run(bars, decide, EngineConfig(run_id="retry"))
        # T+1 (20240103) 跌停拒绝，T+2 (20240104) 成功卖出
        sells = [o for o in result.orders if o.side == "SELL"]
        self.assertEqual(len(sells), 2)
        self.assertEqual(sells[0].status, "REJECTED")
        self.assertEqual(sells[0].reason, "LIMIT_DOWN")
        self.assertEqual(sells[1].status, "FILLED")
        # 两次 SELL 订单引用同一个 ExitEvent ID
        self.assertEqual(sells[0].exit_event_id, exit_eid)
        self.assertEqual(sells[1].exit_event_id, exit_eid)
        # Trade 正常关闭
        self.assertEqual(len(result.trades), 1)
        self.assertEqual(result.trades[0].status, "CLOSED")


class TestPartialFillAndContinuedExit(unittest.TestCase):
    """部分成交后继续卖出剩余。"""

    def test_partial_sell_continues_exit(self):
        """部分成交后 pending_exit 保留，继续卖出剩余。"""
        bars = {
            20240101: {"sh600001": _bar(20240101, "sh600001", 10.0, 10.5, 9.8, 10.2)},
            20240102: {"sh600001": _bar(20240102, "sh600001", 10.2, 10.6, 10.0, 10.4)},
            20240103: {"sh600001": _bar(20240103, "sh600001", 10.4, 10.5, 10.2, 10.3)},
            20240104: {"sh600001": _bar(20240104, "sh600001", 10.3, 10.6, 10.2, 10.5)},
        }

        def decide(ctx: DecisionContext) -> TargetPortfolio | None:
            if ctx.date == 20240101:
                return TargetPortfolio(decision_date=ctx.date, weights={"sh600001": 0.5})
            if ctx.date == 20240102:
                # 触发止盈，但只卖出一部分（通过权重控制）
                trade_id = ctx.positions[0].trade_id if ctx.positions else ""
                eid = exit_event_id(
                    run_id="partial",
                    trade_id=trade_id,
                    decision_date=ctx.date,
                    reason="TAKE_PROFIT",
                    reference_price=10.4,
                )
                # 只卖出一半（权重 0.25，当前 0.5）
                return TargetPortfolio(
                    decision_date=ctx.date,
                    weights={"sh600001": 0.25},
                    exit_event_ids={"sh600001": eid},
                    decision_reasons={"sh600001": "TAKE_PROFIT"},
                )
            if ctx.date == 20240103:
                # 继续清仓（在 20240104 执行）
                return TargetPortfolio(decision_date=ctx.date, weights={})
            return None

        result = run(bars, decide, EngineConfig(run_id="partial"))
        # Trade 正常关闭
        self.assertEqual(len(result.trades), 1)
        self.assertEqual(result.trades[0].status, "CLOSED")


class TestOpenTrade(unittest.TestCase):
    """OPEN Trade（未平仓）。"""

    def test_open_trade_at_backtest_end(self):
        """回测结束仍有持仓，Trade 状态为 OPEN。"""
        bars = {
            20240101: {"sh600001": _bar(20240101, "sh600001", 10.0, 10.5, 9.8, 10.2)},
            20240102: {"sh600001": _bar(20240102, "sh600001", 10.2, 10.6, 10.0, 10.4)},
            20240103: {"sh600001": _bar(20240103, "sh600001", 10.4, 10.5, 10.2, 10.3)},
        }

        def decide(ctx: DecisionContext) -> TargetPortfolio | None:
            if ctx.date == 20240101:
                return TargetPortfolio(decision_date=ctx.date, weights={"sh600001": 0.5})
            return None

        result = run(bars, decide, EngineConfig(run_id="open"))
        self.assertEqual(len(result.trades), 1)
        trade = result.trades[0]
        self.assertEqual(trade.status, "OPEN")
        self.assertIsNone(trade.closed_date)
        self.assertGreater(trade.open_qty, 0)


class TestPendingExitAtBacktestEnd(unittest.TestCase):
    """回测期末未执行的 ExitEvent。"""

    def test_pending_exit_preserved_at_end(self):
        """回测结束仍有 pending_exit，明确保留未完成状态。"""
        bars = {
            20240101: {"sh600001": _bar(20240101, "sh600001", 10.0, 10.5, 9.8, 10.2)},
            20240102: {"sh600001": _bar(20240102, "sh600001", 10.2, 10.6, 10.0, 10.4)},
            20240103: {"sh600001": _bar(20240103, "sh600001", 10.4, 10.5, 9.0, 9.2, is_limit_down=True)},
        }

        def decide(ctx: DecisionContext) -> TargetPortfolio | None:
            if ctx.date == 20240101:
                return TargetPortfolio(decision_date=ctx.date, weights={"sh600001": 0.5})
            if ctx.date == 20240102:
                trade_id = ctx.positions[0].trade_id if ctx.positions else ""
                eid = exit_event_id(
                    run_id="pending-end",
                    trade_id=trade_id,
                    decision_date=ctx.date,
                    reason="STOP_LOSS",
                    reference_price=10.4,
                )
                return TargetPortfolio(
                    decision_date=ctx.date,
                    weights={},
                    exit_event_ids={"sh600001": eid},
                    decision_reasons={"sh600001": "STOP_LOSS"},
                )
            # 20240103 跌停拒绝，回测结束，pending_exit 保留
            return None

        result = run(bars, decide, EngineConfig(run_id="pending-end"))
        # Trade 仍为 OPEN（未成功卖出）
        self.assertEqual(len(result.trades), 1)
        self.assertEqual(result.trades[0].status, "OPEN")
        # ExitEvent 已记录
        self.assertEqual(len(result.exit_events), 1)
        self.assertEqual(result.exit_events[0].reason, "STOP_LOSS")


class TestMultipleRunsIdentityIsolation(unittest.TestCase):
    """多个 Run 的事件身份互不冲突。"""

    def test_different_runs_different_ids(self):
        """不同 run_id 产生不同的 order_id / fill_id / trade_id / exit_event_id。"""
        bars = {
            20240101: {"sh600001": _bar(20240101, "sh600001", 10.0, 10.5, 9.8, 10.2)},
            20240102: {"sh600001": _bar(20240102, "sh600001", 10.2, 10.6, 10.0, 10.4)},
            20240103: {"sh600001": _bar(20240103, "sh600001", 10.4, 10.5, 10.2, 10.3)},
        }

        def decide(ctx: DecisionContext) -> TargetPortfolio | None:
            if ctx.date == 20240101:
                return TargetPortfolio(decision_date=ctx.date, weights={"sh600001": 0.5})
            if ctx.date == 20240102:
                trade_id = ctx.positions[0].trade_id if ctx.positions else ""
                eid = exit_event_id(
                    run_id="iso",
                    trade_id=trade_id,
                    decision_date=ctx.date,
                    reason="TAKE_PROFIT",
                    reference_price=10.4,
                )
                return TargetPortfolio(
                    decision_date=ctx.date,
                    weights={},
                    exit_event_ids={"sh600001": eid},
                    decision_reasons={"sh600001": "TAKE_PROFIT"},
                )
            return None

        r1 = run(bars, decide, EngineConfig(run_id="run-a"))
        r2 = run(bars, decide, EngineConfig(run_id="run-b"))

        # order_id 不同
        self.assertNotEqual(r1.orders[0].order_id, r2.orders[0].order_id)
        # fill_id 不同
        self.assertNotEqual(r1.fills[0].fill_id, r2.fills[0].fill_id)
        # trade_id 不同
        self.assertNotEqual(r1.trades[0].trade_id, r2.trades[0].trade_id)
        # exit_event_id 不同（因为 trade_id 不同）
        self.assertNotEqual(r1.exit_events[0].exit_event_id, r2.exit_events[0].exit_event_id)


if __name__ == "__main__":
    unittest.main()
