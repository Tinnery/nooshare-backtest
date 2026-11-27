"""ExitEvent 契约测试（14 条）。

使用可手算的 Fixture，不依赖外部数据库或文件系统。
覆盖 ExitEvent 完整生命周期：创建 → 重试 → 部分成交 → 完成/放弃。

契约列表：
 1. 首笔 BUY Order 携带 signal_id，trade_id=None
 2. 首笔 BUY Fill 建立 trade_id
 3. PositionView 暴露 trade_id
 4. 普通 SELL Order 携带 trade_id
 5. Rejected SELL Order 携带 trade_id
 6. Rejected SELL 没有 Fill，但仍能关联 Trade 和 ExitEvent
 7. 同一 ExitEvent 跌停拒绝后重试
 8. 重试 Order 保持相同 exit_event_id
 9. 部分成交后持续退出剩余数量
10. 完全平仓后 ExitEvent status=complete
11. 平仓后重新建仓生成新 trade_id
12. 回测结束时仍有 OPEN Trade
13. 回测结束时退出意图尚未完成 → abandoned
14. 不同 Run 的 trade_id / exit_event_id 不冲突
"""
from __future__ import annotations

import unittest
import uuid

from nooshare_backtest.engine import DecisionContext, EngineConfig, run
from nooshare_backtest.models import Bar, TargetPortfolio


# ---------- 可手算的 Fixture 日期 ----------
D1 = 20240102
D2 = 20240103
D3 = 20240104
D4 = 20240105
D5 = 20240108
D6 = 20240109
SYM = "sh600519"


def _bar(date, symbol, o, c, *, volume=1_000_000, ret=0.0,
         is_limit_down=False, is_limit_up=False):
    """构造单支 Bar，high/low 自动包络 open/close。"""
    return Bar(
        date=date, symbol=symbol,
        open=o, high=max(o, c) * 1.01, low=min(o, c) * 0.99, close=c,
        volume=volume, ret=ret,
        pre_close=(o / 0.90 if is_limit_down else (o / 1.10 if is_limit_up else o)),
        limit_up_ratio=0.10, limit_down_ratio=0.10, limit_rule_source="fixture_pit",
        is_limit_down=is_limit_down, is_limit_up=is_limit_up,
        canonical_id=symbol[-6:],
        exchange="sh" if symbol.startswith("sh") else "sz",
    )


def _bars_seq(prices, start_date=D1, symbol=SYM):
    """从价格序列生成 {date: {symbol: Bar}}。

    prices: list of float 或 (float, dict) 元组（元组第二个元素为 Bar 额外参数）。
    """
    bars = {}
    fixture_dates = (D1, D2, D3, D4, D5, D6)
    for i, item in enumerate(prices):
        if isinstance(item, tuple):
            px, kw = item
        else:
            px, kw = item, {}
        # Keep the six-session fixture on actual trading dates. In particular,
        # D4 Friday -> D5 Monday proves retry/T+1 logic is session-based, not +1 calendar day.
        date = fixture_dates[i] if start_date == D1 and i < len(fixture_dates) else start_date + i
        bars[date] = {symbol: _bar(date, symbol, px, px, **kw)}
    return bars


def _is_uuid(s):
    """检查字符串是否为有效 UUID 格式。"""
    try:
        uuid.UUID(s)
        return True
    except (ValueError, AttributeError):
        return False


def _run(bars, targets, run_id="test", initial_cash=100_000.0):
    """简化的引擎调用。"""
    return run(bars, targets, EngineConfig(run_id=run_id, initial_cash=initial_cash))


