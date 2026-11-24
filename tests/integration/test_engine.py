import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from nooshare_backtest.api import run_backtest
from nooshare_backtest.reports import render_html

from tests.fixtures.sample import (
    D1, D2, D3, D4,
    EXPECTED_AAA_BUY_CASH, EXPECTED_AAA_BUY_SHARES,
    EXPECTED_BBB_BUY_CASH, EXPECTED_BBB_BUY_SHARES,
    sample_bars, sample_decisions,
)


class TestEngineIntegration(unittest.TestCase):
    def setUp(self):
        self.result = run_backtest(sample_bars(), sample_decisions(),
                                   initial_cash=1_000_000.0, run_id="fixture")

    def test_t_decision_executes_next_session(self):
        d2_orders = [o for o in self.result.orders if o.date == D2]
        self.assertEqual(len(d2_orders), 2)
        self.assertTrue(all(o.status == "FILLED" for o in d2_orders))
        self.assertEqual([o.symbol for o in d2_orders], ["AAA", "BBB"])
        self.assertEqual([o.shares for o in d2_orders],
                         [EXPECTED_AAA_BUY_SHARES, EXPECTED_BBB_BUY_SHARES])
        self.assertFalse(any(o.date == D1 for o in self.result.orders))

    def test_fill_cash_matches_hand_calculation(self):
        d2_fills = {f.symbol: f for f in self.result.fills if f.date == D2}
        self.assertAlmostEqual(d2_fills["AAA"].net_cash, EXPECTED_AAA_BUY_CASH, places=3)
        self.assertAlmostEqual(d2_fills["BBB"].net_cash, EXPECTED_BBB_BUY_CASH, places=3)

    def test_t_plus_1_sell_on_following_session(self):
        d3_sells = [o for o in self.result.orders if o.date == D3 and o.side == "SELL"]
        self.assertEqual(len(d3_sells), 1)
        self.assertEqual(d3_sells[0].status, "FILLED")
        self.assertNotEqual(d3_sells[0].reason, "T_PLUS_1")

    def test_last_decision_is_reported_as_unexecuted(self):
        tail = [d for d in self.result.deviations if d.reason == "NO_NEXT_SESSION"]
        self.assertEqual(len(tail), 1)
        self.assertEqual(tail[0].date, D4)

    def test_equity_positions_and_ledger_are_consistent(self):
        self.assertEqual([p.date for p in self.result.equity_curve], [D1, D2, D3, D4])
        self.assertEqual(self.result.equity_curve[0].nav, 1.0)
        d2 = self.result.equity_curve[1]
        expected_mv = 10.4 * EXPECTED_AAA_BUY_SHARES + 20.4 * EXPECTED_BBB_BUY_SHARES
        self.assertAlmostEqual(d2.market_value, expected_mv, places=4)
        self.assertAlmostEqual(d2.total_asset, d2.cash + d2.market_value, places=4)
        self.assertEqual(len([p for p in self.result.positions if p.date == D2]), 2)
        self.assertTrue(any(e.kind == "MARK" for e in self.result.ledger))

    def test_reproducible_and_serializable(self):
        other = run_backtest(sample_bars(), sample_decisions(),
                             initial_cash=1_000_000.0, run_id="fixture")
        self.assertEqual(self.result.to_dict(), other.to_dict())
        json.dumps(self.result.to_dict())  # 必须可 JSON 序列化

    def test_report_contains_fill_policy_and_chart(self):
        html = render_html(self.result)
        self.assertIn("T+1", html)
        self.assertIn("<svg", html)
        self.assertIn("目标与实际差异", html)

    def test_cli_run_show_report(self):
        payload = {
            "initial_cash": 1_000_000.0,
            "run_id": "cli-fixture",
            "bars": [
                {"date": d, "symbol": s, "open": b.open, "high": b.high,
                 "low": b.low, "close": b.close, "volume": b.volume, "ret": b.ret}
                for d, day in sample_bars().items() for s, b in day.items()
            ],
            "decisions": [
                {"decision_date": d, "weights": w} for d, w in sample_decisions().items()
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inp = root / "input.json"
            inp.write_text(json.dumps(payload), encoding="utf-8")
            out = root / "out"
            subprocess.run(
                [sys.executable, "-m", "nooshare_backtest.cli", "run",
                 "--input", str(inp), "--output-dir", str(out), "--report"],
                check=True, capture_output=True, text=True,
            )
            self.assertTrue((out / "result.json").exists())
            self.assertTrue((out / "report.html").exists())
            shown = subprocess.run(
                [sys.executable, "-m", "nooshare_backtest.cli", "show",
                 "--result", str(out / "result.json")],
                check=True, capture_output=True, text=True,
            )
            self.assertIn("cli-fixture", shown.stdout)
            rendered = subprocess.run(
                [sys.executable, "-m", "nooshare_backtest.cli", "report",
                 "--result", str(out / "result.json"), "--output", str(root / "r.html")],
                check=True, capture_output=True, text=True,
            )
            self.assertIn("report:", rendered.stdout)


if __name__ == "__main__":
    unittest.main()
