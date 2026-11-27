"""跨仓端到端集成测试（P6/B26）。

Signal Fixture → Strategy TargetPortfolio → Canonical Engine → BacktestResult
→ RunPackage → nooshare.validate_run_package → 完整引用链验证。

在 backtest 的 venv 中运行（含 backtest 源码）；把 nooshare 的 runpackage.py
作为纯 Python 模块加载（它只依赖标准库）。
"""
import importlib.util
import sys
import unittest
from pathlib import Path

from nooshare_backtest.engine import EngineConfig, run
from nooshare_backtest.models import Bar, TargetPortfolio

from tests.fixtures.lifecycle import D1, D2, D3, D4, D5

WS = Path(__file__).resolve().parents[3]  # .../track-b-20261125
NS = WS / "nooshare" / "backend" / "app" / "runpackage.py"


def load_runpackage_module():
    spec = importlib.util.spec_from_file_location("ns_runpackage", NS)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ns_runpackage"] = mod
    spec.loader.exec_module(mod)
    return mod


def make_result():
    s = "sh600519"
    bars = {
        D1: {s: Bar(D1, s, 10, 10, 10, 10, volume=1e6, pre_close=10, limit_up_ratio=0.10, limit_down_ratio=0.10, limit_rule_source="fixture_pit", canonical_id="600519", exchange="sh")},
        D2: {s: Bar(D2, s, 10, 10.2, 9.9, 10.1, volume=1e6, pre_close=10, limit_up_ratio=0.10, limit_down_ratio=0.10, limit_rule_source="fixture_pit", canonical_id="600519", exchange="sh")},
        D3: {s: Bar(D3, s, 10.2, 10.4, 10.0, 10.3, volume=1e6, pre_close=10.1, limit_up_ratio=0.10, limit_down_ratio=0.10, limit_rule_source="fixture_pit", canonical_id="600519", exchange="sh")},
        D4: {s: Bar(D4, s, 10.4, 10.4, 10.2, 10.3, volume=1e6, pre_close=10.3, limit_up_ratio=0.10, limit_down_ratio=0.10, limit_rule_source="fixture_pit", canonical_id="600519", exchange="sh")},
        D5: {s: Bar(D5, s, 10.3, 10.3, 10.1, 10.2, volume=1e6, pre_close=10.3, limit_up_ratio=0.10, limit_down_ratio=0.10, limit_rule_source="fixture_pit", canonical_id="600519", exchange="sh")},
    }
    targets = {
        D1: TargetPortfolio(D1, {s: 0.5}, signal_ids={s: "sig-e2e-1"}),
        D2: TargetPortfolio(D2, {s: 0.0}, signal_ids={s: "sig-e2e-2"}),
        D3: TargetPortfolio(D3, {s: 0.4}, signal_ids={s: "sig-e2e-3"}),
    }
    return run(bars, targets, EngineConfig(run_id="e2e-run", initial_cash=500_000.0))


def result_to_runpackage(result, signals):
    bt = result.to_dict()
    return {
        "schema_version": 1,
        "fixture": True,
        "run": {
            "run_id": result.metadata.run_id,
            "experiment_id": "exp-e2e",
            "variant_id": "var-e2e",
            "strategy_commit": "fixture",
        },
        "signals": signals,
        "backtest_result": bt,
    }


class TestCrossRepoIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rp = load_runpackage_module()

    def test_full_chain_references_intact(self):
        result = make_result()
        signals = [
            {"signal_id": "sig-e2e-1", "canonical_id": "600519", "exchange": "sh",
             "signal_date": "2024-01-02", "feature_values": {}, "reason_codes": ["zscore_up"]},
            {"signal_id": "sig-e2e-2", "canonical_id": "600519", "exchange": "sh",
             "signal_date": "2024-01-03", "feature_values": {}, "reason_codes": ["exit"]},
            {"signal_id": "sig-e2e-3", "canonical_id": "600519", "exchange": "sh",
             "signal_date": "2024-01-04", "feature_values": {}, "reason_codes": ["zscore_up"]},
        ]
        pkg = result_to_runpackage(result, signals)
        report = self.rp.validate_run_package(pkg)
        self.assertTrue(report.ok, report.errors)

    def test_trade_to_order_to_signal_chain(self):
        result = make_result()
        # 每笔 Trade 的 initial_signal_id 可回溯（本 fixture 中 order 带 signal_id）
        self.assertTrue(result.trades)
        buy_orders = [o for o in result.orders if o.side == "BUY" and o.signal_id]
        self.assertTrue(buy_orders)
        for f in result.fills:
            order = next(o for o in result.orders if o.order_id == f.order_id)
            self.assertEqual(order.order_id, f.order_id)
        for t in result.trades:
            allocs = [a for a in result.trade_fill_allocations if a.trade_id == t.trade_id]
            self.assertTrue(allocs)
            self.assertEqual(sum(a.allocated_qty for a in allocs if a.allocation_role == "ENTRY"), t.entry_qty)
            self.assertEqual(sum(a.allocated_qty for a in allocs if a.allocation_role == "EXIT"), t.exit_qty)

    def test_ui_projection_keeps_engine_numbers(self):
        result = make_result()
        view = self.rp.to_frontend(result_to_runpackage(result, []))
        self.assertEqual(view["run"]["run_id"], "e2e-run")
        self.assertEqual(view["metrics"]["trade_count"], result.metrics["trade_count"])
        self.assertEqual(len(view["trades"]), len(result.trades))
        self.assertEqual(len(view["equity_curve"]), len(result.equity_curve))

    def test_deterministic_serialization_across_full_chain(self):
        r1 = make_result()
        r2 = make_result()
        p1 = result_to_runpackage(r1, [])
        p2 = result_to_runpackage(r2, [])
        self.assertEqual(p1, p2)


if __name__ == "__main__":
    unittest.main()
