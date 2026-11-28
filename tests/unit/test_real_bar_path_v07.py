"""Phase 4 手算 Fixture：真实 Bar 路径的执行边界（V0.7）。

覆盖任务 6.6 要求的三类新增场景（引擎级，纯合成）：
E19  缺失 Raw pre_close → 保守拒绝 LIMIT_REFERENCE_UNKNOWN（P0-4）
E20  停牌后恢复交易：停牌日不成交，恢复日按恢复日行情成交
E21  无行情订单：执行日无 bar → NO_MARKET_DATA，现金与持仓不变
"""
import unittest

from nooshare_backtest.engine import DecisionContext, EngineConfig, run
from nooshare_backtest.models import Bar, TargetPortfolio

SYM = "sh600519"


def bar(date, o, c, *, volume=1_000_000.0, suspended=False, pre_close=None,
        list_date=None):
    return Bar(date=date, symbol=SYM, open=o, high=max(o, c) * 1.01,
               low=min(o, c) * 0.99, close=c, volume=volume, ret=0.0,
               pre_close=pre_close, is_suspended=suspended,
               list_date=list_date,
               canonical_id=SYM[2:], exchange="sh")


class TestMissingPreCloseConservativeRejection(unittest.TestCase):
    """E19: pre_close 缺失 → LIMIT_REFERENCE_UNKNOWN，无成交无现金变化"""

    def test_buy_rejected_without_pre_close(self):
        bars = {
            20240102: {SYM: bar(20240102, 10.0, 10.0, pre_close=10.0)},
            # 执行日 pre_close=None 且未预填限价 → 涨跌停不可判定
            20240103: {SYM: bar(20240103, 10.0, 10.0, pre_close=None)},
        }

        def decide(ctx: DecisionContext):
            if ctx.date == 20240102:
                return TargetPortfolio(decision_date=ctx.date, weights={SYM: 0.5})
            return None

        result = run(bars, decide, EngineConfig(run_id="e19"))
        self.assertEqual(len(result.fills), 0, "缺失参考价不得成交")
        rejected = [o for o in result.orders if o.status == "REJECTED"]
        self.assertTrue(any(o.reason == "LIMIT_REFERENCE_UNKNOWN" for o in rejected),
                        [o.reason for o in result.orders])

    def test_sell_rejected_without_pre_close(self):
        """持仓后执行日缺失 pre_close：卖出被保守拒绝，持仓保留"""
        bars = {
            20240102: {SYM: bar(20240102, 10.0, 10.0, pre_close=10.0)},
            20240103: {SYM: bar(20240103, 10.0, 10.0, pre_close=10.0)},
            20240104: {SYM: bar(20240104, 10.0, 10.0, pre_close=None)},
        }

        def decide(ctx: DecisionContext):
            if ctx.date == 20240102:
                return TargetPortfolio(decision_date=ctx.date, weights={SYM: 0.5})
            if ctx.date == 20240103:
                return TargetPortfolio(decision_date=ctx.date, weights={SYM: 0.0})
            return None

        result = run(bars, decide, EngineConfig(run_id="e19b"))
        buys = [f for f in result.fills if f.side == "BUY"]
        self.assertEqual(len(buys), 1)
        sells = [f for f in result.fills if f.side == "SELL"]
        self.assertEqual(len(sells), 0, "缺失参考价的卖出不得成交")
        # 持仓保留：期末快照仍有持仓
        self.assertTrue(any(p.shares > 0 for p in result.positions),
                        [(p.date, p.symbol, p.shares) for p in result.positions])


class TestSuspensionRecovery(unittest.TestCase):
    """E20: 停牌日不成交，恢复日正常成交"""

    def test_order_during_suspension_not_filled(self):
        """D2 决策 → D3 执行日停牌：订单 SUSPENDED 拒绝且不顺延（引擎语义：
        普通买入被拒即丢弃，无重试；只有 ExitEvent 退出有重试）。"""
        bars = {
            20240102: {SYM: bar(20240102, 10.0, 10.0, pre_close=10.0)},
            # D3 停牌：suspended=True，volume=0
            20240103: {SYM: bar(20240103, 10.0, 10.0, volume=0.0, suspended=True,
                                pre_close=10.0)},
            # D4 恢复
            20240104: {SYM: bar(20240104, 10.1, 10.2, pre_close=10.0)},
        }

        def decide(ctx: DecisionContext):
            if ctx.date == 20240102:
                return TargetPortfolio(decision_date=ctx.date, weights={SYM: 0.5})
            return None

        result = run(bars, decide, EngineConfig(run_id="e20"))
        buys = [f for f in result.fills if f.side == "BUY"]
        self.assertEqual(len(buys), 0, "停牌执行日不得成交")
        rejected = [o for o in result.orders if o.status == "REJECTED"]
        self.assertTrue(any(o.reason == "SUSPENDED" for o in rejected),
                        [(o.status, o.reason) for o in result.orders])

    def test_suspended_day_rejects_exit(self):
        """持仓期间停牌：卖出被拒（SUSPENDED），恢复日可退出"""
        bars = {
            20240102: {SYM: bar(20240102, 10.0, 10.0, pre_close=10.0)},
            20240103: {SYM: bar(20240103, 10.0, 10.0, pre_close=10.0)},
            20240104: {SYM: bar(20240104, 10.0, 10.0, volume=0.0, suspended=True,
                                pre_close=10.0)},
            20240105: {SYM: bar(20240105, 10.0, 10.0, pre_close=10.0)},
        }

        def decide(ctx: DecisionContext):
            if ctx.date == 20240102:
                return TargetPortfolio(decision_date=ctx.date, weights={SYM: 0.5})
            if ctx.date == 20240103:
                return TargetPortfolio(decision_date=ctx.date, weights={SYM: 0.0})
            return None

        result = run(bars, decide, EngineConfig(run_id="e20b"))
        rejected = [o for o in result.orders if o.status == "REJECTED"]
        self.assertTrue(any(o.reason == "SUSPENDED" for o in rejected),
                        [o.reason for o in result.orders])
        sells = [f for f in result.fills if f.side == "SELL"]
        self.assertEqual(len(sells), 0)