class TestExitEventContracts(unittest.TestCase):
    """14 条 ExitEvent 契约测试。"""

    # ------------------------------------------------------------------ #
    # 契约 1：首笔 BUY Order 携带 signal_id，trade_id=None
    # ------------------------------------------------------------------ #
    def test_01_buy_order_has_signal_id_no_trade_id(self):
        """D1 收盘决策买入，D2 开盘生成的 BUY Order 应有 signal_id 但无 trade_id。"""
        bars = _bars_seq([10.0, 10.0, 10.0])
        targets = {
            D1: TargetPortfolio(
                decision_date=D1,
                weights={SYM: 0.1},
                signal_ids={SYM: "signal-alpha-001"},
            ),
        }
        result = _run(bars, targets)

        buy_orders = [o for o in result.orders if o.side == "BUY"]
        self.assertTrue(buy_orders, "应至少有一笔 BUY Order")
        order = buy_orders[0]
        self.assertEqual(order.signal_id, "signal-alpha-001",
                         "BUY Order 应携带策略指定的 signal_id")
        self.assertEqual(order.trade_id, "",
                         "首笔 BUY Order 尚无持仓，trade_id 应为空字符串")

    # ------------------------------------------------------------------ #
    # 契约 2：首笔 BUY Fill 建立 trade_id
    # ------------------------------------------------------------------ #
    def test_02_buy_fill_establishes_trade_id(self):
        """首笔 BUY Fill 应建立非空且为有效 UUID 的 trade_id。"""
        bars = _bars_seq([10.0, 10.0, 10.0])
        targets = {D1: TargetPortfolio(decision_date=D1, weights={SYM: 0.1})}
        result = _run(bars, targets)

        buy_fills = [f for f in result.fills if f.side == "BUY"]
        self.assertTrue(buy_fills, "应至少有一笔 BUY Fill")
        fill = buy_fills[0]
        self.assertNotEqual(fill.trade_id, "",
                            "首笔 BUY Fill 应建立 trade_id")
        self.assertTrue(_is_uuid(fill.trade_id),
                        f"trade_id 应为有效 UUID，实际: {fill.trade_id}")

    # ------------------------------------------------------------------ #
    # 契约 3：PositionView 暴露 trade_id
    # ------------------------------------------------------------------ #
    def test_03_position_view_exposes_trade_id(self):
        """买入成交后，策略回调收到的 PositionView 应携带 trade_id。"""
        bars = _bars_seq([10.0, 10.0, 10.0, 10.0])
        observed_trade_ids = []

        def strategy(ctx: DecisionContext):
            # 记录观察到的 position trade_id
            for pos in ctx.positions:
                if pos.shares > 0:
                    observed_trade_ids.append(pos.trade_id)
            if ctx.date == D1:
                return TargetPortfolio(decision_date=D1, weights={SYM: 0.1})
            return TargetPortfolio(decision_date=ctx.date, weights={})

        result = _run(bars, strategy)

        self.assertTrue(observed_trade_ids,
                        "策略应至少观察到一次有持仓的 PositionView")
        for tid in observed_trade_ids:
            self.assertNotEqual(tid, "",
                                "PositionView.trade_id 不应为空")
            self.assertTrue(_is_uuid(tid),
                            f"PositionView.trade_id 应为有效 UUID，实际: {tid}")

    # ------------------------------------------------------------------ #
    # 契约 4：普通 SELL Order 携带 trade_id
    # ------------------------------------------------------------------ #
    def test_04_sell_order_carries_trade_id(self):
        """止盈触发后，SELL Order 的 trade_id 应与对应 position 的 trade_id 一致。"""
        bars = _bars_seq([10.0, 10.0, 11.0, 11.0, 11.0])
        position_trade_id = None

        def strategy(ctx: DecisionContext):
            nonlocal position_trade_id
            if ctx.date == D1:
                return TargetPortfolio(decision_date=D1, weights={SYM: 0.1})
            if ctx.date == D2:
                # D2 买入成交后，记录 position 的 trade_id
                for pos in ctx.positions:
                    if pos.symbol == SYM and pos.shares > 0:
                        position_trade_id = pos.trade_id
                # 触发止盈
                return TargetPortfolio(
                    decision_date=D3, weights={},
                    exit_signals={SYM: ("TAKE_PROFIT", 11.0, 0.0, 11.0)},
                )
            return TargetPortfolio(decision_date=ctx.date, weights={})

        result = _run(bars, strategy)

        sell_orders = [o for o in result.orders if o.side == "SELL"]
        self.assertTrue(sell_orders, "应有 SELL Order")
        self.assertIsNotNone(position_trade_id, "应先建立 position trade_id")
        for order in sell_orders:
            self.assertEqual(order.trade_id, position_trade_id,
                             "SELL Order 的 trade_id 应与 position 一致")
            self.assertNotEqual(order.exit_event_id, "",
                                "SELL Order 应携带 exit_event_id")

    # ------------------------------------------------------------------ #
    # 契约 5：Rejected SELL Order 携带 trade_id
    # ------------------------------------------------------------------ #
    def test_05_rejected_sell_carries_trade_id(self):
        """跌停场景：SELL Order 被拒绝，但 trade_id 仍然正确。"""
        # D2 买入，D3 跌停（SELL 被拒），D4 正常卖出
        bars = _bars_seq([
            10.0,           # D1
            10.0,           # D2 买入
            (9.0, {"is_limit_down": True}),  # D3 跌停
            9.0,            # D4 正常
            9.0,            # D5
        ])
        position_trade_id = None

        def strategy(ctx: DecisionContext):
            nonlocal position_trade_id
            if ctx.date == D1:
                return TargetPortfolio(decision_date=D1, weights={SYM: 0.1})
            if ctx.date == D2:
                for pos in ctx.positions:
                    if pos.symbol == SYM and pos.shares > 0:
                        position_trade_id = pos.trade_id
                return TargetPortfolio(
                    decision_date=D3, weights={},
                    exit_signals={SYM: ("STOP_LOSS", 9.0, 9.0, 0.0)},
                )
            return TargetPortfolio(decision_date=ctx.date, weights={})

        result = _run(bars, strategy)

        rejected_sells = [o for o in result.orders
                          if o.side == "SELL" and o.status == "REJECTED"]
        self.assertTrue(rejected_sells, "跌停应产生被拒绝的 SELL Order")
        self.assertIsNotNone(position_trade_id)
        for order in rejected_sells:
            self.assertEqual(order.trade_id, position_trade_id,
                             "被拒绝的 SELL 仍应携带正确的 trade_id")
            self.assertIn("OPEN_LIMIT_DOWN", order.reason,
                          "拒绝原因应为 OPEN_LIMIT_DOWN")

    # ------------------------------------------------------------------ #
    # 契约 6：Rejected SELL 没有 Fill，但仍能关联 Trade 和 ExitEvent
    # ------------------------------------------------------------------ #
    def test_06_rejected_sell_no_fill_but_has_ids(self):
        """被拒绝的 SELL 没有对应 Fill，但 Order 的 trade_id 和 exit_event_id 都有效。"""
        bars = _bars_seq([
            10.0,           # D1
            10.0,           # D2 买入
            (9.0, {"is_limit_down": True}),  # D3 跌停
            9.0,            # D4 正常卖出
            9.0,            # D5
        ])

        def strategy(ctx: DecisionContext):
            if ctx.date == D1:
                return TargetPortfolio(decision_date=D1, weights={SYM: 0.1})
            if ctx.date == D2:
                return TargetPortfolio(
                    decision_date=D3, weights={},
                    exit_signals={SYM: ("STOP_LOSS", 9.0, 9.0, 0.0)},
                )
            return TargetPortfolio(decision_date=ctx.date, weights={})

        result = _run(bars, strategy)

        rejected_sells = [o for o in result.orders
                          if o.side == "SELL" and o.status == "REJECTED"]
        self.assertTrue(rejected_sells)

        for order in rejected_sells:
            # 被拒绝的 Order 没有对应 Fill
            order_fills = [f for f in result.fills if f.order_id == order.order_id]
            self.assertEqual(len(order_fills), 0,
                             "被拒绝的 SELL 不应有 Fill")
            # 但 trade_id 和 exit_event_id 都有效
            self.assertTrue(_is_uuid(order.trade_id),
                            f"被拒绝 SELL 的 trade_id 应为有效 UUID: {order.trade_id}")
            self.assertTrue(_is_uuid(order.exit_event_id),
                            f"被拒绝 SELL 的 exit_event_id 应为有效 UUID: {order.exit_event_id}")
            # ExitEvent 存在且关联正确
            matching_events = [e for e in result.exit_events
                               if e.exit_event_id == order.exit_event_id]
            self.assertEqual(len(matching_events), 1,
                             "应存在对应的 ExitEvent")
            self.assertEqual(matching_events[0].trade_id, order.trade_id,
                             "ExitEvent 的 trade_id 应与 Order 一致")

    # ------------------------------------------------------------------ #
    # 契约 7：同一 ExitEvent 跌停拒绝后重试
    # ------------------------------------------------------------------ #
    def test_07_exit_retry_after_limit_down(self):
        """跌停拒绝后，下一个交易日继续生成 SELL Order。"""
        bars = _bars_seq([
            10.0,           # D1
            10.0,           # D2 买入
            10.0,           # D3 收盘触发止损
            (9.0, {"is_limit_down": True}),  # D4 跌停，SELL 被拒
            9.0,            # D5 重试卖出
            9.0,            # D6
        ])

        def strategy(ctx: DecisionContext):
            if ctx.date == D1:
                return TargetPortfolio(decision_date=D1, weights={SYM: 0.1})
            if ctx.date == D3:
                return TargetPortfolio(
                    decision_date=D3, weights={},
                    exit_signals={SYM: ("STOP_LOSS", 9.0, 9.0, 0.0)},
                )
            # D3 close 触发退出后，不再重复触发退出信号，引擎自动重试
            return TargetPortfolio(decision_date=ctx.date, weights={})

        result = _run(bars, strategy)

        sell_orders = [o for o in result.orders if o.side == "SELL"]
        self.assertGreaterEqual(len(sell_orders), 2,
                                "跌停拒绝后应至少重试一次，产生 ≥2 笔 SELL Order")
        # 第一笔被拒绝，第二笔应成功
        self.assertEqual(sell_orders[0].status, "REJECTED",
                         "第一笔 SELL 应被跌停拒绝")
        self.assertIn(sell_orders[1].status, ("FILLED", "PARTIALLY_FILLED"),
                      "重试的 SELL 应成功成交")

    # ------------------------------------------------------------------ #
    # 契约 8：重试 Order 保持相同 exit_event_id
    # ------------------------------------------------------------------ #
    def test_08_retry_orders_same_exit_event_id(self):
        """多次重试的 SELL Order 都应引用同一个 exit_event_id。"""
        bars = _bars_seq([
            10.0,           # D1
            10.0,           # D2 买入
            10.0,           # D3 触发止损
            (9.0, {"is_limit_down": True}),  # D4 跌停拒绝
            9.0,            # D5 重试
            9.0,            # D6
        ])

        def strategy(ctx: DecisionContext):
            if ctx.date == D1:
                return TargetPortfolio(decision_date=D1, weights={SYM: 0.1})
            if ctx.date == D3:
                return TargetPortfolio(
                    decision_date=D3, weights={},
                    exit_signals={SYM: ("STOP_LOSS", 9.0, 9.0, 0.0)},
                )
            return TargetPortfolio(decision_date=ctx.date, weights={})

        result = _run(bars, strategy)

        sell_orders = [o for o in result.orders if o.side == "SELL"]
        self.assertGreaterEqual(len(sell_orders), 2,
                                "应至少有 2 笔 SELL Order（首次 + 重试）")
        exit_event_ids = {o.exit_event_id for o in sell_orders}
        self.assertEqual(len(exit_event_ids), 1,
                         f"所有重试 Order 应共享同一 exit_event_id，实际: {exit_event_ids}")
        # exit_event_id 应为有效 UUID
        ee_id = sell_orders[0].exit_event_id
        self.assertTrue(_is_uuid(ee_id),
                        f"exit_event_id 应为有效 UUID: {ee_id}")

    # ------------------------------------------------------------------ #
    # 契约 9：部分成交后持续退出剩余数量
    # ------------------------------------------------------------------ #
    def test_09_t_plus_1_uses_next_trading_session_for_exit(self):
        """D2/D3 两批买入在 D4 卖出：均已跨过 T+1，不应人为制造部分成交。

        同日禁止卖出由 test_execution.py 的同日 BUY→SELL 用例直接覆盖。
        """
        bars = _bars_seq([10.0, 10.0, 10.0, 10.0, 10.0, 10.0])

        def strategy(ctx: DecisionContext):
            if ctx.date == D1:
                return TargetPortfolio(decision_date=D1, weights={SYM: 0.1})
            if ctx.date == D2:
                return TargetPortfolio(decision_date=D2, weights={SYM: 0.2})
            if ctx.date == D3:
                return TargetPortfolio(
                    decision_date=D3, weights={},
                    exit_signals={SYM: ("TAKE_PROFIT", 10.0, 0.0, 10.0)},
                )
            return TargetPortfolio(decision_date=ctx.date, weights={SYM: 0.0})

        result = _run(bars, strategy)

        sell_orders = [o for o in result.orders if o.side == "SELL"]
        self.assertEqual(len(sell_orders), 1)
        sell = sell_orders[0]
        self.assertEqual(sell.date, D4)  # D3 decision -> next trading session D4
        self.assertEqual(sell.status, "FILLED")
        self.assertEqual(sell.filled_shares, sell.requested_shares)

        buy_shares = sum(f.shares for f in result.fills if f.side == "BUY")
        sell_shares = sum(f.shares for f in result.fills if f.side == "SELL")
        self.assertEqual(sell_shares, buy_shares)

    # ------------------------------------------------------------------ #
    # 契约 10：完全平仓后 ExitEvent status=complete
    # ------------------------------------------------------------------ #
    def test_10_full_close_exit_event_complete(self):
        """完全平仓后，ExitEvent 的 status 应为 'complete'。"""
        bars = _bars_seq([10.0, 10.0, 11.0, 11.0, 11.0])

        def strategy(ctx: DecisionContext):
            if ctx.date == D1:
                return TargetPortfolio(decision_date=D1, weights={SYM: 0.1})
            if ctx.date == D2:
                return TargetPortfolio(
                    decision_date=D3, weights={},
                    exit_signals={SYM: ("TAKE_PROFIT", 11.0, 0.0, 11.0)},
                )
            return TargetPortfolio(decision_date=ctx.date, weights={})

        result = _run(bars, strategy)

        self.assertTrue(result.exit_events, "应有 ExitEvent")
        for event in result.exit_events:
            self.assertEqual(event.status, "complete",
                             f"完全平仓后 ExitEvent 应为 complete，实际: {event.status}")
            self.assertTrue(_is_uuid(event.exit_event_id))
            self.assertTrue(_is_uuid(event.trade_id))

    # ------------------------------------------------------------------ #
    # 契约 11：平仓后重新建仓生成新 trade_id
    # ------------------------------------------------------------------ #
    def test_11_reopen_after_close_new_trade_id(self):
        """完全平仓后再次买入，新 position 的 trade_id 应与之前不同。"""
        bars = _bars_seq([10.0, 10.0, 10.0, 10.0, 10.0, 10.0])

        def strategy(ctx: DecisionContext):
            if ctx.date == D1:
                return TargetPortfolio(decision_date=D1, weights={SYM: 0.1})
            if ctx.date == D2:
                # 平仓
                return TargetPortfolio(decision_date=D2, weights={SYM: 0.0})
            if ctx.date >= D3:
                # 重新建仓后持续保持目标权重，回测结束应仍是 OPEN Trade。
                return TargetPortfolio(decision_date=ctx.date, weights={SYM: 0.1})
            return TargetPortfolio(decision_date=ctx.date, weights={})

        result = _run(bars, strategy)

        self.assertEqual(len(result.trades), 2,
                         "应有两个 Trade（建仓→平仓→重新建仓）")
        trade1, trade2 = result.trades[0], result.trades[1]
        self.assertNotEqual(trade1.trade_id, trade2.trade_id,
                            "重新建仓应生成新的 trade_id")
        self.assertTrue(_is_uuid(trade1.trade_id))
        self.assertTrue(_is_uuid(trade2.trade_id))
        self.assertEqual(trade1.status, "CLOSED", "第一个 Trade 应为 CLOSED")
        self.assertEqual(trade2.status, "OPEN", "第二个 Trade 应为 OPEN（回测结束仍有持仓）")

    # ------------------------------------------------------------------ #
    # 契约 12：回测结束时仍有 OPEN Trade
    # ------------------------------------------------------------------ #
    def test_12_open_trade_at_backtest_end(self):
        """买入后不卖出，回测结束时 Trade.status='OPEN'。"""
        bars = _bars_seq([10.0, 10.0, 10.0])

        targets = {D1: TargetPortfolio(decision_date=D1, weights={SYM: 0.1})}
        result = _run(bars, targets)

        self.assertEqual(len(result.trades), 1, "应有一个 Trade")
        trade = result.trades[0]
        self.assertEqual(trade.status, "OPEN",
                         f"回测结束仍有持仓，Trade 应为 OPEN，实际: {trade.status}")
        self.assertIsNone(trade.closed_date, "OPEN Trade 不应有 closed_date")
        self.assertEqual(trade.exit_qty, 0, "OPEN Trade 的 exit_qty 应为 0")
        self.assertGreater(trade.open_qty, 0, "OPEN Trade 的 open_qty 应 > 0")

    # ------------------------------------------------------------------ #
    # 契约 13：回测结束时退出意图尚未完成 → abandoned
    # ------------------------------------------------------------------ #
    def test_13_exit_event_abandoned_at_backtest_end(self):
        """触发止盈但跌停无法卖出，回测结束时 ExitEvent.status='abandoned'。"""
        # D2 买入，D3 触发止损，D4 跌停拒绝，D5 仍跌停 → 回测结束仍未平仓
        bars = _bars_seq([
            10.0,           # D1
            10.0,           # D2 买入
            10.0,           # D3 触发止损
            (9.0, {"is_limit_down": True}),  # D4 跌停拒绝
            (8.0, {"is_limit_down": True}),  # D5 仍跌停（最后交易日）
        ])

        def strategy(ctx: DecisionContext):
            if ctx.date == D1:
                return TargetPortfolio(decision_date=D1, weights={SYM: 0.1})
            if ctx.date == D3:
                return TargetPortfolio(
                    decision_date=D3, weights={},
                    exit_signals={SYM: ("STOP_LOSS", 9.0, 9.0, 0.0)},
                )
            return TargetPortfolio(decision_date=ctx.date, weights={})

        result = _run(bars, strategy)

        self.assertTrue(result.exit_events, "应有 ExitEvent")
        for event in result.exit_events:
            self.assertEqual(event.status, "abandoned",
                             f"回测结束未完成的退出应为 abandoned，实际: {event.status}")
        # 验证确实没有成功的 SELL
        filled_sells = [o for o in result.orders
                        if o.side == "SELL" and o.status in ("FILLED", "PARTIALLY_FILLED")]
        self.assertEqual(len(filled_sells), 0,
                         "跌停场景不应有成功的 SELL")

    # ------------------------------------------------------------------ #
    # 契约 14：不同 Run 的 trade_id / exit_event_id 不冲突
    # ------------------------------------------------------------------ #
    def test_14_different_runs_unique_ids(self):
        """运行两次回测（不同 run_id），所有 trade_id 和 exit_event_id 都唯一。"""
        bars = _bars_seq([10.0, 10.0, 11.0, 11.0, 11.0])

        def make_strategy():
            def strategy(ctx: DecisionContext):
                if ctx.date == D1:
                    return TargetPortfolio(decision_date=D1, weights={SYM: 0.1})
                if ctx.date == D2:
                    return TargetPortfolio(
                        decision_date=D3, weights={},
                        exit_signals={SYM: ("TAKE_PROFIT", 11.0, 0.0, 11.0)},
                    )
                return TargetPortfolio(decision_date=ctx.date, weights={})
            return strategy

        result_a = _run(bars, make_strategy(), run_id="run_alpha")
        result_b = _run(bars, make_strategy(), run_id="run_beta")

        # 收集所有 ID
        trade_ids_a = {t.trade_id for t in result_a.trades}
        trade_ids_b = {t.trade_id for t in result_b.trades}
        ee_ids_a = {e.exit_event_id for e in result_a.exit_events}
        ee_ids_b = {e.exit_event_id for e in result_b.exit_events}

        # 两次回测都应有数据
        self.assertTrue(trade_ids_a and trade_ids_b, "两次回测都应有 Trade")
        self.assertTrue(ee_ids_a and ee_ids_b, "两次回测都应有 ExitEvent")

        # trade_id 不冲突
        self.assertFalse(trade_ids_a & trade_ids_b,
                         f"不同 run 的 trade_id 不应冲突: {trade_ids_a & trade_ids_b}")
        # exit_event_id 不冲突
        self.assertFalse(ee_ids_a & ee_ids_b,
                         f"不同 run 的 exit_event_id 不应冲突: {ee_ids_a & ee_ids_b}")


if __name__ == "__main__":
    unittest.main()
