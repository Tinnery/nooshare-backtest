"""组合账本：现金、持仓批次、成交落账、盯市与净值。

持仓成本采用移动加权平均（含买入费用），与 Strategy 基线一致。
T+1 按「买入批次」判断：每笔买入形成独立 lot，含 bought_date 与成本；
卖出只用已过 T+1 的批次，禁止用「最近一次买入日期」代表全部持仓
（那会错误锁住更早批次，或错误放行当天买入批次）。

账本只记录「实际成交」造成的资金/持仓变化；未成交的目标不进入账本，
只在 TargetDeviation 中体现。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from math import isfinite

from .models import Bar, EquityPoint, Fill, LedgerEntry, PositionSnapshot, PositionView


@dataclass
class _Lot:
    """一个买入批次：同一证券同一买入日的持仓块。"""
    shares: int
    cost_basis: float          # 该批次的含费用成本
    bought_date: int
    pure_price: float = 0.0    # 该批次不含费用的买入成交价


@dataclass
class _Position:
    shares: int
    cost_basis: float          # 持仓总成本（含买入费用）
    lots: list[_Lot] = field(default_factory=list)
    trade_id: str = ""         # 首笔 BUY Fill 时建立的 Trade ID

    @property
    def avg_cost(self) -> float:
        return self.cost_basis / self.shares if self.shares else 0.0

    def sellable_shares(self, date: int, enable_t_plus_1: bool) -> int:
        """T+1 可卖股数：按批次买入日判断，不用单一 bought_date。"""
        if not enable_t_plus_1:
            return self.shares
        return sum(lot.shares for lot in self.lots if lot.bought_date < date)

    def consume(self, shares: int) -> float:
        """按 FIFO 批次消耗持仓，返回被消耗批次的成本（移动加权口径由 cost_basis 决定）。

        批次只用于 T+1 判定与数量扣减；成本按移动加权平均扣减以与基线一致。
        """
        avg = self.avg_cost
        removed_cost = avg * shares
        self.cost_basis -= removed_cost
        self.shares -= shares
        left = shares
        for lot in self.lots:
            if left <= 0:
                break
            take = min(lot.shares, left)
            lot.shares -= take
            left -= take
        self.lots = [lot for lot in self.lots if lot.shares > 0]
        if self.shares <= 0:
            self.shares = 0
            self.cost_basis = 0.0
            self.lots = []
        return removed_cost


@dataclass
class Portfolio:
    cash: float
    positions: dict[str, _Position] = field(default_factory=dict)
    ledger: list[LedgerEntry] = field(default_factory=list)
    enable_t_plus_1: bool = True

    def apply_fill(self, fill: Fill) -> None:
        """把一笔成交落入现金与持仓批次，并写账本。"""
        self.cash += fill.net_cash
        if fill.side == "BUY":
            pos = self.positions.get(fill.symbol)
            if pos is None:
                pos = _Position(shares=0, cost_basis=0.0)
                self.positions[fill.symbol] = pos
            # 首笔 BUY Fill 时建立 Trade ID（后续加仓不改）
            if not pos.trade_id and fill.trade_id:
                pos.trade_id = fill.trade_id
            paid = -fill.net_cash           # 含费用
            pos.cost_basis += paid
            pos.shares += fill.shares
            # 每笔买入形成独立批次，保留真实买入日
            pos.lots.append(_Lot(shares=fill.shares, cost_basis=paid, bought_date=fill.date,
                                 pure_price=fill.price))
        else:  # SELL
            pos = self.positions[fill.symbol]
            pos.consume(fill.shares)
            if pos.shares == 0:
                del self.positions[fill.symbol]
        self.ledger.append(LedgerEntry(
            date=fill.date,
            kind=fill.side,
            symbol=fill.symbol,
            shares=fill.shares,
            cash_delta=round(fill.net_cash, 6),
            note=f"px={fill.price}",
        ))

    def sellable_shares(self, symbol: str, date: int) -> int:
        pos = self.positions.get(symbol)
        return pos.sellable_shares(date, self.enable_t_plus_1) if pos else 0

    def bought_today(self, symbol: str, date: int) -> bool:
        """兼容旧调用：当天是否有买入批次。新逻辑请用 sellable_shares。"""
        pos = self.positions.get(symbol)
        return bool(pos and any(lot.bought_date == date for lot in pos.lots))

    def shares_of(self, symbol: str) -> int:
        pos = self.positions.get(symbol)
        return pos.shares if pos else 0

    def position_views(self, date: int) -> list[PositionView]:
        """产出决策回调用的持仓视图（存活最早批次 + T+1 可卖数 + trade_id）。

        lots 由 FIFO 消费保持按买入日有序，首个批次即存活最早批次。
        """
        views: list[PositionView] = []
        for symbol, pos in sorted(self.positions.items()):
            first = pos.lots[0] if pos.lots else None
            views.append(PositionView(
                symbol=symbol,
                shares=pos.shares,
                avg_cost=pos.avg_cost,
                first_entry_price=first.pure_price if first else pos.avg_cost,
                first_entry_date=first.bought_date if first else date,
                sellable_shares=self.sellable_shares(symbol, date),
                trade_id=pos.trade_id,
            ))
        return views

    def mark(self, date: int, bars: dict[str, Bar], initial_cash: float) -> tuple[EquityPoint, list[PositionSnapshot]]:
        """按收盘价盯市，产出权益点与持仓快照。缺失行情按最近成本估值。"""
        market_value = 0.0
        snaps: list[PositionSnapshot] = []
        for symbol, pos in sorted(self.positions.items()):
            bar = bars.get(symbol)
            # V0.7（X5-B）：mark_price 为账户盯市估值价，上游必须传 Raw close；
            # 缺失时回退 Raw close；两者都缺按最近成本估值。
            # 禁止用 HFQ 价盯市——绝对 HFQ 价内含上市以来全部复权因子。
            px = None
            if bar is not None:
                # isfinite 防护：NaN 被 >0 判断天然排除，但 inf > 0 为 True 会污染账务
                if bar.mark_price is not None and bar.mark_price > 0 and isfinite(bar.mark_price):
                    px = bar.mark_price
                elif bar.close and bar.close > 0 and isfinite(bar.close):
                    px = bar.close
            if px is None:
                px = pos.avg_cost
            mv = px * pos.shares
            market_value += mv
            snaps.append(PositionSnapshot(
                date=date, symbol=symbol, shares=pos.shares,
                avg_cost=round(pos.avg_cost, 6), last_price=px,
                market_value=round(mv, 6),
            ))
        total = self.cash + market_value
        nav = total / initial_cash if initial_cash else 0.0
        point = EquityPoint(
            date=date,
            cash=round(self.cash, 6),
            market_value=round(market_value, 6),
            total_asset=round(total, 6),
            nav=round(nav, 6),
            cumulative_return=round(nav - 1.0, 6),
        )
        self.ledger.append(LedgerEntry(
            date=date, kind="MARK", cash_delta=0.0,
            note=f"total={point.total_asset} nav={point.nav}",
        ))
        return point, snaps
