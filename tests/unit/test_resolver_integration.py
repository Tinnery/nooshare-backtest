"""Phase C 集成 Fixture: 真实 Strategy→Engine 路径的涨跌停规则生效验证。

与 E2/E4/E6/E8 的区别：那些 Fixture 手工填 limit_up_price；
本 Fixture 的 Bar 只有 pre_close/exchange/canonical_id，
由 Backtest RuleSet 调用 LimitRuleResolver 按执行日计算限价。
"""
import unittest

from nooshare_backtest.engine import EngineConfig, run
from nooshare_backtest.models import Bar, TargetPortfolio
from nooshare_backtest.rules import LimitRuleResolver, RuleSet

RESOLVER = LimitRuleResolver(pit_rules={
    ("sh", "main_board"): (0.10, 20080228),
    ("sz", "main_board"): (0.10, 20080228),
    ("sh", "star_market"): (0.20, 20190729),
    ("sz", "chi_next"): (0.20, 20200824),
})
RULES = RuleSet(enable_t_plus_1=True, enable_st_filter=False,
                limit_resolver=RESOLVER)


def real_bar(date, symbol, o, c, pre_close, **kw):
    """模拟 Strategy build_bars_by_date 输出：无 limit price，只有身份+raw pre_close。"""
    return Bar(
        date=date, symbol=symbol, open=o, high=max(o, c), low=min(o, c), close=c,
        volume=1_000_000.0, pre_close=pre_close,
        exchange=symbol[:2], canonical_id=symbol[2:], **kw
    )


class TestResolverInRealPath(unittest.TestCase):
    def test_open_limit_up_blocked_via_resolver(self):
        """主板 10%：pre_close=10 -> lu=11，开盘 11 买入应被拒。"""
        s = "sh600000"
        bars = {
            20240102: {s: real_bar(20240102, s, 10.0, 10.0, 9.9)},
            20240103: {s: real_bar(20240103, s, 11.0, 11.0, 10.0)},  # 开盘涨停
        }
        r = run(bars, {20240102: TargetPortfolio(decision_date=20240102,
                                                 weights={s: 0.01})},
                EngineConfig(run_id="c1", rules=RULES))
        self.assertEqual(r.fills, [])
        self.assertEqual(r.orders[0].reason, "OPEN_LIMIT_UP")

    def test_close_limit_touch_allows_open_buy_via_resolver(self):
        """开盘 10.5 低于 lu=11，收盘涨停 -> 买入成交（不用收盘标记）。"""
        s = "sh600000"
        bars = {
            20240102: {s: real_bar(20240102, s, 10.0, 10.0, 9.9)},
            20240103: {s: real_bar(20240103, s, 10.5, 11.0, 10.0, is_limit_up=True)},
        }
        r = run(bars, {20240102: TargetPortfolio(decision_date=20240102,
                                                 weights={s: 0.01})},
                EngineConfig(run_id="c2", rules=RULES))
        self.assertEqual(len(r.fills), 1)
        self.assertEqual(r.orders[0].status, "FILLED")

    def test_bse_30pct_via_resolver(self):
        """北交所 30%：pre_close=10 -> lu=13，开盘 13 买入被拒；12 成交。"""
        s = "bj920002"
        cfg = EngineConfig(run_id="c3", rules=RULES)
        bars = {
            20250225: {s: real_bar(20250225, s, 10.0, 10.0, 9.9)},
            20250226: {s: real_bar(20250226, s, 13.0, 13.0, 10.0)},
        }
        r = run(bars, {20250225: TargetPortfolio(decision_date=20250225,
                                                 weights={s: 0.01})}, cfg)
        self.assertEqual(len(r.orders), 1)
        self.assertEqual(r.orders[0].reason, "OPEN_LIMIT_UP")
        bars2 = {
            20250225: {s: real_bar(20250225, s, 10.0, 10.0, 9.9)},
            20250226: {s: real_bar(20250226, s, 12.0, 13.0, 10.0)},
        }
        r2 = run(bars2, {20250225: TargetPortfolio(decision_date=20250225,
                                                  weights={s: 0.01})},
                 EngineConfig(run_id="c3b", rules=RULES))
        self.assertEqual(len(r2.fills), 1)

    def test_st_5pct_via_resolver(self):
        """ST 主板 5%：pre_close=5.95 -> lu=6.25，开盘 6.25 买入被拒。"""
        s = "sh600057"
        bars = {
            20240102: {s: real_bar(20240102, s, 5.95, 5.95, 5.90, is_st=True)},
            20240103: {s: real_bar(20240103, s, 6.25, 6.25, 5.95, is_st=True)},
        }
        r = run(bars, {20240102: TargetPortfolio(decision_date=20240102,
                                                 weights={s: 0.01})},
                EngineConfig(run_id="c4", rules=RULES))
        self.assertEqual(r.orders[0].reason, "OPEN_LIMIT_UP")

    def test_ipo_no_limit_window_via_resolver(self):
        """IPO 首日无限制：pre_close=10，开盘 15（+50%）仍成交。"""
        s = "sh600930"
        bars = {
            20240102: {s: real_bar(20240102, s, 10.0, 10.0, 9.9)},
            20240103: {s: real_bar(20240103, s, 15.0, 16.0, 10.0,
                                   trading_days_since_list=1)},
        }
        r = run(bars, {20240102: TargetPortfolio(decision_date=20240102,
                                                 weights={s: 0.015})},
                EngineConfig(run_id="c5", rules=RULES))
        self.assertEqual(len(r.fills), 1)
        self.assertEqual(r.orders[0].status, "FILLED")


if __name__ == "__main__":
    unittest.main()
