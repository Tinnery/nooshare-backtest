"""普通调仓 SELL 和加仓 BUY 携带 trade_id 测试。

覆盖：
- 普通目标权重产生的 SELL Order 携带 trade_id
- 加仓 BUY Order 携带现有 trade_id
- 首次 BUY Order 的 trade_id 为空
- SELL Fill 继承 SELL Order 的 trade_id
- Rejected SELL 不生成 Fill，但 Order 身份不丢失
"""
import unittest

from nooshare_backtest.engine import DecisionContext, EngineConfig, run
from nooshare_backtest.models import Bar, TargetPortfolio


def _bar(date: int, symbol: str, o: float, h: float, l: float, c: float,
         is_limit_down: bool = False, is_limit_up: bool = False) -> Bar:
    return Bar(
        date=date, symbol=symbol, open=o, high=h, low=l, close=c,
        volume=1_000_000.0, ret=0.0,
        is_limit_down=is_limit_down, is_limit_up=is_limit_up,
        canonical_id=symbol[-6:], exchange=symbol[:2],
    )


class TestNormalRebalanceSellCarriesTradeId(unittest.TestCase):
    """普通目标权重产生的 SELL Order 携带 trade_id。"""

    def test_normal_sell_order_carries_trade_id(self):
        """普通调仓（非退出事件）产生的 SELL Order 携带现有持仓的 trade_id。"""
        bars = {
            20240101: {"sh600001": _bar(20240101, "sh600001", 10.0, 10.5, 9.8, 10.2)},
            20240102: {"sh600001": _bar(20240102, "sh600001", 10.2, 10.6, 10.0, 10.4)},
            20240103: {"sh600001": _bar(20240103, "sh600001", 10.4, 10.5, 10.2, 10.3)},
            20240104: {"sh600001": _bar(20240104, "sh600001", 10.3, 10.6, 10.2, 10.5)},
        }

        def decide(ctx: DecisionContext) -> TargetPortfolio | None:
            if ctx.date == 20240101:
                # 首次建仓
                return TargetPortfolio(decision_date=ctx.date, weights={"sh600001": 0.5})
            if ctx.date == 20240102:
                # 普通调仓：降低权重（从 0.5 到 0.25），不涉及退出事件
                return TargetPortfolio(decision_date=ctx.date, weights={"sh600001": 0.25})
            if ctx.date == 20240103:
                # 清仓
                return TargetPortfolio(decision_date=ctx.date, weights={})
            return None

        result = run(bars, decide, EngineConfig(run_id="normal-sell"))

        # 找到普通调仓产生的 SELL 订单（20240103 执行，来自 20240102 的决策）
        sell_orders = [o for o in result.orders if o.side == "SELL"]
        self.assertGreater(len(sell_orders), 0, "应该有 SELL 订单")

        # 第一个 SELL 订单应该携带 trade_id（来自普通调仓，非退出事件）
        first_sell = sell_orders[0]
        self.assertTrue(first_sell.trade_id, "普通调仓 SELL Order 应该携带 trade_id")
        # 不是通过退出事件触发的
        self.assertEqual(first_sell.exit_event_id, "", "普通调仓 SELL 不应该有 exit_event_id")
        self.assertEqual(first_sell.decision_reason, "", "普通调仓 SELL 不应该有 decision_reason")

        # SELL Fill 应该继承 Order 的 trade_id
        sell_fills = [f for f in result.fills if f.side == "SELL"]
        self.assertGreater(len(sell_fills), 0, "应该有 SELL Fill")
        self.assertEqual(sell_fills[0].trade_id, first_sell.trade_id,
                        "SELL Fill 应该继承 SELL Order 的 trade_id")


