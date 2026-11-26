"""执行层：目标组合 → 订单 → 成交。

流程（执行日）：
1. 用执行日开盘价把目标权重换算为目标股数（按组合当前总资产）；
2. 与目标持仓比较，生成 BUY/SELL 订单（A 股整手 100 股）；
3. 逐单经交易规则判定，可成交则按成本模型模拟成交，否则记录拒绝原因；
4. 买入受可用现金约束，现金不足则部分成交，差额记入 TargetDeviation。

订单与成交都携带稳定身份：order_id 由 target.decision_date 与序号确定性生成；
fill_id 由 order_id 与序号生成。信号引用（signal_id）从 TargetPortfolio 透传，
执行层不生成也不判断信号。部分成交状态必须诚实表达（PARTIALLY_FILLED）。
"""
from __future__ import annotations

from dataclasses import dataclass

from .accounting import Portfolio
from .costs import CostModel
from .models import Bar, Fill, Order, TargetDeviation, TargetPortfolio, event_id
from .rules import RuleSet

LOT = 100

ORDER_PENDING = "PENDING"
ORDER_PARTIAL = "PARTIALLY_FILLED"
ORDER_FILLED = "FILLED"
ORDER_REJECTED = "REJECTED"
ORDER_SKIPPED = "SKIPPED"


@dataclass(frozen=True)
class ExecutionConfig:
    lot: int = LOT
    allow_fractional: bool = False


