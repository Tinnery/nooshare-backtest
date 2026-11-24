import unittest

from nooshare_backtest.accounting import Portfolio
from nooshare_backtest.costs import CostModel
from nooshare_backtest.execution import ExecutionConfig, execute_orders
from nooshare_backtest.models import Bar, Order, TargetPortfolio
from nooshare_backtest.rules import R_T_PLUS_1, RuleSet


def bar(px=10.0, **kwargs):
    base = dict(date=20240103, symbol="AAA", open=px, high=px * 1.01,
                low=px * 0.99, close=px, volume=100_000, ret=0.0)
    base.update(kwargs)
    return Bar(**base)


class TestExecution(unittest.TestCase):
    def setUp(self):
        self.cfg = ExecutionConfig()
        self.rules = RuleSet()
        self.costs = CostModel()

    def test_t_plus_1_blocks_same_day_sell_after_buy(self):
        p = Portfolio(cash=100_000.0)
        bars = {bar().symbol: bar()}
        buy = [Order(20240103, "AAA", "BUY", 1_000, 0.5)]
        execute_orders(buy, p, bars, 20240103, self.rules, self.costs, self.cfg)
        self.assertEqual(p.shares_of("AAA"), 1_000)

        sell = [Order(20240103, "AAA", "SELL", 1_000, 0.0)]
        _, orders, devs = execute_orders(sell, p, bars, 20240103, self.rules, self.costs, self.cfg)
        self.assertEqual(orders[0].status, "REJECTED")
        self.assertEqual(orders[0].reason, R_T_PLUS_1)
        self.assertEqual(p.shares_of("AAA"), 1_000)
        self.assertEqual(devs[0].filled_shares, 0)

    def test_cash_constraint_produces_partial_fill_and_deviation(self):
        p = Portfolio(cash=10_500.0)   # 仅够买约 1000 股
        bars = {"AAA": bar()}
        orders = [Order(20240103, "AAA", "BUY", 5_000, 0.5)]
        fills, final, devs = execute_orders(orders, p, bars, 20240103,
                                            self.rules, self.costs, self.cfg)
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0].shares % 100, 0)
        self.assertLess(fills[0].shares, 5_000)
        self.assertEqual(final[0].status, "FILLED")
        self.assertEqual(devs[0].reason, "INSUFFICIENT_CASH")
        self.assertGreaterEqual(p.cash, 0.0)

    def test_blocked_buy_records_reason(self):
        p = Portfolio(cash=100_000.0)
        bars = {"AAA": bar(is_limit_up=True)}
        orders = [Order(20240103, "AAA", "BUY", 1_000, 0.5)]
        fills, final, devs = execute_orders(orders, p, bars, 20240103,
                                            self.rules, self.costs, self.cfg)
        self.assertEqual(fills, [])
        self.assertEqual(final[0].reason, "LIMIT_UP")
        self.assertEqual(devs[0].reason, "LIMIT_UP")

    def test_plan_orders_uses_execution_date(self):
        from nooshare_backtest.execution import plan_orders

        target = TargetPortfolio(decision_date=20240102, weights={"AAA": 0.5})
        orders = plan_orders(target, Portfolio(cash=1_000_000.0),
                             {"AAA": bar(px=10.0, date=20240103)},
                             1_000_000.0, self.cfg, exec_date=20240103)
        self.assertEqual(orders[0].date, 20240103)


if __name__ == "__main__":
    unittest.main()
