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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