class TestNoMarketData(unittest.TestCase):
    """E21: 执行日无行情 → NO_MARKET_DATA，现金持仓不变"""

    def test_symbol_missing_on_trading_day_rejected(self):
        """执行日在交易日历中（其他标的当日有行情）但该标的无行情：
        订单 REJECTED(NO_MARKET_DATA)，不成交、不顺延（任务 6.2 无行情不成交）。"""
        other = "sh600036"
        bars = {
            20240102: {SYM: bar(20240102, 10.0, 10.0, pre_close=10.0),
                       other: bar(20240102, 30.0, 30.0, pre_close=30.0)},
            # 20240103：只有 other 有行情，SYM 缺行情
            20240103: {other: bar(20240103, 30.0, 30.0, pre_close=30.0)},
            20240104: {SYM: bar(20240104, 10.0, 10.0, pre_close=10.0),
                       other: bar(20240104, 30.0, 30.0, pre_close=30.0)},
        }

        def decide(ctx: DecisionContext):
            if ctx.date == 20240102:
                return TargetPortfolio(decision_date=ctx.date, weights={SYM: 0.5})
            return None

        result = run(bars, decide, EngineConfig(run_id="e21a"))
        self.assertEqual(len(result.fills), 0, "执行日标的无行情不得成交")
        rejected = [o for o in result.orders if o.status == "REJECTED"]
        self.assertTrue(any(o.reason == "NO_MARKET_DATA" for o in rejected),
                        [(o.status, o.reason) for o in result.orders])
        self.assertTrue(any(d.reason == "NO_MARKET_DATA" for d in result.deviations))

    def test_whole_session_missing_defers_to_next_session(self):
        """执行日整体不在 bars_by_date（市场休市/数据整日缺失）：
        pending 决策顺延到下一个有行情交易日开盘执行（挂单语义）。
        与上一测试的区别：无行情是标的级（拒绝）还是会话级（顺延）。"""
        bars = {
            20240102: {SYM: bar(20240102, 10.0, 10.0, pre_close=10.0)},
            20240103: {SYM: bar(20240103, 10.0, 10.0, pre_close=10.0)},
            # 20240104 整日缺失
            20240105: {SYM: bar(20240105, 10.0, 10.0, pre_close=10.0)},
        }

        def decide(ctx: DecisionContext):
            if ctx.date == 20240103:
                return TargetPortfolio(decision_date=ctx.date, weights={SYM: 0.5})
            return None

        result = run(bars, decide, EngineConfig(run_id="e21b"))
        buys = [f for f in result.fills if f.side == "BUY"]
        self.assertEqual(len(buys), 1)
        self.assertEqual(buys[0].date, 20240105, "顺延到下一个有行情交易日成交")




class TestResumptionDayLimitReference(unittest.TestCase):
    """§9.2（Data Correctness Closure）：停牌跨日顺延 → 复牌日保守拒绝。

    真实数据形状（DEV 实证）：Raw 行情 VIEW 停牌日无行（volume=0 行数=0），
    复牌日 Bar 的 pre_close 由 Strategy 面板 shift(1) 取停牌日 NaN → None。
    停牌日整日缺失触发挂单顺延语义（E21b），订单在复牌日执行——此时
    pre_close 缺失，开盘可能处于涨跌停锁定，必须保守拒绝，不得以不复核
    限价的价格成交（宁可不交易，不可假交易）。
    """

    def test_deferred_order_rejected_at_resumption_without_pre_close(self):
        bars = {
            20240102: {SYM: bar(20240102, 10.0, 10.0, pre_close=10.0)},
            # 20240103 / 20240104 停牌：整日缺失（真实路径无停牌行情行）
            # 复牌日 20240105：pre_close=None（shift(1) 取停牌日 NaN）
            20240105: {SYM: bar(20240105, 10.0, 10.0, pre_close=None)},
        }

        def decide(ctx: DecisionContext):
            if ctx.date == 20240102:
                return TargetPortfolio(decision_date=ctx.date, weights={SYM: 0.5})
            return None

        result = run(bars, decide, EngineConfig(run_id="s92-resumption"))
        self.assertEqual(len(result.fills), 0, "复牌日参考价缺失不得成交")
        rejected = [o for o in result.orders if o.status == "REJECTED"]
        resumption_rejects = [o for o in rejected
                              if o.reason == "LIMIT_REFERENCE_UNKNOWN"]
        self.assertTrue(
            resumption_rejects,
            [(o.status, o.reason, o.date) for o in result.orders])
        self.assertEqual(
            resumption_rejects[0].date, 20240105,
            "拒绝必须发生在复牌日（跨停牌顺延后）")
        self.assertEqual(resumption_rejects[0].decision_date, 20240102)


if __name__ == "__main__":
    unittest.main()
