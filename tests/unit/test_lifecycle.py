"""P2/P3 事件身份与交易生命周期验收测试（B3-B16）。"""
import json
import unittest

from nooshare_backtest.api import run_backtest
from nooshare_backtest.engine import EngineConfig, run
from nooshare_backtest.execution import ExecutionConfig
from nooshare_backtest.models import (
    BacktestResult, Bar, SCHEMA_VERSION, TargetPortfolio, result_from_dict,
)

from tests.fixtures.lifecycle import (
    D1, D2, D3, D4, D5,
    limit_up_bars, multi_lot_bars, normal_bars, open_position_bars,
)


def decisions(*pairs):
    """pairs: (decision_date, weights, signal_ids?)"""
    out = {}
    for p in pairs:
        date, weights = p[0], p[1]
        sig = p[2] if len(p) > 2 else {}
        out[date] = TargetPortfolio(decision_date=date, weights=weights, signal_ids=sig)
    return out


class TestEventIdentity(unittest.TestCase):
    def test_stable_ids_deterministic_and_unique(self):
        r1 = run(normal_bars(), decisions((D1, {"sh600519": 0.5})),
                 EngineConfig(run_id="fixed"))
        r2 = run(normal_bars(), decisions((D1, {"sh600519": 0.5})),
                 EngineConfig(run_id="fixed"))
        self.assertEqual(r1.to_dict(), r2.to_dict())
        self.assertTrue(r1.orders and r1.fills)
        oids = [o.order_id for o in r1.orders]
        fids = [f.fill_id for f in r1.fills]
        self.assertEqual(len(oids), len(set(oids)))
        self.assertEqual(len(fids), len(set(fids)))
        # 不同 run_id 不冲突
        r3 = run(normal_bars(), decisions((D1, {"sh600519": 0.5})),
                 EngineConfig(run_id="other"))
        self.assertFalse(set(oids) & {o.order_id for o in r3.orders})

    def test_signal_id_propagates_to_order(self):
        bars = normal_bars()
        tgts = decisions((D1, {"sh600519": 0.5}, {"sh600519": "sig-001"}))
        r = run(bars, tgts, EngineConfig(run_id="sigtest"))
        buy = [o for o in r.orders if o.side == "BUY"][0]
        self.assertEqual(buy.signal_id, "sig-001")

    def test_no_signal_when_unmapped(self):
        r = run(normal_bars(), decisions((D1, {"sh600519": 0.5})),
                EngineConfig(run_id="nosig"))
        buy = [o for o in r.orders if o.side == "BUY"][0]
        self.assertEqual(buy.signal_id, "")


