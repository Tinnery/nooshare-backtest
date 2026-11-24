"""最小可运行示例：T 日决策，T+1 开盘执行。"""
from nooshare_backtest import run_backtest
from nooshare_backtest.models import Bar


def main() -> None:
    bars = {
        20240102: {"AAA": Bar(20240102, "AAA", 10.0, 10.2, 9.9, 10.1, 100_000)},
        20240103: {"AAA": Bar(20240103, "AAA", 10.2, 10.5, 10.1, 10.4, 100_000)},
    }
    decisions = {20240102: {"AAA": 0.5}}
    result = run_backtest(bars, decisions, initial_cash=100_000.0, run_id="minimal")
    print(result.metrics)
    print("orders:", result.orders)
    print("deviations:", result.deviations)


if __name__ == "__main__":
    main()
