"""组合账本：现金、持仓、成交落账、盯市与净值。

持仓成本采用移动加权平均（含买入费用）。买入当日标记 bought_date 以支持 T+1。
账本只记录「实际成交」造成的资金/持仓变化；未成交的目标不进入账本，
只在 TargetDeviation 中体现。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .models import Bar, EquityPoint, Fill, LedgerEntry, PositionSnapshot


@dataclass
class _Position:
    shares: int
    cost_basis: float          # 持仓总成本（含买入费用）
    bought_date: int           # 最近一次买入日，用于 T+1

    @property
    def avg_cost(self) -> float:
        return self.cost_basis / self.shares if self.shares else 0.0


@dataclass
class Portfolio:
    cash: float
    positions: dict[str, _Position] = field(default_factory=dict)
    ledger: list[LedgerEntry] = field(default_factory=list)

    def apply_fill(self, fill: Fill) -> None:
        """把一笔成交落入现金与持仓，并写账本。"""
        self.cash += fill.net_cash
        if fill.side == "BUY":
            pos = self.positions.get(fill.symbol)
            if pos is None:
                pos = _Position(shares=0, cost_basis=0.0, bought_date=fill.date)
                self.positions[fill.symbol] = pos
            # 买入付现 = 毛额 + 费用 = -(net_cash)
            paid = -fill.net_cash
            pos.cost_basis += paid
            pos.shares += fill.shares
            pos.bought_date = fill.date
        else:  # SELL
            pos = self.positions[fill.symbol]
            avg = pos.avg_cost
            pos.cost_basis -= avg * fill.shares
            pos.shares -= fill.shares
            if pos.shares <= 0:
                pos.shares = 0
                pos.cost_basis = 0.0
                del self.positions[fill.symbol]
        self.ledger.append(LedgerEntry(
            date=fill.date,
            kind=fill.side,
            symbol=fill.symbol,
            shares=fill.shares,
            cash_delta=round(fill.net_cash, 6),
            note=f"px={fill.price}",
        ))

    def bought_today(self, symbol: str, date: int) -> bool:
        pos = self.positions.get(symbol)
        return bool(pos and pos.bought_date == date)

    def shares_of(self, symbol: str) -> int:
        pos = self.positions.get(symbol)
        return pos.shares if pos else 0

    def mark(self, date: int, bars: dict[str, Bar], initial_cash: float) -> tuple[EquityPoint, list[PositionSnapshot]]:
        """按收盘价盯市，产出权益点与持仓快照。缺失行情按最近成本估值。"""
        market_value = 0.0
        snaps: list[PositionSnapshot] = []
        for symbol, pos in sorted(self.positions.items()):
            bar = bars.get(symbol)
            px = bar.close if bar and bar.close > 0 else pos.avg_cost
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
