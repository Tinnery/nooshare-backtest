"""A 股 Level-1 执行手算用例（E1-E18，任务书 Phase 5）。

每个用例手算预期：订单数、成交数、成交价、佣金、印花税、现金、持仓、
Trade 状态、PnL——不是布尔断言。真实样本数值取自 Phase 0 探查
（nooshare-design/implementation-docs/nooshare-backtest/phase0-dev-data-audit.md）：
  - sh600010 20251013  pc=2.54 -> 涨停 2.79（开盘涨停、盘中开板）
  - sh600057 20250114  pc=5.95 -> ST 涨停 6.25 = round(5.95x1.05, 2)
  - bj920002 20250226  pc=145  -> BSE 涨停 188.50 = round(145x1.30, 2)
  - 600930   20250716  IPO 首日 o=5.50 -> c=7.18（+30.5%，主板首日无限制）
  - sh600519 20250626  除息日 factor_cum 1.7939 -> 1.8292

成本模型（默认）：佣金 2.5bp 双边、印花税 5bp 卖出、滑点 5bp 双边、过户费 0。
手算基准：
  买 1000 股 @10.00：slip_px=10.005，佣金=10005x0.00025=2.50125，滑点=5.0，
    net_cash=-10007.50125，现金=1_000_000-10007.50125=989992.49875
  卖 1000 股 @10.00：slip_px=9.995，佣金=2.49875，印花=4.9975，滑点=5.0，
    net_cash=9987.50375
  完整 Trade（买10.005/卖9.995）：net_pnl=-19.9975，真实费用=9.9975，
    gross_pnl=价差口径=-10.0（滑点含在价内不重复计入）
"""
import unittest

from nooshare_backtest.accounting import Portfolio
from nooshare_backtest.costs import CostModel
from nooshare_backtest.engine import EngineConfig, run
from nooshare_backtest.execution import ExecutionConfig, execute_orders
from nooshare_backtest.models import Bar, Fill, Order, TargetPortfolio
from nooshare_backtest.rules import (
    R_NO_DATA, R_OPEN_LIMIT_DOWN, R_OPEN_LIMIT_UP, R_T_PLUS_1, R_ZERO_VOLUME,
    LimitRuleResolver, RuleSet, limit_prices,
)

CASH0 = 1_000_000.0
SYM = "sh600000"

E16_SKIP = ("BLOCKED: CA dividend/split cash accounting unavailable in DEV "
            "(phase0 R5); fixture deferred until nooshare-base fixes projection")


def bar(date, o, c, *, h=None, l=None, vol=1_000_000.0, pre_close=None,
        pc_ratio=0.10, **kw):
    """构造 Bar：pre_close 提供时按 pc_ratio 手算涨跌停价。"""
    h = h if h is not None else max(o, c)
    l = l if l is not None else min(o, c)
    lu = ld = None
    if pre_close is not None:
        rule = type("R", (), {"ratio": pc_ratio, "no_limit": False})()
        lu, ld = limit_prices(pre_close, rule)
    kw.setdefault("pre_close", pre_close)
    kw.setdefault("limit_up_price", lu)
    kw.setdefault("limit_down_price", ld)
    return Bar(date=date, symbol=SYM, open=o, high=h, low=l, close=c,
               volume=vol, **kw)


def cfg(run_id="fx"):
    return EngineConfig(
        initial_cash=CASH0, run_id=run_id,
        rules=RuleSet(enable_t_plus_1=True, enable_st_filter=False),
    )


def targets(decisions: dict):
    """{决策日: 目标权重}。"""
    return {
        d: TargetPortfolio(decision_date=d, weights=({SYM: w} if w != 0.0 else {}))
        for d, w in decisions.items()
    }