def _round_shares(shares: float, cfg: ExecutionConfig) -> int:
    if cfg.allow_fractional:
        return max(0, int(shares))
    return max(0, int(shares // cfg.lot) * cfg.lot)


def _identity(target: TargetPortfolio, symbol: str) -> tuple[str, str]:
    if target.decision_date and symbol in target.signal_ids:
        return event_id(target.decision_date, "decision", symbol), target.signal_ids[symbol]
    return "", ""


def plan_orders(
    target: TargetPortfolio,
    portfolio: Portfolio,
    bars: dict[str, Bar],
    total_asset: float,
    cfg: ExecutionConfig,
    exec_date: int | None = None,
    run_id: str = "run",
) -> list[Order]:
    """按目标权重生成订单。以执行日开盘价估算目标股数。

    exec_date 为实际执行日（T 日决策的下一交易日）；未传时兼容旧调用使用决策日。
    target.hold 中的持仓保持现状，不生成订单（不调仓、不清仓）。

    trade_id 传递规则：
    - 普通目标权重产生的 SELL：从 portfolio.get_trade_id(symbol) 获取
    - 加仓 BUY（已有持仓）：从 portfolio.get_trade_id(symbol) 获取
    - 首次 BUY（尚无持仓）：trade_id 为空
    """
    date = exec_date if exec_date is not None else target.decision_date
    orders: list[Order] = []
    # 目标组合中未列出的持仓视为目标权重 0，需要卖出；不能只遍历 weights 键。
    # hold 集合显式排除：策略声明保持现状的持仓不参与订单推导。
    symbols = sorted(
        (set(target.weights) | set(portfolio.positions)) - set(target.hold)
    )
    for idx, symbol in enumerate(symbols):
        weight = target.weights.get(symbol, 0.0)
        bar = bars.get(symbol)
        px = bar.open if bar and bar.open > 0 else 0.0
        target_value = weight * total_asset
        want = _round_shares(target_value / px, cfg) if px > 0 else 0
        have = portfolio.shares_of(symbol)
        delta = want - have
        if delta == 0:
            continue
        side = "BUY" if delta > 0 else "SELL"
        decision_id, signal_id = _identity(target, symbol)
        oid = event_id(run_id, "order", date, idx)
        # 退出事件透传：策略层为 SELL 给出的 exit_event_id 和 decision_reason
        exit_eid = target.exit_event_ids.get(symbol, "") if side == "SELL" else ""
        dec_reason = target.decision_reasons.get(symbol, "") if side == "SELL" else ""
        # trade_id 传递：
        # - SELL: 从 portfolio 获取现有持仓的 trade_id
        # - BUY: 如果已有持仓（加仓），从 portfolio 获取；首次建仓则为空
        trade_id = portfolio.get_trade_id(symbol) if (side == "SELL" or have > 0) else ""
        orders.append(Order(
            date=date, symbol=symbol, side=side,
            requested_shares=abs(delta), target_weight=weight,
            order_id=oid, signal_id=signal_id,
            decision_date=target.decision_date,
            canonical_id=bar.canonical_id if bar else "",
            exchange=bar.exchange if bar else "",
            trade_id=trade_id,
            exit_event_id=exit_eid, decision_reason=dec_reason,
        ))
    # 先卖后买，释放现金供买入；order_id 已确定，顺序稳定可复现。
    orders.sort(key=lambda o: (o.side != "SELL", o.symbol))
    return orders


def _order_with_final(
    order: Order, *, status: str, filled: int, reason: str = "",
) -> Order:
    return Order(
        date=order.date, symbol=order.symbol, side=order.side,
        requested_shares=order.requested_shares, target_weight=order.target_weight,
        status=status, reason=reason,
        order_id=order.order_id, signal_id=order.signal_id,
        decision_date=order.decision_date,
        canonical_id=order.canonical_id, exchange=order.exchange,
        filled_shares=filled,
        trade_id=order.trade_id,
        exit_event_id=order.exit_event_id, decision_reason=order.decision_reason,
    )


def _fill(
    order: Order, bd: dict[str, float], date: int, seq: int, shares: int,
) -> Fill:
    return Fill(
        date=date, symbol=order.symbol, side=order.side, shares=shares,
        price=bd["price"], commission=bd["commission"],
        stamp_duty=bd["stamp_duty"], transfer_fee=bd["transfer_fee"],
        slippage=bd["slippage"],
        fill_id=event_id(order.order_id or "run", "fill", date, seq),
        order_id=order.order_id,
        trade_id=order.trade_id,
        canonical_id=order.canonical_id, exchange=order.exchange,
    )


def execute_orders(
    orders: list[Order],
    portfolio: Portfolio,
    bars: dict[str, Bar],
    date: int,
    rules: RuleSet,
    costs: CostModel,
    cfg: ExecutionConfig,
) -> tuple[list[Fill], list[Order], list[TargetDeviation]]:
    """逐单执行：规则判定 → 成交模拟 → 落账 → 记录差异。"""
    fills: list[Fill] = []
    final_orders: list[Order] = []
    deviations: list[TargetDeviation] = []

    # 先处理卖出
    for seq, order in enumerate([o for o in orders if o.side == "SELL"]):
        bar = bars.get(order.symbol)
        reason = rules.can_sell(bar, not bool(portfolio.sellable_shares(order.symbol, date)))
        sellable = portfolio.sellable_shares(order.symbol, date)
        shares = min(order.requested_shares, portfolio.shares_of(order.symbol), sellable)
        if reason:
            final_orders.append(_order_with_final(order, status=ORDER_REJECTED, filled=0, reason=reason))
            deviations.append(TargetDeviation(date, order.symbol, order.target_weight,
                                              order.requested_shares, 0, reason))
            continue
        if shares <= 0:
            block = "T_PLUS_1" if portfolio.shares_of(order.symbol) > 0 else "NO_POSITION"
            final_orders.append(_order_with_final(order, status=ORDER_SKIPPED, filled=0, reason=block))
            continue
        bd = costs.breakdown(bar.open, shares, "SELL")
        fill = _fill(order, bd, date, seq, shares)
        portfolio.apply_fill(fill)
        fills.append(fill)
        status = ORDER_FILLED if shares == order.requested_shares else ORDER_PARTIAL
        final_orders.append(_order_with_final(order, status=status, filled=shares))
        if shares < order.requested_shares:
            deviations.append(TargetDeviation(date, order.symbol, order.target_weight,
                                              order.requested_shares, shares,
                                              "T_PLUS_1" if sellable < order.requested_shares else "PARTIAL_SELL"))

    # 再处理买入（受现金约束）
    for seq, order in enumerate([o for o in orders if o.side == "BUY"]):
        bar = bars.get(order.symbol)
        reason = rules.can_buy(bar)
        if reason:
            final_orders.append(_order_with_final(order, status=ORDER_REJECTED, filled=0, reason=reason))
            deviations.append(TargetDeviation(date, order.symbol, order.target_weight,
                                              order.requested_shares, 0, reason))
            continue
        eff_px = costs.buy_price(bar.open)
        affordable = int(portfolio.cash // (eff_px * cfg.lot)) * cfg.lot if cfg.lot else int(portfolio.cash // eff_px)
        if not cfg.allow_fractional:
            affordable = max(0, affordable)
        shares = min(order.requested_shares, affordable)
        if shares <= 0:
            final_orders.append(_order_with_final(order, status=ORDER_REJECTED, filled=0,
                                                  reason="INSUFFICIENT_CASH"))
            deviations.append(TargetDeviation(date, order.symbol, order.target_weight,
                                              order.requested_shares, 0, "INSUFFICIENT_CASH"))
            continue
        bd = costs.breakdown(bar.open, shares, "BUY")
        fill = _fill(order, bd, date, 1000 + seq, shares)
        portfolio.apply_fill(fill)
        fills.append(fill)
        status = ORDER_FILLED if shares == order.requested_shares else ORDER_PARTIAL
        final_orders.append(_order_with_final(order, status=status, filled=shares))
        if shares < order.requested_shares:
            deviations.append(TargetDeviation(date, order.symbol, order.target_weight,
                                              order.requested_shares, shares, "INSUFFICIENT_CASH"))

    return fills, final_orders, deviations
