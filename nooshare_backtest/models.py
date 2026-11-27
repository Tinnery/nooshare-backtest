"""回测领域数据结构。

目标仓位、订单、成交、实际持仓、交易生命周期是不同对象，不可混用
（见 Design: backtest-implementation）。所有对象均为不可变数据类，
结果可序列化、可复核。

V0.2：加入稳定事件身份（Order/Fill/Trade）、TradeFillAllocation 与 Schema Version。
事件 ID 由 run_id + 确定性序号经 UUID5 生成：不依赖随机数/当前时间，
相同输入与 run_id 重复执行结果一致；不同 run 身份不冲突。
"""
from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import Any
import uuid

# 固定命名空间：仅用于把回测事件映射为稳定 UUID，不对外暴露为密钥。
EVENT_NAMESPACE = uuid.UUID("6f8d1b3a-2c47-5e91-9a04-7b3d6e2f18c5")

SCHEMA_VERSION = 2
LEGACY_SCHEMA_VERSION = 0  # 无事件身份的旧 JSON，不伪造历史关联

# 退出原因常量
EXIT_STOP_LOSS = "STOP_LOSS"
EXIT_TAKE_PROFIT = "TAKE_PROFIT"
EXIT_STRATEGY = "STRATEGY_EXIT"


def exit_event_id(
    run_id: str,
    trade_id: str,
    decision_date: int,
    reason: str,
    reference_price: float = 0.0,
    stop_price: float = 0.0,
    take_price: float = 0.0,
) -> str:
    """确定性生成 ExitEvent UUID（UUID5）。

    同一回测中同一 Trade、同一次退出触发，必须只有一个稳定的 ExitEvent ID。
    """
    key = ":".join([
        str(run_id), "exit", str(trade_id), str(decision_date),
        str(reason), str(reference_price), str(stop_price), str(take_price),
    ])
    return str(uuid.uuid5(EVENT_NAMESPACE, key))


def event_id(run_id: str, kind: str, *parts: Any) -> str:
    """确定性生成事件 UUID（UUID5，namespace + run_id + kind + 序号）。"""
    key = ":".join([str(run_id), kind, *[str(p) for p in parts]])
    return str(uuid.uuid5(EVENT_NAMESPACE, key))


def _coerce(cls, data: dict[str, Any], renames: dict[str, str] | None = None):
    """从 dict 还原 dataclass，兼容旧字段名；忽略无法识别的键而不静默丢关键关联。"""
    renames = renames or {}
    allowed = {f.name for f in fields(cls)}
    kwargs = {}
    for k, v in data.items():
        key = renames.get(k, k)
        if key in allowed:
            kwargs[key] = v
    return cls(**kwargs)


# ---------- 输入：市场数据 ----------

@dataclass(frozen=True)
class Bar:
    """单只证券单个交易日的双价格口径行情。

    open/high/low/close 始终为 Raw 可交易价格；mark_price 是 HFQ 收盘估值价，
    只用于 NAV / PositionSnapshot。pre_close 是同一证券上一交易日 Raw 收盘价。
    涨跌停比例必须由调用方按执行日 PIT 规则解析后提供；无法解析时引擎拒绝成交。
    is_limit_up / is_limit_down 仅保留为收盘状态信息，不参与 NEXT_OPEN 成交判定。
    """
    date: int                 # YYYYMMDD
    symbol: str
    open: float | None        # Raw open; execution only
    high: float | None        # Raw high; execution only / reporting
    low: float | None         # Raw low; execution only / reporting
    close: float | None       # Raw close; fallback mark only
    # Keep the original constructor field order through exchange so existing
    # positional Bar callers do not reinterpret old status flags as new prices.
    volume: float | None = None
    ret: float | None = None  # HFQ return; informational only
    is_suspended: bool = False
    is_limit_up: bool = False     # Close state only; never used for open execution
    is_limit_down: bool = False   # Close state only; never used for open execution
    is_st: bool = False
    canonical_id: str = ""
    exchange: str = ""
    # New dual-price/PIT fields are appended and should be passed by keyword.
    mark_price: float | None = None  # HFQ close; NAV mark only
    pre_close: float | None = None   # Exchange price-limit reference; corporate-action adjusted on ex-right dates
    limit_up_ratio: float | None = None
    limit_down_ratio: float | None = None
    no_limit: bool = False    # Effective-dated rule says no daily price limit
    limit_rule_source: str = ""  # e.g. pit_table / derived; blank means unknown


# ---------- 输入：策略决策 ----------

