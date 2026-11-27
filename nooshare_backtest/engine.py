"""单一标准执行内核（Canonical Engine）。

事件驱动逐交易日推进，是唯一的回测执行路径。批量运行与性能优化都复用本内核，
不允许因性能理由分叉出另一套成交/记账语义。

时间语义：T 日收盘后的目标组合只在下一交易日（T+1）按开盘价执行。
本内核不产生策略，只忠实执行给出的目标组合。

V0.3：Trade ID 早期建立（首笔 BUY Fill 时确定性生成）、ExitEvent 完整身份、
退出重试机制（pending_exit_event_id 在跌停拒绝后保留，后续日期继续尝试）。
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Mapping

from .accounting import Portfolio
from .costs import CostModel
from .execution import ExecutionConfig, execute_orders, plan_orders
from .metrics import compute_metrics
from .models import (
    BacktestResult,
    Bar,
    CostSummary,
    ExitEvent,
    Fill,
    PositionView,
    RunMetadata,
    TargetDeviation,
    TargetPortfolio,
    event_id,
    exit_event_id,
)
from .rules import RuleSet
from .trades import build_trades

FILL_POLICY = "T+1 next-session open; daily OHLC simulated fills; no intraday queue reconstruction"
ENGINE_VERSION = "0.3.0"

# 决策日 -> 目标组合；或逐日决策回调（收盘后调用，返回 None 表示当日无决策）。
# 回调收到的持仓/权益是引擎的真实状态，策略据此表达路径依赖规则（如止盈止损），
# 决策语义仍完全留在策略侧，引擎不判断任何策略规则。
TargetsInput = Mapping[int, TargetPortfolio] | Callable[["DecisionContext"], "TargetPortfolio | None"]


@dataclass(frozen=True)
class DecisionContext:
    """收盘决策时点交给策略的引擎真实状态。

    date 为收盘决策日；positions 为 T+1 口径下的持仓视图
    （first_entry_date/first_entry_price 可用于路径依赖退出规则的建仓基准）。
    trade_id 在首笔 BUY Fill 时建立，供退出事件关联。
    pending_exits: 当前待退出状态 {symbol: exit_event_id}，策略可据此决定是否
    复用已有退出事件而非创建新事件（退出重试机制）。
    """
    date: int
    bars: Mapping[str, Bar]
    positions: tuple[PositionView, ...]
    cash: float
    total_asset: float
    pending_exits: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class EngineConfig:
    initial_cash: float = 1_000_000.0
    run_id: str = "run"
    strategy_version: str = "unknown"
    data_version: str = "unknown"
    cost_model: CostModel = CostModel()
    rules: RuleSet = RuleSet()
    execution: ExecutionConfig = ExecutionConfig()


def _pre_assign_trade_ids(fills: list[Fill], run_id: str) -> dict[str, list[str]]:
    """Pre-scan fills to assign deterministic trade_ids.

    Returns a dict mapping symbol to a list of trade_ids (one per trade lifecycle).
    Handles close-and-reopen: each new position gets a new trade_id.
    """
    open_syms: set[str] = set()
    seqs: dict[str, int] = {}
    all_tids: dict[str, list[str]] = {}

    for fill in fills:
        if fill.side == "BUY" and fill.symbol not in open_syms:
            seq = seqs.get(fill.symbol, 0)
            tid = event_id(run_id, "trade", fill.symbol, seq)
            seqs[fill.symbol] = seq + 1
            open_syms.add(fill.symbol)
            all_tids.setdefault(fill.symbol, []).append(tid)
        elif fill.side == "SELL" and fill.symbol in open_syms:
            open_syms.discard(fill.symbol)

    return all_tids


def run(
    bars_by_date: Mapping[int, Mapping[str, Bar]],
    targets: TargetsInput,
    config: EngineConfig | None = None,
) -> BacktestResult:
    """执行一次回测。

    bars_by_date: 交易日 -> {symbol: Bar}
    targets: 决策日 -> TargetPortfolio（只能是 bars_by_date 中某个交易日收盘后形成），
             或决策回调 Callable[DecisionContext, TargetPortfolio | None]。
    最后一天的决策因没有下一交易日，记为 NO_NEXT_SESSION 差异，不会静默丢弃。
    """
    cfg = config or EngineConfig()
    dates = sorted(bars_by_date)
    portfolio = Portfolio(cash=cfg.initial_cash, enable_t_plus_1=cfg.rules.enable_t_plus_1)
    pending: TargetPortfolio | None = None
    last_total_asset = cfg.initial_cash

    equity_curve: list = []
    positions: list = []
    orders: list = []
    fills: list = []
    deviations: list = []
    exit_events: list[ExitEvent] = []
    cost_total = CostSummary()

    for date in dates:
        day_bars = dict(bars_by_date[date])

        # 1) 先执行上一交易日的决策（T -> T+1），使用上一收盘的总资产定目标股数
        if pending is not None:
            planned = plan_orders(pending, portfolio, day_bars, last_total_asset,
                                  cfg.execution, exec_date=date, run_id=cfg.run_id)
            day_fills, day_orders, day_devs = execute_orders(
                planned, portfolio, day_bars, date, cfg.rules, cfg.cost_model, cfg.execution
            )
            fills.extend(day_fills)
            orders.extend(day_orders)
            deviations.extend(day_devs)

            # 首笔 BUY Fill 时建立 trade_id（确定性生成）
            # 使用 pre-scan 方式，在循环结束后统一分配
            # 此处仅更新 portfolio 的 trade_id 供 PositionView 使用
            for fill in day_fills:
                if fill.side == "BUY":
                    pos = portfolio.positions.get(fill.symbol)
                    if pos and not pos.trade_id:
                        # 临时分配，后续会被 pre_assign 覆盖
                        tid = event_id(cfg.run_id, "trade", fill.symbol, 0)
                        portfolio.set_trade_id(fill.symbol, tid)

            # 处理退出事件：
            # - SELL 被拒绝（如 LIMIT_DOWN）→ 保留 pending_exit_event_id
            # - 持仓完全平仓（shares == 0）→ 清除 pending_exit_event_id
            # - 持仓仍有剩余 → 保留 pending_exit_event_id（继续卖出剩余）
            for order in day_orders:
                if order.side == "SELL" and order.exit_event_id:
                    # 检查持仓是否完全平仓
                    remaining_shares = portfolio.shares_of(order.symbol)
                    if remaining_shares == 0:
                        portfolio.clear_pending_exit(order.symbol)
                    else:
                        # 保留 pending_exit_event_id，后续日期继续尝试
                        portfolio.set_pending_exit(order.symbol, order.exit_event_id)

            # 累计成本
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
        if callable(targets):
            # 构建 pending_exits 映射
            pending_exits: dict[str, str] = {}
            for sym in list(portfolio.positions.keys()):
                pe = portfolio.get_pending_exit(sym)
                if pe:
                    pending_exits[sym] = pe
            ctx = DecisionContext(
                date=date,
                bars=day_bars,
                positions=tuple(portfolio.position_views(date)),
                cash=portfolio.cash,
                total_asset=point.total_asset,
                pending_exits=pending_exits,
            )
            result_target = targets(ctx)
            pending = result_target
            # 收集策略回调产生的 ExitEvent
            if pending is not None:
                for sym, eid in pending.exit_event_ids.items():
                    reason = pending.decision_reasons.get(sym, "")
                    bar = day_bars.get(sym)
                    ref_price = bar.close if bar else 0.0
                    ee = ExitEvent(
                        exit_event_id=eid,
                        trade_id=portfolio.get_trade_id(sym),
                        symbol=sym,
                        decision_date=date,
                        reason=reason,
                        reference_price=ref_price,
                    )
                    exit_events.append(ee)
                    # 设置 pending_exit 以便后续重试
                    portfolio.set_pending_exit(sym, eid)
        else:
            pending = targets.get(date)

    # 最后一日未执行的决策显式留痕
    if pending is not None and dates:
        for symbol, weight in sorted(pending.weights.items()):
            deviations.append(TargetDeviation(
                date=dates[-1], symbol=symbol, target_weight=weight,
                target_shares=0, filled_shares=0, reason="NO_NEXT_SESSION",
            ))

    # Pre-scan fills to assign deterministic trade_ids (reproducible, not UUID4).
    # This handles close-and-reopen correctly: each new position gets a new trade_id.
    all_trade_ids = _pre_assign_trade_ids(fills, cfg.run_id)

    # 从实际成交事件流构建交易生命周期与分配关系
    trades, allocations = build_trades(
        fills, dates, run_id=cfg.run_id, orders=orders,
        pre_assigned_trade_ids=all_trade_ids,
    )

    ledger = list(portfolio.ledger)
    metadata = RunMetadata(
        run_id=cfg.run_id,
        engine_version=ENGINE_VERSION,
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
    metrics = compute_metrics(equity_curve, fills, cfg.initial_cash)
    metrics["trade_count"] = float(len(trades))
    return BacktestResult(
        metadata=metadata,
        equity_curve=equity_curve,
        positions=positions,
        orders=orders,
        fills=fills,
        trades=trades,
        trade_fill_allocations=allocations,
        ledger=ledger,
        costs=cost_total,
        metrics=metrics,
        deviations=deviations,
        exit_events=exit_events,
    )
