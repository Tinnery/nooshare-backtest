"""面向 Python / CLI / Strategy / 测试程序的统一入口。

只接受市场数据与策略目标组合，返回可序列化的 BacktestResult。
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

from .adapters.market_data import normalize_bars
from .adapters.strategy import normalize_targets
from .costs import CostModel
from .engine import EngineConfig, run
from .execution import ExecutionConfig
from .models import BacktestResult, Bar, TargetPortfolio
from .rules import RuleSet


def run_backtest(
    bars: Mapping[int, Any] | Iterable[Bar] | Iterable[Mapping[str, Any]],
    decisions: Mapping[int, Any] | Iterable[Any],
    *,
    initial_cash: float = 1_000_000.0,
    run_id: str = "run",
    strategy_version: str = "unknown",
    data_version: str = "unknown",
    cost_model: CostModel | None = None,
    rules: RuleSet | None = None,
    execution: ExecutionConfig | None = None,
) -> BacktestResult:
    """运行一次回测。

    bars 可以是 {date: {symbol: Bar}}、Bar 列表或记录列表；
    decisions 可以是 {decision_date: TargetPortfolio} 或目标组合列表/字典列表。
    两者都由 adapters 归一化后交给唯一的 Canonical Engine。
    """
    normalized_bars = normalize_bars(bars)
    normalized_targets = normalize_targets(decisions)
    config = EngineConfig(
        initial_cash=initial_cash,
        run_id=run_id,
        strategy_version=strategy_version,
        data_version=data_version,
        cost_model=cost_model or CostModel(),
        rules=rules or RuleSet(),
        execution=execution or ExecutionConfig(),
    )
    return run(normalized_bars, normalized_targets, config)
