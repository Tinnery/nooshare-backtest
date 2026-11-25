"""CLI：运行回测、查看结果、生成静态报告。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .api import run_backtest
from .models import result_from_dict
from .reports import write_html_report


def _read_json(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write_json(data: dict, path: str | Path) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def cmd_run(args: argparse.Namespace) -> int:
    payload = _read_json(args.input)
    result = run_backtest(
        payload["bars"], payload["decisions"],
        initial_cash=float(payload.get("initial_cash", 1_000_000.0)),
        run_id=str(payload.get("run_id", "run")),
        strategy_version=str(payload.get("strategy_version", "unknown")),
        data_version=str(payload.get("data_version", "unknown")),
    )
    out_dir = Path(args.output_dir)
    result_path = out_dir / "result.json"
    _write_json(result.to_dict(), result_path)
    if args.report:
        report_path = write_html_report(result, out_dir / "report.html")
        print(f"report: {report_path}")
    print(f"result: {result_path}")
    print(json.dumps(result.metrics, ensure_ascii=False, indent=2))
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    result = result_from_dict(_read_json(args.result))
    print(f"run_id: {result.metadata.run_id}")
    print(f"fill_policy: {result.metadata.fill_policy}")
    print(f"orders={len(result.orders)} fills={len(result.fills)} "
          f"deviations={len(result.deviations)} equity_points={len(result.equity_curve)}")
    print(json.dumps(result.metrics, ensure_ascii=False, indent=2))
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    result = result_from_dict(_read_json(args.result))
    path = write_html_report(result, args.output)
    print(f"report: {path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="backtest", description="A-share daily backtest engine")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run a backtest from a JSON input")
    run.add_argument("--input", required=True, help="input JSON with bars and decisions")
    run.add_argument("--output-dir", default="out", help="directory for result.json / report.html")
    run.add_argument("--report", action="store_true", help="also write static HTML report")
    run.set_defaults(func=cmd_run)

    show = sub.add_parser("show", help="show metrics from result.json")
    show.add_argument("--result", required=True)
    show.set_defaults(func=cmd_show)

    report = sub.add_parser("report", help="render static HTML report from result.json")
    report.add_argument("--result", required=True)
    report.add_argument("--output", required=True)
    report.set_defaults(func=cmd_report)

    sim = sub.add_parser("simulate-zscore-trades", help="simulate Z-score signal trades")
    sim.add_argument("--run-id", required=True, help="experiment run ID")
    sim.add_argument("--stop-loss", type=float, default=0.05, help="stop loss ratio (default 0.05)")
    sim.add_argument("--take-profit", type=float, default=0.10, help="take profit ratio (default 0.10)")
    sim.add_argument("--max-hold-days", type=int, default=20, help="max holding days (default 20)")
    sim.set_defaults(func=cmd_simulate_zscore)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)




def cmd_simulate_zscore(args: argparse.Namespace) -> int:
    """模拟 Z-score 信号的交易"""
    import uuid
    from datetime import date
    from .zscore_trader import TradeConfig, simulate_trades, save_trades
    from nooshare_strategy.infrastructure import database as db
    from nooshare_strategy.signals import load_signals
    
    run_id = args.run_id
    stop_loss = args.stop_loss
    take_profit = args.take_profit
    max_hold_days = args.max_hold_days
    
    # 加载信号
    signals = load_signals(run_id)
    if signals.empty:
        print(f"No signals found for run {run_id}")
        return 1
    
    print(f"Loaded {len(signals)} signals")
    
    # 加载行情数据
    symbols = signals["symbol"].unique().tolist()
    dates = signals["signal_date"].unique().tolist()
    min_date = min(dates)
    max_date = max(dates) + timedelta(days=max_hold_days + 10)
    
    print(f"Loading bars for {len(symbols)} symbols...")
    bars_by_symbol_date = load_bars(symbols, min_date, max_date)
    
    # 模拟交易
    config = TradeConfig(
        stop_loss=stop_loss,
        take_profit=take_profit,
        max_hold_days=max_hold_days,
    )
    
    trades = simulate_trades(run_id, signals, bars_by_symbol_date, config)
    
    # 保存交易
    n_saved = save_trades(run_id, trades)
    print(f"Simulated and saved {n_saved} trades")
    
    # 更新信号状态
    update_signal_status(run_id, trades)
    
    return 0


def load_bars(symbols: list[str], start_date: date, end_date: date) -> dict:
    """加载行情数据。"""
    from nooshare_indicators.infrastructure import database as db
    from nooshare_indicators.infrastructure.data_loader import code_to_symbol
    
    # 将 symbol 转为 code
    codes = [s[2:] if len(s) > 2 else s for s in symbols]
    
    start_int = int(start_date.strftime("%Y%m%d"))
    end_int = int(end_date.strftime("%Y%m%d"))
    
    symbol_list = ",".join([f"'{s}'" for s in symbols])
    sql = f"""
    SELECT symbol, date, open, high, low, close, volume,
           is_suspended, is_limit_up
    FROM indicators.daily_base_aligned
    WHERE code IN ({",".join([f"'{c}'" for c in codes])})
      AND date >= {start_int}
      AND date <= {end_int}
    ORDER BY code, date
    """
    
    df = db.read_sql(sql)
    
    # 转为 {symbol: {date: bar}} 格式
    bars_by_symbol_date = {}
    for _, row in df.iterrows():
        symbol = row["symbol"] if "symbol" in row else f"sz{row['code']}"
        dt = pd.to_datetime(str(row["date"]), format="%Y%m%d").date()
        
        if symbol not in bars_by_symbol_date:
            bars_by_symbol_date[symbol] = {}
        
        bars_by_symbol_date[symbol][dt] = {
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "volume": float(row["volume"]) if row.get("volume") else None,
            "is_suspended": bool(row.get("is_suspended", False)),
            "is_limit_up": bool(row.get("is_limit_up", False)),
        }
    
    return bars_by_symbol_date


def update_signal_status(run_id: str, trades: list[dict]):
    """更新信号状态。"""
    from nooshare_strategy.infrastructure import database as db
    
    traded_signal_ids = {t["signal_id"] for t in trades}
    
    if traded_signal_ids:
        sql = """
        UPDATE strategy_lab.signal
        SET status = 'traded'
        WHERE signal_id = ANY(%s)
        """
        db.execute_sql(sql, (list(traded_signal_ids),))
    
    # 其余信号标记为 not_traded
    sql = """
    UPDATE strategy_lab.signal
    SET status = 'not_traded'
    WHERE run_id = %s AND status = 'pending'
    """
    db.execute_sql(sql, (run_id,))


if __name__ == "__main__":
    sys.exit(main())
