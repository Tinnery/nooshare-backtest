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


class TestMarkPriceFiniteGuard(unittest.TestCase):
    """mark() 对非有限价格（inf）必须回退，禁止污染 market_value/nav。

    NaN 已被 >0 判断天然排除；inf > 0 为 True，需要 isfinite 显式防护
    （甄别自 Backtest PR #5 0678a21/4e8e12b，按 V0.7 结构实现）。"""

    def _mark_with_mark_price(self, mark_price):
        from nooshare_backtest.accounting import Portfolio
        from nooshare_backtest.models import Bar, Fill
        pf = Portfolio(cash=99_000.0)
        pf.apply_fill(Fill(20240102, "sh600519", "BUY", 100, 10.0,
                           commission=5.0, stamp_duty=0.0, transfer_fee=1.0,
                           slippage=0.0, trade_id="t1"))
        bars = {"sh600519": Bar(20240103, "sh600519", 10.0, 10.0, 10.0, 10.0,
                                mark_price=mark_price)}
        return pf.mark(20240103, bars, 100_000.0)

    def test_inf_mark_price_falls_back_to_raw_close(self):
        eq, snaps = self._mark_with_mark_price(float("inf"))
        self.assertTrue(eq.nav == eq.nav, "nav 不得为 NaN")
        self.assertLess(eq.nav, 2.0, "100 股按 Raw close 10 盯市 nav 应接近 1.0 而非 inf")
        self.assertEqual(snaps[-1].last_price, 10.0)

    def test_nan_mark_price_falls_back_to_raw_close(self):
        eq, snaps = self._mark_with_mark_price(float("nan"))
        self.assertTrue(eq.nav == eq.nav, "nav 不得为 NaN")
        self.assertEqual(snaps[-1].last_price, 10.0)


if __name__ == "__main__":
    unittest.main()