class FixtureE1_E8_OpenState(unittest.TestCase):
    """E1-E8：开盘状态判定（NEXT_OPEN 语义核心）。"""

    def _two_day_bars(self, d2_bar):
        """D1 收盘决策（权重 0.01 -> 1000 股 @10），D2 执行。"""
        return {
            20240102: {SYM: bar(20240102, 10.0, 10.0)},
            20240103: {SYM: d2_bar},
        }

    # E1 正常开盘买入：手算现金与成交价
    def test_e1_normal_buy_filled(self):
        bars = self._two_day_bars(bar(20240103, 10.0, 10.1, pre_close=10.0))
        r = run(bars, targets({20240102: 0.01}), cfg("e1"))
        self.assertEqual(len(r.fills), 1)
        f = r.fills[0]
        self.assertEqual(f.shares, 1000)
        self.assertAlmostEqual(f.price, 10.005, places=6)          # 10x1.0005
        self.assertAlmostEqual(f.commission, 2.50125, places=6)    # 10005x2.5bp
        self.assertAlmostEqual(f.stamp_duty, 0.0, places=6)
        self.assertAlmostEqual(f.slippage, 5.0, places=6)
        self.assertAlmostEqual(f.net_cash, -10007.50125, places=6)
        self.assertAlmostEqual(r.equity_curve[-1].cash, 989992.49875, places=6)
        self.assertEqual(r.orders[0].status, "FILLED")

    # E2 开盘价等于涨停价：保守不成交
    def test_e2_open_limit_up_rejected(self):
        # 真实样本口径：pc=2.54 -> lu=2.79，开盘 2.79
        bars = {
            20240102: {SYM: bar(20240102, 2.54, 2.54)},
            20240103: {SYM: bar(20240103, 2.79, 2.79, pre_close=2.54)},
        }
        r = run(bars, targets({20240102: 0.01}), cfg("e2"))
        self.assertEqual(r.fills, [])
        self.assertEqual(r.orders[0].status, "REJECTED")
        self.assertEqual(r.orders[0].reason, R_OPEN_LIMIT_UP)
        self.assertAlmostEqual(r.equity_curve[-1].cash, CASH0, places=6)

    # E3 开盘低于涨停、收盘触及涨停：开盘买入允许成交
    def test_e3_close_limit_touch_allows_open_buy(self):
        # Phase 0 样本 sh600006 20250520: o=7.17 < lu=7.83，收盘 7.83 涨停
        bars = {
            20240102: {SYM: bar(20240102, 7.12, 7.12)},
            20240103: {SYM: bar(20240103, 7.17, 7.83, h=7.83, l=7.15,
                                pre_close=7.12, is_limit_up=True)},
        }
        r = run(bars, targets({20240102: 0.014}), cfg("e3"))
        self.assertEqual(len(r.fills), 1)
        self.assertAlmostEqual(r.fills[0].price, 7.17 * 1.0005, places=6)
        self.assertEqual(r.orders[0].status, "FILLED")

    # E4 全天一字涨停：不产生买入 Fill
    def test_e4_one_char_limit_up_rejected(self):
        bars = self._two_day_bars(
            bar(20240103, 11.0, 11.0, h=11.0, l=11.0, pre_close=10.0))
        r = run(bars, targets({20240102: 0.01}), cfg("e4"))
        self.assertEqual(r.fills, [])
        self.assertEqual(r.orders[0].reason, R_OPEN_LIMIT_UP)

    # E5 正常卖出：手算现金、Trade PnL（net/gross/costs）
    def test_e5_normal_sell_full_ledger(self):
        bars = {
            20240102: {SYM: bar(20240102, 10.0, 10.0)},
            20240103: {SYM: bar(20240103, 10.0, 10.0, pre_close=10.0)},
            20240104: {SYM: bar(20240104, 10.0, 10.0, pre_close=10.0)},
        }
        r = run(bars, targets({20240102: 0.01, 20240103: 0.0}), cfg("e5"))
        self.assertEqual(len(r.fills), 2)
        buy, sell = r.fills
        self.assertEqual((buy.side, sell.side), ("BUY", "SELL"))
        self.assertAlmostEqual(sell.price, 9.995, places=6)          # 10x0.9995
        self.assertAlmostEqual(sell.commission, 2.49875, places=6)
        self.assertAlmostEqual(sell.stamp_duty, 4.9975, places=6)
        self.assertAlmostEqual(sell.net_cash, 9987.50375, places=6)
        # 终态现金 = 1_000_000 - 10007.50125 + 9987.50375
        self.assertAlmostEqual(r.equity_curve[-1].cash, 999980.0025, places=6)
        self.assertEqual(len(r.trades), 1)
        t = r.trades[0]
        self.assertEqual(t.status, "CLOSED")
        self.assertAlmostEqual(t.net_pnl, -19.9975, places=6)        # 含全部费用
        self.assertAlmostEqual(t.gross_pnl, -10.0, places=6)         # 价差口径
        self.assertAlmostEqual(t.total_cost, 19.9975, places=6)      # 含滑点展示
        self.assertAlmostEqual(t.avg_entry_price, 10.005, places=6)  # 不含费用成交价
        self.assertAlmostEqual(t.avg_exit_price, 9.995, places=6)

    # E6 开盘价等于跌停价：卖出保守不成交，持仓保留
    def test_e6_open_limit_down_rejected(self):
        bars = {
            20240102: {SYM: bar(20240102, 10.0, 10.0)},
            20240103: {SYM: bar(20240103, 10.0, 10.0, pre_close=10.0)},
            20240104: {SYM: bar(20240104, 9.0, 9.0, pre_close=10.0)},  # 开盘跌停
        }
        r = run(bars, targets({20240102: 0.01, 20240103: 0.0}), cfg("e6"))
        sells = [o for o in r.orders if o.side == "SELL"]
        self.assertEqual(sells[0].status, "REJECTED")
        self.assertEqual(sells[0].reason, R_OPEN_LIMIT_DOWN)
        self.assertEqual([f for f in r.fills if f.side == "SELL"], [])
        # 持仓未被动清除：仓位快照保留 1000 股
        pos = [p for p in r.positions if p.date == 20240104]
        self.assertEqual(pos[-1].shares, 1000)

    # E7 开盘高于跌停、收盘触及跌停：开盘卖出允许成交
    def test_e7_close_limit_touch_allows_open_sell(self):
        # Phase 0 样本 sh600006 20250407: o=7.04 > ld=6.77，收盘 6.77 跌停
        bars = {
            20240102: {SYM: bar(20240102, 7.52, 7.52)},
            20240103: {SYM: bar(20240103, 7.52, 7.52, pre_close=7.52)},
            20240104: {SYM: bar(20240104, 7.04, 6.77, h=7.05, l=6.77,
                                pre_close=7.52, is_limit_down=True)},
        }
        r = run(bars, targets({20240102: 0.013, 20240103: 0.0}), cfg("e7"))
        sells = [o for o in r.orders if o.side == "SELL"]
        self.assertEqual(sells[0].status, "FILLED")
        self.assertAlmostEqual(r.fills[-1].price, 7.04 * 0.9995, places=6)

    # E8 全天一字跌停：不产生卖出 Fill
    def test_e8_one_char_limit_down_rejected(self):
        bars = {
            20240102: {SYM: bar(20240102, 10.0, 10.0)},
            20240103: {SYM: bar(20240103, 10.0, 10.0, pre_close=10.0)},
            20240104: {SYM: bar(20240104, 9.0, 9.0, h=9.0, l=9.0,
                                pre_close=10.0)},
        }
        r = run(bars, targets({20240102: 0.01, 20240103: 0.0}), cfg("e8"))
        self.assertEqual([f for f in r.fills if f.side == "SELL"], [])
        self.assertEqual(
            [o for o in r.orders if o.side == "SELL"][0].reason, R_OPEN_LIMIT_DOWN)


