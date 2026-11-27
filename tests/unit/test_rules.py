import unittest

from nooshare_backtest.models import Bar
from nooshare_backtest.rules import (
    R_NO_DATA, R_NO_SELLABLE, R_NOT_LISTED, R_OPEN_LIMIT_DOWN, R_OPEN_LIMIT_UP,
    R_ST, R_SUSPENDED, R_T_PLUS_1, R_ZERO_VOLUME, LimitRuleResolver, RuleSet,
    limit_prices,
)


def bar(**kwargs):
    """默认正常 Bar：raw open=10, pre_close=10, 涨停 11 / 跌停 9（手算 10%）。"""
    base = dict(date=20240103, symbol="sh600000", open=10.0, high=10.1, low=9.9,
                close=10.0, volume=1000, ret=0.01,
                pre_close=10.0, limit_up_price=11.0, limit_down_price=9.0)
    base.update(kwargs)
    return Bar(**base)


class TestOpenStateRules(unittest.TestCase):
    """V0.4 判定语义：开盘状态决定成交，收盘标记不参与。"""

    def setUp(self):
        self.rules = RuleSet()

    # ---- 买入 ----
    def test_normal_bar_allowed(self):
        self.assertEqual(self.rules.can_buy(bar()), "")
        self.assertEqual(self.rules.can_sell(bar(), 1000, 1000), "")

    def test_open_limit_up_blocks_buy(self):
        self.assertEqual(self.rules.can_buy(bar(open=11.0)), R_OPEN_LIMIT_UP)
        # 一字板 O=H=L=C=涨停价
        one_char = bar(open=11.0, high=11.0, low=11.0, close=11.0)
        self.assertEqual(self.rules.can_buy(one_char), R_OPEN_LIMIT_UP)

    def test_close_limit_mark_does_not_block_open_buy(self):
        # 核心新语义：开盘低于涨停价、收盘涨停 → 开盘买入允许成交
        self.assertEqual(
            self.rules.can_buy(bar(open=10.5, close=11.0, is_limit_up=True)), "")

    def test_no_limit_day_allows_buy_at_any_price(self):
        self.assertEqual(self.rules.can_buy(bar(open=15.0, no_limit=True)), "")

    # ---- 卖出 ----
    def test_open_limit_down_blocks_sell(self):
        self.assertEqual(self.rules.can_sell(bar(open=9.0), 1000, 1000), R_OPEN_LIMIT_DOWN)

    def test_close_limit_mark_does_not_block_open_sell(self):
        # 开盘高于跌停价、收盘跌停 → 开盘卖出允许成交
        self.assertEqual(
            self.rules.can_sell(bar(open=9.5, close=9.0, is_limit_down=True), 1000, 1000), "")

    # ---- 账务前置条件 ----
    def test_t_plus_1_and_no_sellable(self):
        self.assertEqual(self.rules.can_sell(bar(), 0, 1000), R_T_PLUS_1)
        self.assertEqual(self.rules.can_sell(bar(), 0, 0), R_NO_SELLABLE)

    # ---- 不可交易日 ----
    def test_suspended_and_zero_volume(self):
        self.assertEqual(self.rules.can_buy(bar(is_suspended=True)), R_SUSPENDED)
        self.assertEqual(self.rules.can_buy(bar(volume=0)), R_ZERO_VOLUME)
        self.assertEqual(self.rules.can_sell(bar(volume=0), 1000, 1000), R_ZERO_VOLUME)

    def test_missing_data_and_not_listed(self):
        self.assertEqual(self.rules.can_buy(None), R_NO_DATA)
        self.assertEqual(
            self.rules.can_buy(bar(list_date=20240105)), R_NOT_LISTED)

    def test_st_policy(self):
        self.assertEqual(self.rules.can_buy(bar(is_st=True)), R_ST)      # 政策过滤
        self.assertEqual(self.rules.can_sell(bar(is_st=True), 1000, 1000), "")  # 卖出不限

    def test_missing_limit_price_conservatively_rejected(self):
        # V0.7 P0-4：涨跌停价不可判定（pre_close 缺失）时保守拒绝，
        # 防止开盘涨跌停锁定状态下以不真实价格成交。
        from nooshare_backtest.rules import R_LIMIT_REF_UNKNOWN
        self.assertEqual(self.rules.can_buy(bar(limit_up_price=None)),
                         R_LIMIT_REF_UNKNOWN)


