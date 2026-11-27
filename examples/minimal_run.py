"""最小可运行示例：T 日决策，T+1 交易日开盘用 Raw 执行。"""
from nooshare_backtest import run_backtest
from nooshare_backtest.models import Bar


def build_example_result():
    """Build a deterministic example with resolved PIT limit metadata."""
    bars = {
        20240102: {
            "AAA": Bar(
                date=20240102, symbol="AAA",
                open=10.0, high=10.2, low=9.9, close=10.1,
                volume=100_000, mark_price=10.1,
                pre_close=10.0, limit_up_ratio=0.10, limit_down_ratio=0.10,
                limit_rule_source="example_pit_fixture",
            )
        },
        20240103: {
            "AAA": Bar(
                date=20240103, symbol="AAA",
                open=10.2, high=10.5, low=10.1, close=10.4,
                volume=100_000, mark_price=10.4,
                pre_close=10.1, limit_up_ratio=0.10, limit_down_ratio=0.10,
                limit_rule_source="example_pit_fixture",
            )
        },
    }
    decisions = {20240102: {"AAA": 0.5}}
    return run_backtest(
        bars, decisions, initial_cash=100_000.0, run_id="minimal"
    )


def main() -> None:
    result = build_example_result()
    print(result.metrics)
    print("orders:", result.orders)
    print("fills:", result.fills)
    print("deviations:", result.deviations)


if __name__ == "__main__":
    main()
