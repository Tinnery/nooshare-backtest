import unittest

from nooshare_backtest.costs import CostModel


class TestCostModel(unittest.TestCase):
    def test_buy_formula_matches_hand_calculation(self):
        cost = CostModel()
        # 1bp = 1e-4：10 * (1+5bp) * (1+2.5bp)
        self.assertAlmostEqual(cost.buy_price(10.0), 10.00750125, places=9)

    def test_sell_formula_matches_hand_calculation(self):
        cost = CostModel()
        # 10 * (1-5bp) * (1 - 2.5bp - 5bp)
        self.assertAlmostEqual(cost.sell_price(10.0), 9.98750375, places=9)

    def test_breakdown_net_cash_equals_baseline_formula(self):
        cost = CostModel()
        shares = 10_000
        bd = cost.breakdown(10.0, shares, "BUY")
        paid = bd["price"] * shares + bd["commission"] + bd["transfer_fee"]
        self.assertAlmostEqual(paid, cost.buy_price(10.0) * shares, places=4)

        bd = cost.breakdown(10.0, shares, "SELL")
        got = bd["price"] * shares - bd["commission"] - bd["stamp_duty"] - bd["transfer_fee"]
        self.assertAlmostEqual(got, cost.sell_price(10.0) * shares, places=4)
        self.assertAlmostEqual(bd["stamp_duty"], 10.0 * 0.9995 * shares * 0.0005, places=6)


if __name__ == "__main__":
    unittest.main()