@dataclass(frozen=True)
class TargetPortfolio:
    """策略在 decision_date 收盘后给出的目标组合。

    weights: symbol -> 目标权重（0..1，允许所有权重之和 <= 1，剩余为现金）。
    hold: 明确保持现状的持仓集合——不生成任何订单（不调仓、不清仓），
    与「未列出 = 目标权重 0 = 卖出」的默认语义区分，供路径依赖规则表达
    「继续持有」而不产生开盘价与收盘价差异导致的噪声调仓。
    signal_ids: symbol -> 原始 signal_id（可选）。无法唯一关联时留空，
    引擎不猜测信号。引擎在下一交易日按规则执行，不假设 decision_date 当日成交。
    exit_signals: symbol -> (reason, reference_price, stop_price, take_price)。
    策略检测到止盈/止损触发时填写，引擎据此生成带 exit_event_id 的 SELL 订单，
    并在未完全成交时持续重试直到平仓或回测结束。
    """
    decision_date: int
    weights: dict[str, float]
    version: str = "unknown"
    signal_ids: dict[str, str] = field(default_factory=dict)
    hold: frozenset[str] = frozenset()
    exit_signals: dict[str, tuple[str, float, float, float]] = field(default_factory=dict)


# ---------- 订单与成交 ----------

@dataclass(frozen=True)
class Order:
    """由目标仓位推导的订单。status 取值见 ORDER_* 常量。

    requested_shares 为意图，filled_shares 为实际成交；二者独立查询。
    部分成交必须标记 PARTIALLY_FILLED，不得标记 FILLED。
    """
    date: int                 # 执行日（decision_date 的下一交易日）
    symbol: str
    side: str                 # BUY / SELL
    requested_shares: int
    target_weight: float
    status: str = "PENDING"
    reason: str = ""
    order_id: str = ""
    signal_id: str = ""
    decision_date: int = 0
    canonical_id: str = ""
    exchange: str = ""
    filled_shares: int = 0
    exit_event_id: str = ""
    trade_id: str = ""
    decision_reason: str = ""

    @property
    def shares(self) -> int:
        """兼容旧调用：shares 指请求的股数。实际成交请用 filled_shares。"""
        return self.requested_shares

    @property
    def fully_filled(self) -> bool:
        return self.filled_shares == self.requested_shares and self.filled_shares > 0


@dataclass(frozen=True)
class Fill:
    """实际成交。价格与费用拆分记录，可逐笔复核。

    每条 Fill 必须归属唯一 order_id；没有实际成交不创建 Fill。
    """
    date: int
    symbol: str
    side: str
    shares: int
    price: float              # 含滑点的成交价
    commission: float
    stamp_duty: float
    transfer_fee: float
    slippage: float
    fill_id: str = ""
    order_id: str = ""
    trade_id: str = ""
    exit_event_id: str = ""
    canonical_id: str = ""
    exchange: str = ""

    @property
    def total_cost(self) -> float:
        return self.commission + self.stamp_duty + self.transfer_fee + self.slippage

    @property
    def net_cash(self) -> float:
        """成交对现金的净影响（买为负，卖为正）。"""
        gross = self.price * self.shares
        fee = self.commission + self.stamp_duty + self.transfer_fee
        return -gross - fee if self.side == "BUY" else gross - fee


# ---------- 交易生命周期 ----------

@dataclass(frozen=True)
class TradeFillAllocation:
    """一笔成交如何进入/退出一段持仓生命周期。

    ENTRY：买入成交分配；EXIT：卖出成交分配。
    一个 Fill 的分配数量不得超过其实际成交数量。
    """
    trade_id: str
    fill_id: str
    allocation_role: str      # ENTRY / EXIT
    allocated_qty: int


@dataclass(frozen=True)
class Trade:
    """同一证券从无持仓到形成持仓，再到持仓完全归零的一段连续生命周期。

    多次买入、多次卖出、部分成交都属于同一段 Trade；完全平仓后重新建仓产生新 Trade。
    OPEN：回测结束仍有剩余持仓；UNRESOLVED：事件关联不完整，无法确认结果。
    不得把 OPEN 伪装成 CLOSED，不得为使报表好看强制平仓。
    """
    trade_id: str
    symbol: str
    canonical_id: str = ""
    exchange: str = ""
    initial_signal_id: str = ""
    opened_date: int = 0
    closed_date: int | None = None
    status: str = "OPEN"      # OPEN / CLOSED / UNRESOLVED
    entry_qty: int = 0
    exit_qty: int = 0
    open_qty: int = 0
    avg_entry_price: float = 0.0
    avg_exit_price: float = 0.0
    gross_pnl: float = 0.0
    net_pnl: float = 0.0
    gross_return: float = 0.0
    net_return: float = 0.0
    total_cost: float = 0.0
    holding_days: int = 0
    exit_reason: str = "OPEN"



