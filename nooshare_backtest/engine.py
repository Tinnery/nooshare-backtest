"""单一标准执行内核（Canonical Engine）。

事件驱动逐交易日推进，是唯一的回测执行路径。批量运行与性能优化都复用本内核，
不允许因性能理由分叉出另一套成交/记账语义。

时间语义：T 日收盘后的目标组合只在下一交易日（T+1）按开盘价执行。
本内核不产生策略，只忠实执行给出的目标组合。

V0.3 新增：
- Trade ID 在首笔 BUY Fill 时早期建立，通过 PositionView 暴露给策略回调
- ExitEvent 完整身份（run_id + trade_id + decision_date + reason + prices）
- 退出重试机制：止盈/止损触发后持续重试直到完全平仓或回测结束
- 止损优先于止盈（同日同时触发时）
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
    EXIT_STOP_LOSS,
    EXIT_TAKE_PROFIT,
    ExitEvent,
    Order,
    PositionView,
    RunMetadata,
    TargetDeviation,
    TargetPortfolio,
    event_id,
    exit_event_id,
)
from .rules import RuleSet
from .trades import build_trades

FILL_POLICY = (
    "T+1 next-session open (NEXT_OPEN); raw-price execution with conservative no-fill"
    " at limit-open; HFQ research mark-to-market; daily OHLC simulated fills;"
    " no intraday queue reconstruction"
)
ENGINE_VERSION = "0.4.0"

# 决策日 -> 目标组合；或逐日决策回调（收盘后调用，返回 None 表示当日无决策）。
# 回调收到的持仓/权益是引擎的真实状态，策略据此表达路径依赖规则（如止盈止损），
# 决策语义仍完全留在策略侧，引擎不判断任何策略规则。
TargetsInput = Mapping[int, TargetPortfolio] | Callable[["DecisionContext"], "TargetPortfolio | None"]


@dataclass(frozen=True)
class DecisionContext:
    """收盘决策时点交给策略的引擎真实状态。

    date 为收盘决策日；positions 为 T+1 口径下的持仓视图
    （first_entry_date/first_entry_price 可用于路径依赖退出规则的建仓基准；
    trade_id 在首笔 BUY Fill 时建立，供策略直接引用）。
    pending_exits 为尚未完成的退出意图列表，策略可据此决定是否继续重试。
    """
    date: int
    bars: Mapping[str, Bar]
    positions: tuple[PositionView, ...]
    cash: float
    total_asset: float
    pending_exits: tuple[PendingExit, ...] = ()


@dataclass(frozen=True)
class PendingExit:
    """尚未完成的退出意图。

    首次触发止盈/止损时创建，后续日期即使没有再次碰到阈值也继续发出 SELL 目标。
    所有重试订单引用同一个 exit_event_id。完全平仓后才结束这项退出意图。
    """
    exit_event_id: str
    trade_id: str
    symbol: str
    reason: str               # STOP_LOSS / TAKE_PROFIT
    decision_date: int        # 首次触发日（T 日收盘判断）
    reference_price: float    # 触发时参考价（建仓价或收盘价）
    stop_price: float         # 止损价（0 表示无止损）
    take_price: float         # 止盈价（0 表示无止盈）
    remaining_shares: int     # 尚未卖出的数量


@dataclass(frozen=True)
class EngineConfig:
    initial_cash: float = 1_000_000.0
    run_id: str = "run"
    strategy_version: str = "unknown"
    data_version: str = "unknown"
    cost_model: CostModel = CostModel()
    rules: RuleSet = RuleSet()
    execution: ExecutionConfig = ExecutionConfig()


@dataclass
class _ExitIntent:
    """引擎内部维护的退出意图（可变，跟踪剩余数量）。"""
    exit_event_id: str
    trade_id: str
    symbol: str
    reason: str
    decision_date: int
    reference_price: float
    stop_price: float
    take_price: float
    remaining_shares: int


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

    # 退出意图：symbol -> _ExitIntent（同一 symbol 同时只维护一个退出意图）
    pending_exits: dict[str, _ExitIntent] = {}
    # Trade ID 早期建立：symbol -> trade_id（首笔 BUY Fill 时建立）
    trade_ids: dict[str, str] = {}
    trade_seq = 0

    equity_curve: list = []
    positions: list = []
    orders: list = []
    fills: list = []
    deviations: list = []
    cost_total = CostSummary()
    exit_events_by_id: dict[str, ExitEvent] = {}  # 跟踪所有退出事件

    for date in dates:
        day_bars = dict(bars_by_date[date])

        # 1) 先执行上一交易日的决策（T -> T+1），使用上一收盘的总资产定目标股数
        # 即使 pending 为 None，只要有 pending_exits 也要执行退出订单
        if pending is not None or pending_exits:
            # 构造待退出意图字典供 plan_orders 使用
            pending_exits_dict = {
                symbol: {
                    "exit_event_id": ei.exit_event_id,
                    "reason": ei.reason,
                }
                for symbol, ei in pending_exits.items()
            }
            # 如果有 pending target，正常规划订单；否则只规划退出订单
            if pending is not None:
                planned = plan_orders(
                    pending, portfolio, day_bars, last_total_asset,
                    cfg.execution, exec_date=date, run_id=cfg.run_id,
                    pending_exits=pending_exits_dict,
                    trade_ids=trade_ids,
                )
            else:
                # 只有退出订单，没有常规调仓
                planned = plan_orders(
                    TargetPortfolio(decision_date=date, weights={}),
                    portfolio, day_bars, last_total_asset,
                    cfg.execution, exec_date=date, run_id=cfg.run_id,
                    pending_exits=pending_exits_dict,
                    trade_ids=trade_ids,
                )
            
            # Prepare mutable trade_seq for execute_orders to update
            trade_seq_mut = [trade_seq]
            
            day_fills, day_orders, day_devs = execute_orders(
                planned, portfolio, day_bars, date, cfg.rules, cfg.cost_model, cfg.execution,
                run_id=cfg.run_id, trade_ids=trade_ids, trade_seqs=trade_seq_mut,
            )
            # Sync trade_seq back from execute_orders
            trade_seq = trade_seq_mut[0]
            
            fills.extend(day_fills)
            orders.extend(day_orders)
            deviations.extend(day_devs)
            
            # 更新待退出意图的剩余数量
            for fill in day_fills:
                # 首笔 BUY Fill 时建立 Trade ID（如果 symbol 不在 trade_ids 中，说明是新持仓周期）
                if fill.side == "BUY" and fill.symbol not in trade_ids:
                    tid = event_id(cfg.run_id, "trade", fill.symbol, trade_seq)
                    trade_seq += 1
                    trade_ids[fill.symbol] = tid
                # 如果是 SELL 且有对应的待退出意图，更新剩余数量
                if fill.side == "SELL" and fill.symbol in pending_exits:
                    pending_exits[fill.symbol].remaining_shares -= fill.shares
                    # 更新 ExitEvent 状态
                    if fill.exit_event_id and fill.exit_event_id in exit_events_by_id:
                        old_ee = exit_events_by_id[fill.exit_event_id]
                        if pending_exits[fill.symbol].remaining_shares <= 0:
                            new_status = "complete"
                        else:
                            new_status = "partial"
                        exit_events_by_id[fill.exit_event_id] = ExitEvent(
                            exit_event_id=old_ee.exit_event_id,
                            trade_id=old_ee.trade_id,
                            decision_date=old_ee.decision_date,
                            reason=old_ee.reason,
                            reference_price=old_ee.reference_price,
                            stop_price=old_ee.stop_price,
                            take_price=old_ee.take_price,
                            status=new_status,
                        )
                    if pending_exits[fill.symbol].remaining_shares <= 0:
                        del pending_exits[fill.symbol]
                # 如果 SELL 完全平仓，移除 trade_id 以便下次建仓时生成新 ID
                if fill.side == "SELL" and portfolio.shares_of(fill.symbol) == 0:
                    trade_ids.pop(fill.symbol, None)
                
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
            # 构造 pending_exits 快照给策略
            exit_snapshots = tuple(
                PendingExit(
                    exit_event_id=ei.exit_event_id,
                    trade_id=ei.trade_id,
                    symbol=ei.symbol,
                    reason=ei.reason,
                    decision_date=ei.decision_date,
                    reference_price=ei.reference_price,
                    stop_price=ei.stop_price,
                    take_price=ei.take_price,
                    remaining_shares=ei.remaining_shares,
                )
                for ei in pending_exits.values()
            )
            ctx = DecisionContext(
                date=date,
                bars=day_bars,
                positions=tuple(portfolio.position_views(date)),
                cash=portfolio.cash,
                total_asset=point.total_asset,
                pending_exits=exit_snapshots,
            )
            pending = targets(ctx)
            # 处理策略返回的退出信号
            if pending and pending.exit_signals:
                for symbol, (reason, ref_px, stop_px, take_px) in pending.exit_signals.items():
                    if symbol not in pending_exits:
                        # 首次触发：创建新的退出意图
                        trade_id = trade_ids.get(symbol, "")
                        ee_id = exit_event_id(
                            cfg.run_id, trade_id, date,
                            reason, ref_px, stop_px, take_px,
                        )
                        shares = portfolio.shares_of(symbol)
                        pending_exits[symbol] = _ExitIntent(
                            exit_event_id=ee_id,
                            trade_id=trade_id,
                            symbol=symbol,
                            reason=reason,
                            decision_date=date,
                            reference_price=ref_px,
                            stop_price=stop_px,
                            take_price=take_px,
                            remaining_shares=shares,
                        )
                        # 创建 ExitEvent 跟踪
                        exit_events_by_id[ee_id] = ExitEvent(
                            exit_event_id=ee_id,
                            trade_id=trade_id,
                            decision_date=date,
                            reason=reason,
                            reference_price=ref_px,
                            stop_price=stop_px,
                            take_price=take_px,
                            status="pending",
                        )
        else:
            pending = targets.get(date)
            # 处理静态目标中的退出信号
            if pending and pending.exit_signals:
                for symbol, (reason, ref_px, stop_px, take_px) in pending.exit_signals.items():
                    if symbol not in pending_exits:
                        trade_id = trade_ids.get(symbol, "")
                        ee_id = exit_event_id(
                            cfg.run_id, trade_id, date,
                            reason, ref_px, stop_px, take_px,
                        )
                        shares = portfolio.shares_of(symbol)
                        pending_exits[symbol] = _ExitIntent(
                            exit_event_id=ee_id,
                            trade_id=trade_id,
                            symbol=symbol,
                            reason=reason,
                            decision_date=date,
                            reference_price=ref_px,
                            stop_price=stop_px,
                            take_price=take_px,
                            remaining_shares=shares,
                        )
                        # 创建 ExitEvent 跟踪
                        exit_events_by_id[ee_id] = ExitEvent(
                            exit_event_id=ee_id,
                            trade_id=trade_id,
                            decision_date=date,
                            reason=reason,
                            reference_price=ref_px,
                            stop_price=stop_px,
                            take_price=take_px,
                            status="pending",
                        )

    # 最后一日未执行的决策显式留痕
    if pending is not None and dates:
        for symbol, weight in sorted(pending.weights.items()):
            deviations.append(TargetDeviation(
                date=dates[-1], symbol=symbol, target_weight=weight,
                target_shares=0, filled_shares=0, reason="NO_NEXT_SESSION",
            ))

    # 回测结束仍未退出的 ExitEvent 显式留痕
    for ei in pending_exits.values():
        if ei.remaining_shares > 0:
            deviations.append(TargetDeviation(
                date=dates[-1] if dates else 0,
                symbol=ei.symbol,
                target_weight=0.0,
                target_shares=ei.remaining_shares,
                filled_shares=0,
                reason=f"PENDING_EXIT_{ei.reason}_INCOMPLETE",
            ))
            # 标记 ExitEvent 为 abandoned
            if ei.exit_event_id in exit_events_by_id:
                old_ee = exit_events_by_id[ei.exit_event_id]
                exit_events_by_id[ei.exit_event_id] = ExitEvent(
                    exit_event_id=old_ee.exit_event_id,
                    trade_id=old_ee.trade_id,
                    decision_date=old_ee.decision_date,
                    reason=old_ee.reason,
                    reference_price=old_ee.reference_price,
                    stop_price=old_ee.stop_price,
                    take_price=old_ee.take_price,
                    status="abandoned",
                )

    # 从实际成交事件流构建交易生命周期与分配关系
    trades, allocations = build_trades(fills, dates, run_id=cfg.run_id, orders=orders)

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
            "enable_t_plus_1": cfg.rules.enable_t_plus_1,
            "enable_st_filter": cfg.rules.enable_st_filter,
            "price_convention": "raw_execution_hfq_mark",
            "trading_days": len(dates),
        },
        strategy_version=cfg.strategy_version,
        data_version=cfg.data_version,
        fill_policy=FILL_POLICY,
    )
    metrics = compute_metrics(equity_curve, fills, cfg.initial_cash)
    metrics["trade_count"] = float(len(trades))  # 交易生命周期数，不是 Fill 数
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
        exit_events=list(exit_events_by_id.values()),
    )