class TestTradeLifecycle(unittest.TestCase):
    def test_normal_round_trip_is_one_closed_trade(self):
        r = run(normal_bars(), decisions((D1, {"sh600519": 0.5}),
                                         (D2, {"sh600519": 0.0})),
                EngineConfig(run_id="normal"))
        self.assertEqual(len(r.trades), 1)
        t = r.trades[0]
        self.assertEqual(t.status, "CLOSED")
        self.assertEqual(t.entry_qty, t.exit_qty)
        self.assertEqual(t.open_qty, 0)
        self.assertGreater(t.entry_qty, 0)
        self.assertLessEqual(t.net_return, t.gross_return)  # 净收益不高毛收益于

    def test_open_position_not_faked_as_closed(self):
        r = run(open_position_bars(), decisions((D1, {"sh600519": 0.5})),
                EngineConfig(run_id="openpos"))
        self.assertEqual(len(r.trades), 1)
        t = r.trades[0]
        self.assertEqual(t.status, "OPEN")
        self.assertIsNone(t.closed_date)
        self.assertEqual(t.exit_qty, 0)
        self.assertGreater(t.open_qty, 0)
        self.assertFalse(any(f.side == "SELL" for f in r.fills))

    def test_last_day_decision_is_no_next_session(self):
        r = run(normal_bars(), decisions((D4, {"sh600519": 0.5})),
                EngineConfig(run_id="tail"))
        tail = [d for d in r.deviations if d.reason == "NO_NEXT_SESSION"]
        self.assertEqual(len(tail), 1)
        self.assertEqual(tail[0].date, D4)

    def test_limit_up_buy_rejected_no_fill(self):
        r = run(limit_up_bars(), decisions((D1, {"sh600519": 0.5})),
                EngineConfig(run_id="limitup"))
        orders = [o for o in r.orders if o.side == "BUY"]
        self.assertTrue(orders)
        self.assertTrue(all(o.status == "REJECTED" for o in orders))
        self.assertTrue(all(o.reason == "OPEN_LIMIT_UP" for o in orders))
        self.assertEqual(r.fills, [])
        self.assertEqual(r.trades, [])

    def test_insufficient_cash_partial_buy(self):
        """两只股票竞争有限现金：先买的成交后，第二只只能部分成交。"""
        bars = {
            D1: {
                "sh600519": Bar(D1, "sh600519", 10, 10, 10, 10, volume=1e6),
                "sz000001": Bar(D1, "sz000001", 10, 10, 10, 10, volume=1e6),
            },
            D2: {
                "sh600519": Bar(D2, "sh600519", 10, 10, 10, 10, volume=1e6),
                "sz000001": Bar(D2, "sz000001", 10, 10, 10, 10, volume=1e6),
            },
        }
        tgts = decisions((D1, {"sh600519": 0.9, "sz000001": 0.9}))
        r = run(bars, tgts, EngineConfig(run_id="partial", initial_cash=11_000.0))
        buys = [o for o in r.orders if o.side == "BUY"]
        partial = [o for o in buys if o.status == "PARTIALLY_FILLED"]
        self.assertTrue(partial, "资金不足应产生 PARTIALLY_FILLED")
        o = partial[0]
        self.assertGreater(o.filled_shares, 0)
        self.assertLess(o.filled_shares, o.requested_shares)
        self.assertTrue(any(d.reason == "INSUFFICIENT_CASH" for d in r.deviations))

    def test_t_plus_1_per_lot_not_whole_position(self):
        """D1 买 1000，D2 又买 500；D2 当天只能卖 D1 批次，不能卖当天批次。

        直接测会计层批次：这是旧实现单一 bought_date 的核心 bug——
        最近买入日覆盖旧批次会让全部 1500 股被错误锁住。
        """
        from nooshare_backtest.accounting import Portfolio
        from nooshare_backtest.models import Fill
        p = Portfolio(cash=1_000_000.0)
        p.apply_fill(Fill(D1, "sh600519", "BUY", 1000, 10.0, 0, 0, 0, 0))
        p.apply_fill(Fill(D2, "sh600519", "BUY", 500, 10.0, 0, 0, 0, 0))
        self.assertEqual(p.shares_of("sh600519"), 1500)
        # 最近买入日为 D2，但 D1 批次已过 T+1
        self.assertEqual(p.sellable_shares("sh600519", D2), 1000)
        self.assertEqual(p.sellable_shares("sh600519", D3), 1500)
        self.assertTrue(p.bought_today("sh600519", D2))

    def test_multi_entry_exit_same_trade(self):
        """多次买入、部分卖出、再卖出：完整生命周期只产生一个 Trade。"""
        bars = multi_lot_bars()
        tgts = decisions(
            (D1, {"sh600519": 0.10}),   # D2 买入
            (D2, {"sh600519": 0.20}),   # D3 加仓
            (D3, {"sh600519": 0.05}),   # D4 部分卖出
            (D4, {"sh600519": 0.0}),    # D5 全部卖出
        )
        r = run(bars, tgts, EngineConfig(run_id="multi"))
        self.assertEqual(len(r.trades), 1)
        t = r.trades[0]
        self.assertEqual(t.status, "CLOSED")
        self.assertGreater(t.entry_qty, 1000)
        self.assertEqual(t.entry_qty, t.exit_qty)
        entries = [a for a in r.trade_fill_allocations if a.allocation_role == "ENTRY"]
        exits = [a for a in r.trade_fill_allocations if a.allocation_role == "EXIT"]
        self.assertGreaterEqual(len(entries), 2)
        self.assertGreaterEqual(len(exits), 2)

    def test_reopen_after_close_creates_new_trade(self):
        bars = normal_bars()
        tgts = decisions(
            (D1, {"sh600519": 0.5}), (D2, {"sh600519": 0.0}),
            (D3, {"sh600519": 0.5}),
        )
        r = run(bars, tgts, EngineConfig(run_id="reopen"))
        self.assertEqual(len(r.trades), 2)
        self.assertNotEqual(r.trades[0].trade_id, r.trades[1].trade_id)


