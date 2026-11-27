import unittest

from nooshare_backtest.models import Bar
from nooshare_backtest.rules import (
    R_FAKE_GAP, R_LIMIT_DOWN, R_LIMIT_UP, R_SUSPENDED, R_ST, R_T_PLUS_1, RuleSet,
)


def bar(**kwargs):
    base = dict(date=20240103, symbol="AAA", open=10.0, high=10.1, low=9.9,
                close=10.0, volume=1000, ret=0.01, pre_close=10.0,
                limit_up_ratio=0.10, limit_down_ratio=0.10,
                limit_rule_source="fixture_pit")
    base.update(kwargs)
    return Bar(**base)


class TestRules(unittest.TestCase):
    def setUp(self):
        self.rules = RuleSet()

    def test_normal_bar_allowed(self):
        self.assertEqual(self.rules.can_buy(bar()), "")
        self.assertEqual(self.rules.can_sell(bar(), False), "")

    def test_t_plus_1(self):
        self.assertEqual(self.rules.can_sell(bar(), True), R_T_PLUS_1)

    def test_suspended_and_zero_volume(self):
        self.assertEqual(self.rules.can_buy(bar(is_suspended=True)), R_SUSPENDED)
        self.assertEqual(self.rules.can_buy(bar(volume=0)), R_SUSPENDED)

    def test_limit_and_st(self):
        # Opening prices, not close-state booleans, determine NEXT_OPEN eligibility.
        self.assertEqual(self.rules.can_buy(bar(open=11.0, pre_close=10.0, is_limit_up=False)), R_LIMIT_UP)
        self.assertEqual(self.rules.can_sell(bar(open=9.0, pre_close=10.0, is_limit_down=False), False), R_LIMIT_DOWN)
        self.assertEqual(self.rules.can_buy(bar(is_st=True)), R_ST)
        self.assertEqual(self.rules.can_sell(bar(is_st=True), False), "")

    def test_large_valid_return_is_not_rejected_by_fake_gap_rule(self):
        self.assertEqual(self.rules.can_buy(bar(ret=0.22, no_limit=True)), "")
        self.assertEqual(self.rules.can_sell(bar(ret=-0.22, no_limit=True), False), "")

    def test_missing_data_and_unresolved_price_limit_are_explicit(self):
        from nooshare_backtest.rules import R_NO_PRE_CLOSE, R_UNRESOLVED_LIMIT_RULE
        self.assertNotEqual(self.rules.can_buy(None), "")
        self.assertNotEqual(self.rules.can_sell(None, False), "")
        self.assertEqual(self.rules.can_buy(bar(pre_close=None)), R_NO_PRE_CLOSE)
        self.assertEqual(self.rules.can_buy(bar(limit_up_ratio=None)), R_UNRESOLVED_LIMIT_RULE)


if __name__ == "__main__":
    unittest.main()
