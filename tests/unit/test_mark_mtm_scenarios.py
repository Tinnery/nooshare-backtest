"""盯市 6 场景矩阵补全（任务书 §7.2）。

现有 tests/unit/test_accounting.py 已覆盖：
  - close 有效 + mark NULL           → fresh
  - mark NaN + close 有效            → fresh
  - 缺 Bar（停牌）                   → last_valid_mark + stale
  - Inf / NaN 价格                   → 不作为有效价格
  - 从未有过有效价格                 → 明确失败

本文件补齐剩余两格：
  - 停牌后复牌：复牌当日必须回到当日真实价格并恢复 fresh
  - mark 有值但 close 缺失/无效：当日有 mark 时以 mark 为准（fresh）
  - 以及 Raw 执行边界：Bar 不得携带 HFQ 盯市字段
"""
import unittest

from nooshare_backtest.accounting import Portfolio
from nooshare_backtest.models import Bar, Fill


def _portfolio_with_position(price=10.0, shares=100):
    pf = Portfolio(cash=100_000.0 - price * shares)
    pf.apply_fill(Fill(20260101, "sh600000", "BUY", shares, price,
                       commission=0.0, stamp_duty=0.0, transfer_fee=0.0,
                       slippage=0.0, trade_id="t1"))
    return pf


def _bar(symbol="sh600000", close=10.0, mark_price=None, date=20260101):
    return Bar(date, symbol, close, close, close, close, mark_price=mark_price)


class TestResumeAfterSuspension(unittest.TestCase):
    """停牌 → 复牌：stale 只在停牌期间，复牌日必须 fresh。"""

    def test_resume_day_returns_to_real_price_and_fresh(self):
        pf = _portfolio_with_position()
        initial = 100_000.0

        # D1 正常交易：fresh
        _, snaps = pf.mark(20260101, {"sh600000": _bar(close=10.0)}, initial)
        self.assertFalse(snaps[-1].price_stale)
        self.assertEqual(snaps[-1].last_price, 10.0)

        # D2 / D3 停牌（无 Bar）：stale，价格停在最近有效盯市价
        for d in (20260102, 20260103):
            _, snaps = pf.mark(d, {}, initial)
            self.assertTrue(snaps[-1].price_stale,
                            f"{d} 停牌无 Bar 必须标 stale")
            self.assertEqual(snaps[-1].last_price, 10.0)

        # D4 复牌，真实价 12.0：必须 fresh 且用当日价
        _, snaps = pf.mark(20260104, {"sh600000": _bar(close=12.0, date=20260104)}, initial)
        self.assertFalse(snaps[-1].price_stale,
                         "复牌日使用当日真实价格，不得继续标 stale")
        self.assertEqual(snaps[-1].last_price, 12.0)
        self.assertEqual(pf.last_valid_mark["sh600000"], 12.0)

    def test_resume_with_lower_price_is_reflected(self):
        """复牌价低于停牌前：盯市必须反映真实下跌。"""
        pf = _portfolio_with_position(price=20.0, shares=100)
        initial = 100_000.0
        pf.mark(20260101, {"sh600000": _bar(close=20.0)}, initial)
        pf.mark(20260102, {}, initial)  # 停牌
        _, snaps = pf.mark(20260103,
                           {"sh600000": _bar(close=15.0, date=20260103)}, initial)
        self.assertFalse(snaps[-1].price_stale)
        self.assertEqual(snaps[-1].last_price, 15.0)
        self.assertEqual(snaps[-1].market_value, 1_500.0)


class TestMarkPriceWithoutClose(unittest.TestCase):
    """当日 Bar 存在时，mark_price 优先于 close；两者都无效才回退 stale。"""

    def test_mark_present_close_none_is_fresh(self):
        pf = _portfolio_with_position()
        b = Bar(20260101, "sh600000", 0.0, 0.0, 0.0, None, mark_price=11.0)
        _, snaps = pf.mark(20260101, {"sh600000": b}, 100_000.0)
        self.assertFalse(snaps[-1].price_stale,
                         "当日有 mark_price 即当日真实价格，不是陈旧回退")
        self.assertEqual(snaps[-1].last_price, 11.0)

    def test_close_invalid_mark_valid_uses_mark(self):
        pf = _portfolio_with_position()
        b = Bar(20260101, "sh600000", 0.0, 0.0, 0.0, 0.0, mark_price=9.5)
        _, snaps = pf.mark(20260101, {"sh600000": b}, 100_000.0)
        self.assertFalse(snaps[-1].price_stale)
        self.assertEqual(snaps[-1].last_price, 9.5)

    def test_mark_and_close_both_invalid_falls_back_stale(self):
        """mark 非有限且 close 无效，但有历史价 → 回退历史价并 stale。"""
        pf = _portfolio_with_position()
        pf.mark(20260101, {"sh600000": _bar(close=10.0)}, 100_000.0)
        b = Bar(20260102, "sh600000", 0.0, 0.0, 0.0, None,
                mark_price=float("nan"))
        _, snaps = pf.mark(20260102, {"sh600000": b}, 100_000.0)
        self.assertTrue(snaps[-1].price_stale)
        self.assertEqual(snaps[-1].last_price, 10.0)


class TestRawExecutionBoundary(unittest.TestCase):
    def test_bar_has_no_hfq_mark_field(self):
        """Bar 不得带 HFQ 盯市字段：执行/盯市只用 Raw 口径。"""
        b = _bar()
        self.assertFalse(hasattr(b, "close_hfq"))
        self.assertFalse(hasattr(b, "mark_price_hfq"))


if __name__ == "__main__":
    unittest.main()