class TestAccountingIntegrity(unittest.TestCase):
    def test_full_accounting_invariants(self):
        r = run(multi_lot_bars(), decisions(
            (D1, {"sh600519": 0.10}), (D2, {"sh600519": 0.20}),
            (D3, {"sh600519": 0.05}), (D4, {"sh600519": 0.0}),
        ), EngineConfig(run_id="audit"))
        # 1) 现金 + 持仓市值 = 总资产
        for p in r.equity_curve:
            self.assertAlmostEqual(p.total_asset, p.cash + p.market_value, places=4)
        # 2) Order.filled_shares == 关联 Fill 数量汇总
        for o in r.orders:
            linked = sum(f.shares for f in r.fills if f.order_id == o.order_id)
            self.assertEqual(o.filled_shares, linked)
        # 3) 每条 Fill 唯一归属一个 Order
        oid_set = {o.order_id for o in r.orders}
        for f in r.fills:
            self.assertIn(f.order_id, oid_set)
        # 4) Allocation 数量不超 Fill 数量
        fill_qty = {f.fill_id: f.shares for f in r.fills}
        allocated = {}
        for a in r.trade_fill_allocations:
            allocated[a.fill_id] = allocated.get(a.fill_id, 0) + a.allocated_qty
            self.assertLessEqual(allocated[a.fill_id], fill_qty[a.fill_id])
        # 5) Trade entry_qty/exit_qty 与 Allocation 对应
        for t in r.trades:
            e = sum(a.allocated_qty for a in r.trade_fill_allocations
                    if a.trade_id == t.trade_id and a.allocation_role == "ENTRY")
            x = sum(a.allocated_qty for a in r.trade_fill_allocations
                    if a.trade_id == t.trade_id and a.allocation_role == "EXIT")
            self.assertEqual(t.entry_qty, e)
            self.assertEqual(t.exit_qty, x)
            self.assertEqual(t.entry_qty - t.exit_qty, t.open_qty)
            if t.status == "CLOSED":
                self.assertEqual(t.open_qty, 0)
        # 6) 费用汇总一致
        self.assertAlmostEqual(
            r.costs.commission, sum(f.commission for f in r.fills), places=4)
        # 7) Trade 费用汇总 == Fill 费用汇总
        self.assertAlmostEqual(
            sum(t.total_cost for t in r.trades),
            sum(f.total_cost for f in r.fills), places=3)


class TestSerialization(unittest.TestCase):
    def test_schema_version_and_round_trip(self):
        r = run(normal_bars(), decisions((D1, {"sh600519": 0.5})),
                EngineConfig(run_id="ser"))
        self.assertEqual(r.schema_version, SCHEMA_VERSION)
        d = r.to_dict()
        self.assertEqual(d["schema_version"], SCHEMA_VERSION)
        json.dumps(d)
        r2 = result_from_dict(d)
        self.assertEqual(r.to_dict(), r2.to_dict())

    def test_unsupported_version_raises(self):
        with self.assertRaises(ValueError):
            result_from_dict({"metadata": {"run_id": "x", "engine_version": "0", "config": {}},
                              "schema_version": 999})

    def test_legacy_without_identity_not_fabricated(self):
        legacy = {
            "metadata": {"run_id": "legacy", "engine_version": "0.1.0", "config": {}},
            "orders": [{"date": D2, "symbol": "sh600519", "side": "BUY",
                        "shares": 100, "target_weight": 0.5, "status": "FILLED"}],
        }
        r = result_from_dict(legacy)
        self.assertEqual(r.schema_version, 0)
        self.assertEqual(r.orders[0].order_id, "")
        self.assertEqual(r.trades, [])


if __name__ == "__main__":
    unittest.main()
