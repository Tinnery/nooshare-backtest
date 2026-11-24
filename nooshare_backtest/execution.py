"""执行层：目标组合 → 订单 → 成交。

流程（执行日）：
1. 用执行日开盘价把目标权重换算为目标股数（按组合当前总资产）；
2. 与目标持仓比较，生成 BUY/SELL 订单（A 股整手 100 股）；
3. 逐单经交易规则判定，可成交则按成本模型模拟成交，否则记录拒绝原因；
4. 买入受可用现金约束，现金不足则部分成交，差额记入 TargetDeviation。

所有未完全执行的目标都会产生 TargetDeviation，保证「目标≠成交」可查询、可解释。
"""
from __future__ import annotations

from dataclasses import dataclass

from .accounting import Portfolio
from .costs import CostModel
from .models import Bar, Fill, Order, TargetDeviation, TargetPortfolio
from .rules import RuleSet

LOT = 100


@dataclass(frozen=True)
class ExecutionConfig:
    lot: int = LOT
    allow_fractional: bool = False


def _round_shares(shares: float, cfg: ExecutionConfig) -> int:
    if cfg.allow_fractional:
        return max(0, int(shares))
    return max(0, int(shares // cfg.lot) * cfg.lot)


def plan_orders(
    target: TargetPortfolio,
    portfolio: Portfolio,
    bars: dict[str, Bar],
    total_asset: float,
    cfg: ExecutionConfig,
    exec_date: int | None = None,
) -> list[Order]:
    """按目标权重生成订单。以执行日开盘价估算目标股数。

    exec_date 为实际执行日（T 日决策的下一交易日）；未传时兼容旧调用使用决策日。
    """
    date = exec_date if exec_date is not None else target.decision_date
    orders: list[Order] = []
    # 目标组合中未列出的持仓视为目标权重 0，需要卖出；不能只遍历 weights 键。
    symbols = sorted(set(target.weights) | set(portfolio.positions))
    for symbol in symbols:
        weight = target.weights.get(symbol, 0.0)
        bar = bars.get(symbol)
        px = bar.open if bar and bar.open > 0 else 0.0
        target_value = weight * total_asset
        want = _round_shares(target_value / px, cfg) if px > 0 else 0
        have = portfolio.shares_of(symbol)
        delta = want - have
        if delta > 0:
            orders.append(Order(date, symbol, "BUY", delta, weight))
        elif delta < 0:
            orders.append(Order(date, symbol, "SELL", -delta, weight))
    # 先卖后买，释放现金供买入
    orders.sort(key=lambda o: (o.side != "SELL", o.symbol))
    return orders


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
    for order in [o for o in orders if o.side == "SELL"]:
        bar = bars.get(order.symbol)
        reason = rules.can_sell(bar, portfolio.bought_today(order.symbol, date))
        shares = min(order.shares, portfolio.shares_of(order.symbol))
        if reason:
            final_orders.append(Order(date, order.symbol, order.side, order.shares,
                                      order.target_weight, "REJECTED", reason))
            deviations.append(TargetDeviation(date, order.symbol, order.target_weight,
                                              order.shares, 0, reason))
            continue
        if shares <= 0:
            final_orders.append(Order(date, order.symbol, order.side, order.shares,
                                      order.target_weight, "SKIPPED", "NO_POSITION"))
            continue
        bd = costs.breakdown(bar.open, shares, "SELL")
        fill = Fill(date, order.symbol, "SELL", shares, bd["price"],
                    bd["commission"], bd["stamp_duty"], bd["transfer_fee"], bd["slippage"])
        portfolio.apply_fill(fill)
        fills.append(fill)
        final_orders.append(Order(date, order.symbol, order.side, shares,
                                  order.target_weight, "FILLED"))
        if shares < order.shares:
            deviations.append(TargetDeviation(date, order.symbol, order.target_weight,
                                              order.shares, shares, "PARTIAL_SELL"))

    # 再处理买入（受现金约束）
    for order in [o for o in orders if o.side == "BUY"]:
        bar = bars.get(order.symbol)
        reason = rules.can_buy(bar)
        if reason:
            final_orders.append(Order(date, order.symbol, order.side, order.shares,
                                      order.target_weight, "REJECTED", reason))
            deviations.append(TargetDeviation(date, order.symbol, order.target_weight,
                                              order.shares, 0, reason))
            continue
        # 用等效买入价计算可负担股数
        eff_px = costs.buy_price(bar.open)
        affordable = int(portfolio.cash // (eff_px * cfg.lot)) * cfg.lot if cfg.lot else int(portfolio.cash // eff_px)
        if not cfg.allow_fractional:
            affordable = max(0, affordable)
        shares = min(order.shares, affordable)
        if shares <= 0:
            final_orders.append(Order(date, order.symbol, order.side, order.shares,
                                      order.target_weight, "REJECTED", "INSUFFICIENT_CASH"))
            deviations.append(TargetDeviation(date, order.symbol, order.target_weight,
                                              order.shares, 0, "INSUFFICIENT_CASH"))
            continue
        bd = costs.breakdown(bar.open, shares, "BUY")
        fill = Fill(date, order.symbol, "BUY", shares, bd["price"],
                    bd["commission"], bd["stamp_duty"], bd["transfer_fee"], bd["slippage"])
        portfolio.apply_fill(fill)
        fills.append(fill)
        final_orders.append(Order(date, order.symbol, order.side, shares,
                                  order.target_weight, "FILLED"))
        if shares < order.shares:
            deviations.append(TargetDeviation(date, order.symbol, order.target_weight,
                                              order.shares, shares, "INSUFFICIENT_CASH"))

    return fills, final_orders, deviations