class TestAddPositionBuyCarriesTradeId(unittest.TestCase):
    """加仓 BUY Order 携带现有 trade_id。"""

    def test_add_position_buy_carries_trade_id(self):
        """对现有持仓加仓的 BUY Order 携带现有持仓的 trade_id。"""
        bars = {
            20240101: {"sh600001": _bar(20240101, "sh600001", 10.0, 10.5, 9.8, 10.2)},
            20240102: {"sh600001": _bar(20240102, "sh600001", 10.2, 10.6, 10.0, 10.4)},
            20240103: {"sh600001": _bar(20240103, "sh600001", 10.4, 10.5, 10.2, 10.3)},
            20240104: {"sh600001": _bar(20240104, "sh600001", 10.3, 10.6, 10.2, 10.5)},
        }

        def decide(ctx: DecisionContext) -> TargetPortfolio | None:
            if ctx.date == 20240101:
                # 首次建仓
                return TargetPortfolio(decision_date=ctx.date, weights={"sh600001": 0.3})
            if ctx.date == 20240102:
                # 加仓（提高权重从 0.3 到 0.6）
                return TargetPortfolio(decision_date=ctx.date, weights={"sh600001": 0.6})
            if ctx.date == 20240103:
                # 清仓
                return TargetPortfolio(decision_date=ctx.date, weights={})
            return None

        result = run(bars, decide, EngineConfig(run_id="add-buy"))

        # 找到 BUY 订单
        buy_orders = [o for o in result.orders if o.side == "BUY"]
        self.assertEqual(len(buy_orders), 2, "应该有 2 个 BUY 订单（首次建仓 + 加仓）")

        # 第一个 BUY 订单（首次建仓）trade_id 应该为空
        first_buy = buy_orders[0]
        self.assertEqual(first_buy.trade_id, "",
                        "首次 BUY Order 的 trade_id 应该为空（尚未成交）")

        # 第二个 BUY 订单（加仓）应该携带现有持仓的 trade_id
        second_buy = buy_orders[1]
        self.assertTrue(second_buy.trade_id,
                       "加仓 BUY Order 应该携带现有持仓的 trade_id")

        # 验证 trade_id 与 Trade 的关联
        self.assertEqual(len(result.trades), 1, "应该只有 1 个 Trade")
        self.assertEqual(second_buy.trade_id, result.trades[0].trade_id,
                        "加仓 BUY 的 trade_id 应该与 Trade 的 trade_id 一致")


class TestRejectedSellPreservesIdentity(unittest.TestCase):
    """Rejected SELL 不生成 Fill，但 Order 身份不丢失。"""

    def test_rejected_sell_no_fill_but_order_preserved(self):
        """跌停拒绝的 SELL 不生成 Fill，但 Order 保留 trade_id 和 exit_event_id。"""
        bars = {
            20240101: {"sh600001": _bar(20240101, "sh600001", 10.0, 10.5, 9.8, 10.2)},
            20240102: {"sh600001": _bar(20240102, "sh600001", 10.2, 10.6, 10.0, 10.4)},
            20240103: {"sh600001": _bar(20240103, "sh600001", 10.4, 10.5, 9.0, 9.2, is_limit_down=True)},
            20240104: {"sh600001": _bar(20240104, "sh600001", 9.2, 9.5, 9.0, 9.4)},
        }

        def decide(ctx: DecisionContext) -> TargetPortfolio | None:
            if ctx.date == 20240101:
                return TargetPortfolio(decision_date=ctx.date, weights={"sh600001": 0.5})
            if ctx.date == 20240102:
                # 普通调仓：清仓
                return TargetPortfolio(decision_date=ctx.date, weights={})
            return None

        result = run(bars, decide, EngineConfig(run_id="rejected-sell"))

        # 找到 SELL 订单
        sell_orders = [o for o in result.orders if o.side == "SELL"]
        self.assertGreater(len(sell_orders), 0, "应该有 SELL 订单")

        # 第一个 SELL 订单应该被拒绝（跌停）
        first_sell = sell_orders[0]
        self.assertEqual(first_sell.status, "REJECTED", "第一个 SELL 应该被拒绝")
        self.assertEqual(first_sell.reason, "LIMIT_DOWN", "拒绝原因应该是 LIMIT_DOWN")

        # 但 Order 身份不丢失
        self.assertTrue(first_sell.trade_id, "Rejected SELL Order 应该保留 trade_id")

        # 不生成 Fill（通过检查 fill 的 order_id）
        fills_for_rejected = [f for f in result.fills if f.order_id == first_sell.order_id]
        self.assertEqual(len(fills_for_rejected), 0,
                        "Rejected SELL 不应该生成 Fill")


if __name__ == "__main__":
    unittest.main()
