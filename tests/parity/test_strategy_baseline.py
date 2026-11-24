"""与 nooshare-strategy 向量化回测基线的口径一致性测试。

基线常量取自 nooshare_strategy/units/backtest.py 与 performance.py：
commission 2.5bp 双边、stamp 5bp 卖出、slippage 5bp 双边、假跳空 0.21、244 日年化。
不 import 基线的私有实现，避免 public repo 对工作区私有代码产生依赖；
基线变更后应同步更新本文件的常量并重新解释差异。
"""
import unittest

from nooshare_backtest.api import run_backtest
from nooshare_backtest.costs import CostModel
from nooshare_backtest.metrics import TRADING_DAYS_PER_YEAR

from tests.fixtures.sample import (
    D2, EXPECTED_AAA_BUY_CASH, sample_bars, sample_decisions,
)

BP = 1e-4
BASELINE_COMMISSION_BP = 2.5
BASELINE_STAMP_BP = 5.0
BASELINE_SLIPPAGE_BP = 5.0
BASELINE_FAKE_GAP = 0.21


class TestStrategyBaseline(unittest.TestCase):
    def test_cost_constants_are_baseline(self):
        cost = CostModel()
        self.assertEqual(cost.commission_bp, BASELINE_COMMISSION_BP)
        self.assertEqual(cost.stamp_bp, BASELINE_STAMP_BP)
        self.assertEqual(cost.slippage_bp, BASELINE_SLIPPAGE_BP)
        self.assertEqual(TRADING_DAYS_PER_YEAR, 244)

    def test_buy_and_sell_formulas_match_baseline_line_by_line(self):
        cost = CostModel()
        for px in (3.33, 10.0, 27.77, 100.01):
            baseline_buy = px * (1 + BASELINE_SLIPPAGE_BP * BP) * (1 + BASELINE_COMMISSION_BP * BP)
            baseline_sell = px * (1 - BASELINE_SLIPPAGE_BP * BP) * (
                1 - (BASELINE_COMMISSION_BP + BASELINE_STAMP_BP) * BP
            )
            self.assertAlmostEqual(cost.buy_price(px), baseline_buy, places=10)
            self.assertAlmostEqual(cost.sell_price(px), baseline_sell, places=10)

    def test_engine_fill_cash_matches_baseline_formula(self):
        result = run_backtest(sample_bars(), sample_decisions(), initial_cash=1_000_000.0)
        aaa = next(f for f in result.fills if f.date == D2 and f.symbol == "AAA")
        baseline_cash = -(10.2 * (1 + BASELINE_SLIPPAGE_BP * BP)
                          * (1 + BASELINE_COMMISSION_BP * BP) * aaa.shares)
        self.assertAlmostEqual(aaa.net_cash, baseline_cash, places=3)
        self.assertAlmostEqual(aaa.net_cash, EXPECTED_AAA_BUY_CASH, places=3)

    def test_fake_gap_limit_matches_baseline(self):
        from nooshare_backtest.rules import RuleSet

        self.assertEqual(RuleSet().fake_gap_limit, BASELINE_FAKE_GAP)


if __name__ == "__main__":
    unittest.main()
