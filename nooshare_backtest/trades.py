"""交易生命周期：从实际成交事件流构建 Trade 与 TradeFillAllocation。

定义（Design/Track B）：同一证券从无持仓到形成持仓，再到持仓完全归零的一段
连续持仓生命周期构成一个 Trade。多次买入、部分买入、多次卖出、部分卖出都属于
同一段 Trade；完全平仓后重新建仓产生新 Trade ID。

Trade 不是通过股票代码+日期启发式配对，而是由实际 Fill 事件产生。
成本分配使用移动加权平均（与 Portfolio 总账一致），每个 Fill 的归属通过
TradeFillAllocation 明确记录，ENTRY/EXIT 数量与成交可对账。
"""
from __future__ import annotations

from dataclasses import dataclass, replace

from .models import Fill, Trade, TradeFillAllocation, event_id


@dataclass
class _OpenTrade:
    trade_id: str
    symbol: str
    canonical_id: str
    exchange: str
    initial_signal_id: str
    opened_date: int
    shares: int = 0
    cost_basis: float = 0.0        # 移动加权成本（含买入费用）
    entry_qty: int = 0
    exit_qty: int = 0
    entry_notional: float = 0.0    # 买入成交价*数量
    exit_notional: float = 0.0
    costs: float = 0.0             # 该 Trade 全部成交费用（含滑点，展示口径）
    fee_costs: float = 0.0         # 佣金+印花税+过户费（不含滑点；gross_pnl 还原用）
    net_pnl: float = 0.0
    exit_reason: str = ""
    unresolved: bool = False


def build_trades(
    fills: list[Fill],
    trading_dates: list[int],
    run_id: str = "run",
    orders: list | None = None,
) -> tuple[list[Trade], list[TradeFillAllocation]]:
    """按时间顺序消费 Fill，构建完整 Trade 生命周期与分配记录。

    fills 必须已按 (date, 卖出先于买入) 的执行顺序排列——引擎保证该顺序。
    orders 可选：提供时从 BUY Order 提取 signal_id 写入 initial_signal_id，
    从 SELL Order 提取 decision_reason 写入 exit_reason。
    若 Fill 已携带 trade_id（引擎早期建立），则直接复用，不重新生成。
    """
    trades: list[Trade] = []
    allocations: list[TradeFillAllocation] = []
    open_trades: dict[str, _OpenTrade] = {}
    trade_seq = 0
    # 构建 order_id -> Order 映射，供 Fill 反查 signal_id / decision_reason
    order_map: dict[str, any] = {}
    if orders:
        for o in orders:
            if hasattr(o, "order_id") and o.order_id:
                order_map[o.order_id] = o

    for fill in fills:
        t = open_trades.get(fill.symbol)
        if fill.side == "BUY":
            if t is None:
                # 优先使用 Fill 已携带的 trade_id（引擎早期建立）
                if fill.trade_id:
                    tid = fill.trade_id
                else:
                    tid = event_id(run_id, "trade", fill.symbol, trade_seq)
                    trade_seq += 1
                # 从 BUY Order 提取 signal_id 作为 initial_signal_id
                init_sig = ""
                if fill.order_id and fill.order_id in order_map:
                    init_sig = order_map[fill.order_id].signal_id
                t = _OpenTrade(
                    trade_id=tid, symbol=fill.symbol,
                    canonical_id=fill.canonical_id, exchange=fill.exchange,
                    initial_signal_id=init_sig, opened_date=fill.date,
                )
                open_trades[fill.symbol] = t
            paid = -fill.net_cash
            t.cost_basis += paid
            t.shares += fill.shares
            t.entry_qty += fill.shares
            t.entry_notional += fill.price * fill.shares
            t.costs += fill.total_cost
            t.fee_costs += fill.commission + fill.stamp_duty + fill.transfer_fee
            allocations.append(TradeFillAllocation(t.trade_id, fill.fill_id, "ENTRY", fill.shares))
        else:  # SELL
            if t is None or t.shares <= 0:
                # 事件关联不完整：无法确认生命周期，记录 UNRESOLVED 而不是丢弃证据。
                if fill.trade_id:
                    tid = fill.trade_id
                else:
                    tid = event_id(run_id, "trade", fill.symbol, trade_seq)
                    trade_seq += 1
                t = _OpenTrade(
                    trade_id=tid, symbol=fill.symbol,
                    canonical_id=fill.canonical_id, exchange=fill.exchange,
                    initial_signal_id="", opened_date=fill.date,
                )
                open_trades[fill.symbol] = t
                t.unresolved = True
            consumed = min(fill.shares, t.shares)
            if consumed < fill.shares:
                t.unresolved = True  # 卖出超过持仓，数据/事件不完整
            if consumed > 0:
                avg = t.cost_basis / t.shares if t.shares else 0.0
                removed_cost = avg * consumed
                # net_cash 按比例分摊（fill 可能被拆分到不同 Trade 情况）
                prop = consumed / fill.shares
                t.net_pnl += fill.net_cash * prop - removed_cost
                t.cost_basis -= removed_cost
                t.shares -= consumed
                t.exit_qty += consumed
                t.exit_notional += fill.price * consumed
                t.costs += fill.total_cost * prop
                t.fee_costs += (fill.commission + fill.stamp_duty + fill.transfer_fee) * prop
                allocations.append(TradeFillAllocation(t.trade_id, fill.fill_id, "EXIT", consumed))
                # 从 SELL Order 提取 decision_reason（STOP_LOSS/TAKE_PROFIT/TARGET_WEIGHT）
                dec_reason = "TARGET_WEIGHT"
                if fill.order_id and fill.order_id in order_map:
                    dec_reason = order_map[fill.order_id].decision_reason or "TARGET_WEIGHT"
                t.exit_reason = dec_reason
            if t.shares == 0 and not t.unresolved:
                _finalize(t, trades, trading_dates, closed_date=fill.date)
                del open_trades[fill.symbol]

    # 回测结束仍有持仓 → OPEN，不伪造 Exit Fill，不强制平仓。
    for t in sorted(open_trades.values(), key=lambda x: (x.opened_date, x.symbol)):
        _finalize(t, trades, trading_dates, closed_date=None)

    return trades, allocations


