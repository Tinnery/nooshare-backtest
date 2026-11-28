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

class TestMissingBarMarking(unittest.TestCase):
    """Round-4 §9.1 手算 Fixture：T-1 有效 Raw close → T 停牌缺失 Bar
    → T+1 复牌。

    合同：缺失行情日的持仓盯市用最近一次有效 Raw close（陈旧但真实），
    绝不回退到持仓成本——成本回退会把估值从真实最后收盘价人为拉回
    建仓成本（10 → 12 的虚假价格跳变）。快照 price_stale 显式区分
    当前有效行情与陈旧盯市价格。"""

    def _portfolio_with_position(self):
        from nooshare_backtest.accounting import Portfolio
        pf = Portfolio(cash=100_000.0)
        # 成本 12 元/股（含费用），1000 股 → cost 12,000
        buy = make_fill(20240101, "AAA", "BUY", 1_000, 12.0)
        pf.apply_fill(buy)
        return pf

    def _bars(self, date, close):
        from nooshare_backtest.models import Bar
        return {"AAA": Bar(date, "AAA", close, close, close, close,
                           mark_price=close)}

    def test_suspension_day_marks_at_last_valid_close_not_cost(self):
        pf = self._portfolio_with_position()
        # T-1: 真实 Raw close 10.0（低于成本 12）
        eq1, snaps1 = pf.mark(20240102, self._bars(20240102, 10.0), 100_000.0)
        self.assertEqual(snaps1[-1].last_price, 10.0)
        self.assertFalse(snaps1[-1].price_stale)
        # 手算：market_value = 10 * 1000 = 10,000（不是成本 12,000）
        self.assertAlmostEqual(snaps1[-1].market_value, 10_000.0, places=6)

        # T: 停牌，无 Bar
        eq2, snaps2 = pf.mark(20240103, {}, 100_000.0)
        self.assertEqual(snaps2[-1].last_price, 10.0,
                         "停牌日必须盯在最后有效 Raw close，不得回退成本 12")
        self.assertTrue(snaps2[-1].price_stale,
                        "陈旧盯市必须显式标记 price_stale=True")
        # 手算：估值保持 10,000，无虚假跳变
        self.assertAlmostEqual(snaps2[-1].market_value, 10_000.0, places=6)
        self.assertAlmostEqual(eq2.market_value, 10_000.0, places=6)

        # T+1: 复牌，真实 Raw close 11.0
        eq3, snaps3 = pf.mark(20240104, self._bars(20240104, 11.0), 100_000.0)
        self.assertEqual(snaps3[-1].last_price, 11.0)
        self.assertFalse(snaps3[-1].price_stale,
                         "复牌日恢复真实 Raw close 盯市，不再是陈旧价")
        self.assertAlmostEqual(snaps3[-1].market_value, 11_000.0, places=6)

    def test_inf_mark_price_day_treated_as_stale_and_falls_back(self):
        pf = self._portfolio_with_position()
        # T-1 valid close
        pf.mark(20240102, self._bars(20240102, 10.0), 100_000.0)
        # T: bar 存在但 mark_price 与 close 全为 inf（污染数据）
        from nooshare_backtest.models import Bar
        bad = {"AAA": Bar(20240103, "AAA", float("inf"), float("inf"),
                          float("inf"), float("inf"),
                          mark_price=float("inf"))}
        eq, snaps = pf.mark(20240103, bad, 100_000.0)
        self.assertEqual(snaps[-1].last_price, 10.0)
        self.assertTrue(snaps[-1].price_stale)
        self.assertAlmostEqual(eq.market_value, 10_000.0, places=6)

    def test_never_marked_position_without_bar_raises(self):
        """从未取得有效价格（上游数据损坏）必须显式失败，不得静默按
        成本伪造估值。"""
        pf = self._portfolio_with_position()
        with self.assertRaises(ValueError):
            pf.mark(20240102, {}, 100_000.0)


class TestPriceStaleSemantics(unittest.TestCase):
    """Round-5 纠偏：price_stale 只在真正使用历史 last_valid_mark 时为 True。

    当日 Bar 存在、mark_price 缺失/无效但 Raw close 有效时，
    实际使用的是当日真实价格，不得误标为陈旧。"""

    def _pf(self):
        from nooshare_backtest.accounting import Portfolio
        pf = Portfolio(cash=100_000.0)
        pf.apply_fill(make_fill(20240101, "AAA", "BUY", 1_000, 12.0))
        return pf

    def test_mark_price_none_but_close_valid_is_not_stale(self):
        from nooshare_backtest.models import Bar
        pf = self._pf()
        bars = {"AAA": Bar(20240102, "AAA", 11.0, 11.0, 11.0, 11.0,
                           mark_price=None)}
        eq, snaps = pf.mark(20240102, bars, 100_000.0)
        s = snaps[-1]
        self.assertEqual(s.last_price, 11.0, "应用当日 Raw close")
        self.assertFalse(s.price_stale,
                         "当日真实价格盯市不得误标 stale")
        self.assertAlmostEqual(s.market_value, 11_000.0, places=6)

    def test_mark_price_nan_but_close_valid_is_not_stale(self):
        from nooshare_backtest.models import Bar
        pf = self._pf()
        bars = {"AAA": Bar(20240102, "AAA", 11.0, 11.0, 11.0, 11.0,
                           mark_price=float("nan"))}
        eq, snaps = pf.mark(20240102, bars, 100_000.0)
        self.assertEqual(snaps[-1].last_price, 11.0)
        self.assertFalse(snaps[-1].price_stale)

    def test_consecutive_suspension_days_all_stale_at_last_close(self):
        from nooshare_backtest.models import Bar
        pf = self._pf()
        pf.mark(20240102, {"AAA": Bar(20240102, "AAA", 10.0, 10.0, 10.0, 10.0,
                                      mark_price=10.0)}, 100_000.0)
        # 连续 3 天停牌
        for d in (20240103, 20240104, 20240105):
            eq, snaps = pf.mark(d, {}, 100_000.0)
            self.assertEqual(snaps[-1].last_price, 10.0)
            self.assertTrue(snaps[-1].price_stale)
            self.assertAlmostEqual(eq.market_value, 10_000.0, places=6)
        # 复牌
        eq, snaps = pf.mark(20240106, {"AAA": Bar(20240106, "AAA", 9.5, 9.5, 9.5, 9.5,
                                                  mark_price=9.5)}, 100_000.0)
        self.assertEqual(snaps[-1].last_price, 9.5)
        self.assertFalse(snaps[-1].price_stale)