class FixtureE9_E18(unittest.TestCase):
    """E9-E18：T+1、停牌、缺失、IPO、ST、BSE、公司行为、未成交语义。"""

    # E9 T+1：执行日当天的买入批次不可卖（直接预置批次手算）
    def test_e9_t_plus_1_blocks_same_day_lot(self):
        p = Portfolio(cash=CASH0)
        # 手工预置一个「今天买入」的批次（模拟同日先买后卖意图下的锁仓）
        p.apply_fill(Fill(date=20240103, symbol=SYM, side="BUY", shares=1000,
                          price=10.005, commission=2.50125, stamp_duty=0.0,
                          transfer_fee=0.0, slippage=5.0))
        self.assertEqual(p.sellable_shares(SYM, 20240103), 0)     # 当日批次不可卖
        self.assertEqual(p.sellable_shares(SYM, 20240104), 1000)  # 次日可卖
        rules = RuleSet(enable_t_plus_1=True, enable_st_filter=False)
        bars = {SYM: bar(20240103, 10.0, 10.0, pre_close=10.0)}
        _, final, _ = execute_orders(
            [Order(20240103, SYM, "SELL", 1000, 0.0)],
            p, bars, 20240103, rules, CostModel(), ExecutionConfig())
        self.assertEqual(final[0].status, "REJECTED")
        self.assertEqual(final[0].reason, R_T_PLUS_1)

    # E10 前一日买入、次日卖出：允许（E5 全链路已覆盖，这里显式断言）
    def test_e10_next_day_sell_allowed(self):
        p = Portfolio(cash=CASH0)
        p.apply_fill(Fill(date=20240102, symbol=SYM, side="BUY", shares=1000,
                          price=10.005, commission=2.50125, stamp_duty=0.0,
                          transfer_fee=0.0, slippage=5.0))
        rules = RuleSet(enable_t_plus_1=True, enable_st_filter=False)
        bars = {SYM: bar(20240103, 10.0, 10.0, pre_close=10.0)}
        fills, final, _ = execute_orders(
            [Order(20240103, SYM, "SELL", 1000, 0.0)],
            p, bars, 20240103, rules, CostModel(), ExecutionConfig())
        self.assertEqual(len(fills), 1)
        self.assertEqual(final[0].status, "FILLED")

    # E11 停牌（零成交）：不成交
    def test_e11_zero_volume_rejected(self):
        bars = {
            20240102: {SYM: bar(20240102, 10.0, 10.0)},
            20240103: {SYM: bar(20240103, 10.0, 10.0, vol=0.0, pre_close=10.0)},
        }
        r = run(bars, targets({20240102: 0.01}), cfg("e11"))
        self.assertEqual(r.fills, [])
        self.assertEqual(r.orders[0].reason, R_ZERO_VOLUME)
        self.assertAlmostEqual(r.equity_curve[-1].cash, CASH0, places=6)

    # E12 行情记录缺失：不静默当正常交易
    def test_e12_missing_market_data(self):
        bars = {
            20240102: {SYM: bar(20240102, 10.0, 10.0)},
            20240103: {},  # 执行日无行情
        }
        r = run(bars, targets({20240102: 0.01}), cfg("e12"))
        self.assertEqual(r.fills, [])
        self.assertEqual(len(r.orders), 1)   # 订单显式留痕，不静默丢弃
        self.assertEqual(r.orders[0].status, "REJECTED")
        self.assertEqual(r.orders[0].reason, R_NO_DATA)
        self.assertAlmostEqual(r.equity_curve[-1].cash, CASH0, places=6)

    # E13 上市初期无涨跌幅限制：正确识别并按开盘价成交
    def test_e13_ipo_no_limit(self):
        # Phase 0 样本 600930 首日 o=5.50（+50% 也允许成交）
        bars = {
            20240102: {SYM: bar(20240102, 10.0, 10.0)},
            20240103: {SYM: bar(20240103, 15.0, 16.0, pre_close=10.0,
                                no_limit=True, list_date=20240103,
                                trading_days_since_list=1)},
        }
        r = run(bars, targets({20240102: 0.015}), cfg("e13"))
        self.assertEqual(len(r.fills), 1)
        self.assertEqual(r.orders[0].status, "FILLED")
        self.assertAlmostEqual(r.fills[0].price, 15.0 * 1.0005, places=6)

    # E14 ST 规则日期切换：按历史规则手算（真实样本 sh600057）
    def test_e14_st_rule_dates(self):
        res = LimitRuleResolver(pit_rules={("sh", "main_board"): (0.10, 20080228)})
        lu, ld = limit_prices(5.95, res.resolve("sh", "600057", True, 20250114, None))
        self.assertEqual((lu, ld), (6.25, 5.65))       # ST 5%
        lu2, ld2 = limit_prices(5.95, res.resolve("sh", "600057", True, 20260706, None))
        self.assertEqual((lu2, ld2), (6.55, 5.36))     # 5.355 -> 5.36 四舍五入

    # E15 BSE 30% 涨跌幅：不被已删除的 fake_gap 规则错误拒绝
    def test_e15_bse_30pct(self):
        # Phase 0 样本 bj920002 20250226: pc=145 -> 涨停 188.50
        res = LimitRuleResolver()
        lu, ld = limit_prices(145.0, res.resolve("bj", "920002", False, 20250226, None))
        self.assertEqual((lu, ld), (188.50, 101.50))
        sym = "bj920002"
        bars = {
            20250225: {sym: Bar(date=20250225, symbol=sym, open=145.0, high=145.0,
                                low=145.0, close=145.0, volume=1_000_000.0)},
            20250226: {sym: Bar(date=20250226, symbol=sym, open=185.6, high=188.5,
                                low=185.6, close=188.5, volume=2_000_000.0,
                                ret=0.30, pre_close=145.0,
                                limit_up_price=188.5, limit_down_price=101.5)},
        }
        r = run(bars, {20250225: TargetPortfolio(decision_date=20250225,
                                                 weights={sym: 0.02})},
                cfg("e15"))
        self.assertEqual(len(r.fills), 1)
        self.assertEqual(r.orders[0].status, "FILLED")   # ret=0.30 > 旧 fake_gap 0.21 仍成交

    # E16 公司行为零碎股份：BLOCKED——2024-2025 无 CA 现金账务可验证
    @unittest.skip(E16_SKIP)
    def test_e16_fractional_shares_after_corporate_action(self):
        pass

    # E17 除权除息：现金分红账务 BLOCKED；账户盯市 = Raw close（X5-B 契约）
    # （茅台 20250626：raw 1435.86->1420 跳变 = 分红未入账的真实经济体现。
    #  账户 NAV 因此跳变是 corporate_action_support=BLOCKED 的披露行为；
    #  禁止用 HFQ 价伪造除权日连续性——HFQ 连续性仅属研究口径
    #  （indicators/strategy），不进入账户经济账务。）
    def test_e17_ex_dividend_mark_price_uses_raw_close(self):
        bars = {
            20240102: {SYM: bar(20240102, 10.0, 10.0)},
            20240103: {SYM: bar(20240103, 10.0, 10.0, pre_close=10.0)},
            # 除息日：raw close 跳变到 7.0（分红未入账，BLOCKED）；不传 mark_price，
            # 引擎回退 Raw close 盯市（X5-B：上游 run_backtest_v2 亦填 Raw close）
            20240104: {SYM: bar(20240104, 7.0, 7.0, pre_close=10.0)},
        }
        r = run(bars, targets({20240102: 0.01}), cfg("e17"))
        snaps = [p for p in r.positions if p.date == 20240104]
        self.assertEqual(snaps[-1].shares, 1000)
        self.assertAlmostEqual(snaps[-1].last_price, 7.0, places=6)   # Raw close 盯市
        self.assertAlmostEqual(snaps[-1].market_value, 7000.0, places=6)

    # E18 订单未成交：不产生 Fill、Trade 或已实现 PnL
    def test_e18_unfilled_no_fake_ledger(self):
        bars = {
            20240102: {SYM: bar(20240102, 10.0, 10.0)},
            20240103: {SYM: bar(20240103, 11.0, 11.0, pre_close=10.0)},  # 开盘涨停
        }
        r = run(bars, targets({20240102: 0.01}), cfg("e18"))
        self.assertEqual(r.fills, [])
        self.assertEqual(r.trades, [])
        self.assertEqual(r.trade_fill_allocations, [])
        self.assertAlmostEqual(r.costs.total, 0.0, places=6)
        self.assertAlmostEqual(r.equity_curve[-1].cash, CASH0, places=6)
        self.assertAlmostEqual(r.equity_curve[-1].nav, 1.0, places=6)
        self.assertEqual(r.orders[0].reason, R_OPEN_LIMIT_UP)


if __name__ == "__main__":
    unittest.main()
