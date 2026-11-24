"""回测领域数据结构。

目标仓位、订单、成交、实际持仓是不同对象，不可混用（见 Design: backtest-implementation）。
所有对象均为不可变数据类，结果可序列化、可复核。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ---------- 输入：市场数据 ----------

@dataclass(frozen=True)
class Bar:
    """单只证券单个交易日的行情。价格为后复权(hfq)口径。

    is_suspended / is_limit_up / is_limit_down / is_st 为当日交易状态，
    用于 A 股交易规则判定。ret 为当日后复权收益（可选）。
    """
    date: int                 # YYYYMMDD
    symbol: str
    open: float
    high: float
    low: float
    close: float
    volume: float | None = None   # None=未知，此时仅按 is_suspended 判定；0 视为停牌
    ret: float | None = None
    is_suspended: bool = False
    is_limit_up: bool = False
    is_limit_down: bool = False
    is_st: bool = False


# ---------- 输入：策略决策 ----------

@dataclass(frozen=True)
class TargetPortfolio:
    """策略在 decision_date 收盘后给出的目标组合。

    weights: symbol -> 目标权重（0..1，允许所有权重之和 <= 1，剩余为现金）。
    引擎在下一个交易日按规则执行，不假设 decision_date 当日成交。
    """
    decision_date: int
    weights: dict[str, float]
    version: str = "unknown"


# ---------- 订单与成交 ----------

@dataclass(frozen=True)
class Order:
    """由目标仓位推导的订单。status 取值见 ORDER_* 常量。"""
    date: int                 # 执行日（decision_date 的下一交易日）
    symbol: str
    side: str                 # BUY / SELL
    shares: int
    target_weight: float
    status: str = "PENDING"
    reason: str = ""          # 未成交/拒绝原因


@dataclass(frozen=True)
class Fill:
    """实际成交。价格与费用拆分记录，可逐笔复核。"""
    date: int
    symbol: str
    side: str
    shares: int
    price: float              # 含滑点的成交价
    commission: float
    stamp_duty: float
    transfer_fee: float
    slippage: float

    @property
    def total_cost(self) -> float:
        return self.commission + self.stamp_duty + self.transfer_fee + self.slippage

    @property
    def net_cash(self) -> float:
        """成交对现金的净影响（买为负，卖为正）。"""
        gross = self.price * self.shares
        fee = self.commission + self.stamp_duty + self.transfer_fee
        return -gross - fee if self.side == "BUY" else gross - fee


# ---------- 账本 ----------

@dataclass(frozen=True)
class LedgerEntry:
    """现金/持仓变化的一笔账本记录。"""
    date: int
    kind: str                 # CASH / BUY / SELL / MARK
    symbol: str = ""
    shares: int = 0
    cash_delta: float = 0.0
    note: str = ""


@dataclass(frozen=True)
class PositionSnapshot:
    """某交易日收盘的实际持仓。"""
    date: int
    symbol: str
    shares: int
    avg_cost: float           # 含买入成本的每股成本
    last_price: float
    market_value: float


@dataclass(frozen=True)
class EquityPoint:
    """某交易日收盘的组合权益。"""
    date: int
    cash: float
    market_value: float
    total_asset: float
    nav: float
    cumulative_return: float


@dataclass(frozen=True)
class TargetDeviation:
    """目标与实际差异——策略目标未被完全执行的可解释记录。"""
    date: int
    symbol: str
    target_weight: float
    target_shares: int
    filled_shares: int
    reason: str


# ---------- 结果 ----------

@dataclass(frozen=True)
class RunMetadata:
    run_id: str
    engine_version: str
    config: dict[str, Any]
    strategy_version: str = "unknown"
    data_version: str = "unknown"
    fill_policy: str = "T+1 next-open, daily-OHLC simulated"


@dataclass(frozen=True)
class CostSummary:
    commission: float = 0.0
    stamp_duty: float = 0.0
    transfer_fee: float = 0.0
    slippage: float = 0.0

    @property
    def total(self) -> float:
        return self.commission + self.stamp_duty + self.transfer_fee + self.slippage


@dataclass
class BacktestResult:
    """一次回测的完整结果。八类结果 + 目标/实际差异。

    equity_curve / positions / orders / fills / ledger / costs / metrics 分别对应
    Design 中 BacktestResult 领域模型的 Equity/Positions/Orders/Fills/Ledger/Costs/Metrics；
    metadata 为 Run Metadata；deviations 保留目标组合与实际组合差异。
    """
    metadata: RunMetadata
    equity_curve: list[EquityPoint] = field(default_factory=list)
    positions: list[PositionSnapshot] = field(default_factory=list)
    orders: list[Order] = field(default_factory=list)
    fills: list[Fill] = field(default_factory=list)
    ledger: list[LedgerEntry] = field(default_factory=list)
    costs: CostSummary = field(default_factory=CostSummary)
    metrics: dict[str, float] = field(default_factory=dict)
    deviations: list[TargetDeviation] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        from dataclasses import asdict
        return asdict(self)


def result_from_dict(data: dict[str, Any]) -> BacktestResult:
    """从 to_dict()/JSON 还原结果，供 CLI 的 show/report 与跨语言消费使用。"""
    md = data["metadata"]
    metadata = RunMetadata(
        run_id=md["run_id"], engine_version=md["engine_version"],
        config=md.get("config", {}), strategy_version=md.get("strategy_version", "unknown"),
        data_version=md.get("data_version", "unknown"),
        fill_policy=md.get("fill_policy", "T+1 next-open, daily-OHLC simulated"),
    )
    cost = data.get("costs", {})
    return BacktestResult(
        metadata=metadata,
        equity_curve=[EquityPoint(**x) for x in data.get("equity_curve", [])],
        positions=[PositionSnapshot(**x) for x in data.get("positions", [])],
        orders=[Order(**x) for x in data.get("orders", [])],
        fills=[Fill(**x) for x in data.get("fills", [])],
        ledger=[LedgerEntry(**x) for x in data.get("ledger", [])],
        costs=CostSummary(
            commission=cost.get("commission", 0.0),
            stamp_duty=cost.get("stamp_duty", 0.0),
            transfer_fee=cost.get("transfer_fee", 0.0),
            slippage=cost.get("slippage", 0.0),
        ),
        metrics=dict(data.get("metrics", {})),
        deviations=[TargetDeviation(**x) for x in data.get("deviations", [])],
    )
