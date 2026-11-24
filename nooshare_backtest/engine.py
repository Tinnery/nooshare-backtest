"""单一标准执行内核（Canonical Engine）。

事件驱动逐交易日推进，是唯一的回测执行路径。批量运行与性能优化都复用本内核，
不允许因性能理由分叉出另一套成交/记账语义。

时间语义：T 日收盘后的目标组合只在下一交易日（T+1）按开盘价执行。
本内核不产生策略，只忠实执行给出的目标组合。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .accounting import Portfolio
from .costs import CostModel
from .execution import ExecutionConfig, execute_orders, plan_orders
from .metrics import compute_metrics
from .models import (
    BacktestResult,
    Bar,
    CostSummary,
    RunMetadata,
    TargetDeviation,
    TargetPortfolio,
)
from .rules import RuleSet

FILL_POLICY = "T+1 next-session open; daily OHLC simulated fills; no intraday queue reconstruction"


@dataclass(frozen=True)
class EngineConfig:
    initial_cash: float = 1_000_000.0
    run_id: str = "run"
    strategy_version: str = "unknown"
    data_version: str = "unknown"
    cost_model: CostModel = CostModel()
    rules: RuleSet = RuleSet()
    execution: ExecutionConfig = ExecutionConfig()


def run(
    bars_by_date: Mapping[int, Mapping[str, Bar]],
    targets: Mapping[int, TargetPortfolio],
    config: EngineConfig | None = None,
) -> BacktestResult:
    """执行一次回测。

    bars_by_date: 交易日 -> {symbol: Bar}
    targets: 决策日 -> TargetPortfolio（只能是 bars_by_date 中某个交易日收盘后形成）
    最后一天的决策因没有下一交易日，记为 NO_NEXT_SESSION 差异，不会静默丢弃。
    """
    cfg = config or EngineConfig()
    dates = sorted(bars_by_date)
    portfolio = Portfolio(cash=cfg.initial_cash)
    pending: TargetPortfolio | None = None
    last_total_asset = cfg.initial_cash

    equity_curve: list = []
    positions: list = []
    orders: list = []
    fills: list = []
    ledger: list = []
    deviations: list = []
    cost_total = CostSummary()

    for date in dates:
        day_bars = dict(bars_by_date[date])

        # 1) 先执行上一交易日的决策（T -> T+1），使用上一收盘的总资产定目标股数
        if pending is not None:
            planned = plan_orders(pending, portfolio, day_bars, last_total_asset,
                                  cfg.execution, exec_date=date)
            day_fills, day_orders, day_devs = execute_orders(
                planned, portfolio, day_bars, date, cfg.rules, cfg.cost_model, cfg.execution
            )
            fills.extend(day_fills)
            orders.extend(day_orders)
            deviations.extend(day_devs)
            for fill in day_fills:
                cost_total = CostSummary(
                    commission=cost_total.commission + fill.commission,
                    stamp_duty=cost_total.stamp_duty + fill.stamp_duty,
                    transfer_fee=cost_total.transfer_fee + fill.transfer_fee,
                    slippage=cost_total.slippage + fill.slippage,
                )

        # 2) 收盘盯市
        point, snaps = portfolio.mark(date, day_bars, cfg.initial_cash)
        equity_curve.append(point)
        positions.extend(snaps)
        last_total_asset = point.total_asset

        # 3) 收盘后接收当日决策，留待下一交易日执行
        pending = targets.get(date)

    # 最后一日未执行的决策显式留痕
    if pending is not None and dates:
        for symbol, weight in sorted(pending.weights.items()):
            deviations.append(TargetDeviation(
                date=dates[-1], symbol=symbol, target_weight=weight,
                target_shares=0, filled_shares=0, reason="NO_NEXT_SESSION",
            ))

    ledger = list(portfolio.ledger)
    metadata = RunMetadata(
        run_id=cfg.run_id,
        engine_version="0.1.0",
        config={
            "initial_cash": cfg.initial_cash,
            "lot": cfg.execution.lot,
            "allow_fractional": cfg.execution.allow_fractional,
            "commission_bp": cfg.cost_model.commission_bp,
            "stamp_bp": cfg.cost_model.stamp_bp,
            "slippage_bp": cfg.cost_model.slippage_bp,
            "transfer_bp": cfg.cost_model.transfer_bp,
            "fake_gap_limit": cfg.rules.fake_gap_limit,
            "enable_t_plus_1": cfg.rules.enable_t_plus_1,
            "enable_st_filter": cfg.rules.enable_st_filter,
            "trading_days": len(dates),
        },
        strategy_version=cfg.strategy_version,
        data_version=cfg.data_version,
        fill_policy=FILL_POLICY,
    )
    return BacktestResult(
        metadata=metadata,
        equity_curve=equity_curve,
        positions=positions,
        orders=orders,
        fills=fills,
        ledger=ledger,
        costs=cost_total,
        metrics=compute_metrics(equity_curve, fills, cfg.initial_cash),
        deviations=deviations,
    )