@dataclass(frozen=True)
class ExitEvent:
    """退出事件：记录一次退出意图的完整生命周期。

    从 pending 开始，成交后转为 complete（或部分成交后 partial），
    回测结束仍未完全平仓则标记为 abandoned。
    """
    exit_event_id: str
    trade_id: str
    decision_date: int        # YYYYMMDD 格式
    reason: str               # STOP_LOSS / TAKE_PROFIT / TARGET_WEIGHT / MANUAL
    reference_price: float
    stop_price: float
    take_price: float
    status: str = "pending"   # pending / partial / complete / abandoned


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
    """某交易日收盘持仓快照。avg_cost / Trade PnL 为 Raw 成交成本；last_price 优先为 HFQ mark。"""
    date: int
    symbol: str
    shares: int
    avg_cost: float           # Raw 成交口径，含买入成本
    last_price: float         # HFQ mark；缺失时按契约回退 Raw close / avg_cost
    market_value: float       # last_price × shares，研究估值口径


@dataclass(frozen=True)
class PositionView:
    """决策回调用的持仓视图。

    first_entry_price / first_entry_date 取自当前存活批次中最早批次的
    不含费用买入价与买入日，供路径依赖退出规则（止盈止损）以建仓价为基准。
    分批加仓不改基准；建仓批次全部卖出后以剩余最早批次为准。
    trade_id 在首笔 BUY Fill 时建立，供策略回调直接引用。
    """
    symbol: str
    shares: int
    avg_cost: float           # 含费用移动加权成本
    first_entry_price: float  # 存活最早批次不含费用买入价
    first_entry_date: int     # 该批次买入日（YYYYMMDD）
    sellable_shares: int      # T+1 规则下的当日可卖股数
    trade_id: str = ""        # 首笔 BUY Fill 时建立的 Trade ID


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
    """一次回测的完整结果。

    Schema Version 2 在八类结果之上新增 Trades 与 Trade-Fill Allocations，
    并给 Order/Fill 稳定身份。metadata 为 Run Metadata；
    deviations 保留目标组合与实际组合差异。
    """
    metadata: RunMetadata
    equity_curve: list[EquityPoint] = field(default_factory=list)
    positions: list[PositionSnapshot] = field(default_factory=list)
    orders: list[Order] = field(default_factory=list)
    fills: list[Fill] = field(default_factory=list)
    trades: list[Trade] = field(default_factory=list)
    trade_fill_allocations: list[TradeFillAllocation] = field(default_factory=list)
    ledger: list[LedgerEntry] = field(default_factory=list)
    costs: CostSummary = field(default_factory=CostSummary)
    metrics: dict[str, float] = field(default_factory=dict)
    deviations: list[TargetDeviation] = field(default_factory=list)
    exit_events: list = field(default_factory=list)
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        from dataclasses import asdict
        return asdict(self)


def result_from_dict(data: dict[str, Any]) -> BacktestResult:
    """从 to_dict()/JSON 还原结果，供 CLI 与跨语言消费使用。

    未知 Schema Version 明确报错；旧版（无版本、无事件身份）标记为版本 0，
    不伪造 Order/Fill/Trade 的历史关联。
    """
    version = int(data.get("schema_version", LEGACY_SCHEMA_VERSION))
    if version not in (LEGACY_SCHEMA_VERSION, SCHEMA_VERSION):
        raise ValueError(
            f"unsupported BacktestResult schema_version: {version}; supported: "
            f"{LEGACY_SCHEMA_VERSION}, {SCHEMA_VERSION}"
        )
    md = data["metadata"]
    metadata = _coerce(RunMetadata, md)
    cost = data.get("costs", {})
    return BacktestResult(
        metadata=metadata,
        equity_curve=[_coerce(EquityPoint, x) for x in data.get("equity_curve", [])],
        positions=[_coerce(PositionSnapshot, x) for x in data.get("positions", [])],
        orders=[_coerce(Order, x, {"shares": "requested_shares"}) for x in data.get("orders", [])],
        fills=[_coerce(Fill, x) for x in data.get("fills", [])],
        trades=[_coerce(Trade, x) for x in data.get("trades", [])],
        trade_fill_allocations=[
            _coerce(TradeFillAllocation, x) for x in data.get("trade_fill_allocations", [])
        ],
        ledger=[_coerce(LedgerEntry, x) for x in data.get("ledger", [])],
        costs=CostSummary(
            commission=cost.get("commission", 0.0),
            stamp_duty=cost.get("stamp_duty", 0.0),
            transfer_fee=cost.get("transfer_fee", 0.0),
            slippage=cost.get("slippage", 0.0),
        ),
        metrics=dict(data.get("metrics", {})),
        deviations=[_coerce(TargetDeviation, x) for x in data.get("deviations", [])],
        schema_version=version,
    )