def _finalize(t: _OpenTrade, out: list[Trade], trading_dates: list[int], closed_date: int | None) -> None:
    entry_cost = t.entry_notional  # 含滑点的买入成交额，作为收益率分母
    # gross_pnl = 价差口径（exit_notional - entry_notional）= net_pnl + 真实费用
    # （佣金+印花税+过户费）。滑点已含在成交价中，不得重复加回（V0.4 修复）。
    gross_pnl = t.net_pnl + t.fee_costs
    gross_return = gross_pnl / entry_cost if entry_cost else 0.0
    net_return = t.net_pnl / entry_cost if entry_cost else 0.0
    if closed_date is not None and closed_date in trading_dates and t.opened_date in trading_dates:
        holding = trading_dates.index(closed_date) - trading_dates.index(t.opened_date)
    elif closed_date is not None:
        holding = max(0, (closed_date - t.opened_date) // 1)  # 无日历时用自然日兜底并标注
    else:
        holding = 0
    status = "UNRESOLVED" if t.unresolved else ("CLOSED" if closed_date is not None else "OPEN")
    out.append(Trade(
        trade_id=t.trade_id, symbol=t.symbol,
        canonical_id=t.canonical_id, exchange=t.exchange,
        initial_signal_id=t.initial_signal_id,
        opened_date=t.opened_date, closed_date=closed_date,
        status=status,
        entry_qty=t.entry_qty, exit_qty=t.exit_qty, open_qty=t.shares,
        avg_entry_price=round(t.entry_notional / t.entry_qty, 6) if t.entry_qty else 0.0,
        avg_exit_price=round(t.exit_notional / t.exit_qty, 6) if t.exit_qty else 0.0,
        gross_pnl=round(gross_pnl, 6), net_pnl=round(t.net_pnl, 6),
        gross_return=round(gross_return, 6), net_return=round(net_return, 6),
        total_cost=round(t.costs, 6), holding_days=holding,
        exit_reason=t.exit_reason if t.exit_reason else ("OPEN" if closed_date is None else "UNKNOWN"),
    ))
