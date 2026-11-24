import unittest

from nooshare_backtest.accounting import Portfolio
from nooshare_backtest.costs import CostModel
from nooshare_backtest.models import Fill


def make_fill(date, symbol, side, shares, px):
    bd = CostModel().breakdown(px, shares, side)
    return Fill(date, symbol, side, shares, bd["price"], bd["commission"],
                bd["stamp_duty"], bd["transfer_fee"], bd["slippage"])


class TestPortfolio(unittest.TestCase):
    def test_buy_then_sell_updates_cash_position_and_ledger(self):
        p = Portfolio(cash=100_000.0)
        buy = make_fill(20240103, "AAA", "BUY", 1_000, 10.0)
        p.apply_fill(buy)
        self.assertEqual(p.shares_of("AAA"), 1_000)
        self.assertAlmostEqual(p.cash, 100_000.0 + buy.net_cash, places=6)
        self.assertAlmostEqual(p.positions["AAA"].avg_cost, -buy.net_cash / 1_000, places=6)
        self.assertEqual(p.bought_today("AAA", 20240103), True)
        self.assertEqual(p.bought_today("AAA", 20240104), False)

        sell = make_fill(20240104, "AAA", "SELL", 1_000, 11.0)
        p.apply_fill(sell)
        self.assertNotIn("AAA", p.positions)
        self.assertAlmostEqual(p.cash, 100_000.0 + buy.net_cash + sell.net_cash, places=6)
        self.assertEqual(len(p.ledger), 2)

    def test_mark_produces_nav_and_snapshot(self):
        from tests.fixtures.sample import sample_bars

        bars = sample_bars()
        p = Portfolio(cash=1_000_000.0)
        point, snaps = p.mark(20240102, bars[20240102], 1_000_000.0)
        self.assertEqual(point.nav, 1.0)
        self.assertEqual(point.cumulative_return, 0.0)
        self.assertEqual(snaps, [])


if __name__ == "__main__":
    unittest.main()