class TestLimitRuleResolver(unittest.TestCase):
    """按执行日解析涨跌幅规则，全部手算断言。"""

    def setUp(self):
        self.resolver = LimitRuleResolver(pit_rules={
            ("sh", "main_board"): (0.10, 20080228),
            ("sz", "main_board"): (0.10, 20080228),
            ("sh", "star_market"): (0.20, 20190729),
            ("sz", "chi_next"): (0.20, 20200824),
        })

    def test_security_type_inference(self):
        self.assertEqual(self.resolver.security_type("sh", "600000"), "main_board")
        self.assertEqual(self.resolver.security_type("sh", "688981"), "star_market")
        self.assertEqual(self.resolver.security_type("sz", "300750"), "chi_next")
        self.assertEqual(self.resolver.security_type("bj", "920002"), "bse")

    def test_pit_rules_win(self):
        r = self.resolver.resolve("sh", "600000", False, 20250102, None)
        self.assertEqual(r.ratio, 0.10)
        self.assertEqual(r.source, "pit_table")

    def test_star_chinext_bse_ratios(self):
        self.assertEqual(self.resolver.resolve("sh", "688981", False, 20250102, None).ratio, 0.20)
        self.assertEqual(self.resolver.resolve("sz", "300750", False, 20250102, None).ratio, 0.20)
        r = self.resolver.resolve("bj", "920002", False, 20250102, None)
        self.assertEqual(r.ratio, 0.30)
        self.assertEqual(r.source, "derived")

    def test_st_main_board_5pct(self):
        r = self.resolver.resolve("sh", "600057", True, 20250114, None)
        self.assertEqual(r.ratio, 0.05)

    def test_sh_st_main_board_10pct_after_20260706(self):
        r = self.resolver.resolve("sh", "600057", True, 20260706, None)
        self.assertEqual(r.ratio, 0.10)

    def test_sz_st_main_board_10pct_after_20260706(self):
        """SZ 主板 ST 2026-07-06 后 10%（深交所同日调整，量化报告证实）。
        依据：st-limit-quantification-report.md — 87 证券 / 6,465 差异日。"""
        r = self.resolver.resolve("sz", "000972", True, 20260706, None)
        self.assertEqual(r.ratio, 0.10)
        self.assertEqual(r.source, "derived")

    def test_st_main_board_5pct_before_20260706_both_exchanges(self):
        """2026-07-06 前一日（边界）：沪深主板 ST 仍 5%。"""
        self.assertEqual(self.resolver.resolve("sh", "600057", True, 20260703, None).ratio, 0.05)
        self.assertEqual(self.resolver.resolve("sz", "000972", True, 20260703, None).ratio, 0.05)

    def test_ipo_no_limit_windows(self):
        # 主板首日无限制
        r = self.resolver.resolve("sh", "600930", False, 20250716, 1)
        self.assertTrue(r.no_limit)
        # STAR 上市前 5 个交易日无限制（样本 601026 上市首日 +19.8%）
        self.assertTrue(self.resolver.resolve("sh", "688xxx", False, 20251017, 3).no_limit)
        self.assertFalse(self.resolver.resolve("sh", "688xxx", False, 20251017, 6).no_limit)
        # ChiNext 同理
        self.assertTrue(self.resolver.resolve("sz", "301xxx", False, 20251015, 5).no_limit)
        # BSE 首日
        self.assertTrue(self.resolver.resolve("bj", "920xxx", False, 20250102, 1).no_limit)


class TestLimitPrices(unittest.TestCase):
    """涨跌停价手算：round(pre_close × (1±ratio), 2)。"""

    def test_main_board_10pct(self):
        lu, ld = limit_prices(10.0, self.resolver_rule(0.10))
        self.assertEqual((lu, ld), (11.00, 9.00))

    def test_st_5pct_rounding(self):
        # Phase 0 真实样本：sh600057 pc=5.95 -> 收盘 6.25 = round(5.95*1.05, 2)
        lu, ld = limit_prices(5.95, self.resolver_rule(0.05))
        self.assertEqual((lu, ld), (6.25, 5.65))

    def test_no_limit_returns_none(self):
        lu, ld = limit_prices(10.0, self.resolver_rule(0.0, no_limit=True))
        self.assertEqual((lu, ld), (None, None))

    def test_invalid_pre_close(self):
        lu, ld = limit_prices(None, self.resolver_rule(0.10))
        self.assertEqual((lu, ld), (None, None))

    @staticmethod
    def resolver_rule(ratio, no_limit=False):
        from nooshare_backtest.rules import LimitRule
        return LimitRule(ratio=ratio, no_limit=no_limit)


if __name__ == "__main__":
    unittest.main()
