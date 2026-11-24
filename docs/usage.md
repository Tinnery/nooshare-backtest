# 使用与 API 契约

系统设计见 `nooshare-design/architecture/backtest-implementation.md`。本文只描述本仓库的调用方式与输出结构。

## Python API

```python
from nooshare_backtest import run_backtest

result = run_backtest(
    bars={20240103: {"600000": Bar(...)}},   # 交易日 -> {symbol: Bar}
    decisions={20240102: {"600000": 0.5}},   # 决策日 -> {symbol: 目标权重}
    initial_cash=1_000_000,
    run_id="example",
)
print(result.metrics)
result.to_dict()  # JSON 可序列化，供 nooshare 产品层消费
```

`bars` 也接受 `Bar` 列表或记录 dict 列表；`decisions` 也接受 `TargetPortfolio` 列表或记录 dict 列表。适配逻辑在 `nooshare_backtest/adapters/`，只做归一化，不产生策略。

## CLI

```bash
backtest run  --input input.json --output-dir out --report
backtest show --result out/result.json
backtest report --result out/result.json --output out/report.html
```

`input.json` 结构：

```json
{
  "initial_cash": 1000000,
  "run_id": "demo",
  "strategy_version": "unknown",
  "data_version": "unknown",
  "bars": [
    {"date": 20240102, "symbol": "AAA", "open": 10, "high": 10.2,
     "low": 9.9, "close": 10.1, "volume": 1000000, "ret": 0.01}
  ],
  "decisions": [
    {"decision_date": 20240102, "weights": {"AAA": 0.5}}
  ]
}
```

输出：`result.json`（`BacktestResult` 全量结构）与可选 `report.html`（静态报告）。

## 结果结构

`BacktestResult` 包含：`metadata`、`equity_curve`、`positions`、`orders`、`fills`、`ledger`、`costs`、`metrics`、`deviations`。

- `orders[].status` ∈ `PENDING / FILLED / REJECTED / SKIPPED`；未成交必有 `reason`。
- `deviations` 记录目标股数、实际成交股数与原因，包括因规则拒绝、资金不足、无下一交易日的情形。
- `metadata.fill_policy` 明示模拟成交假设。
- `result_from_dict()` 可从 JSON 还原结果，用于 `show` / `report`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试用标准库 `unittest`，无运行时依赖。`tests/fixtures/sample.py` 是手工可计算的小型 Fixture；`tests/parity/` 固定与 Strategy 基线对齐的常量与公式。
