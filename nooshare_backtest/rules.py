"""A 股交易规则（按执行日适用，V0.4 Raw 开盘语义）。

核心变化（对照 design implementation-docs/nooshare-backtest/execution-model.md）：
- 判定基于执行日**开盘状态**：开盘涨停不可买、开盘跌停不可卖；
  收盘触板（is_limit_up/is_limit_down）仅参考信息，不再用于开盘订单判定；
- 移除 fake_gap_limit 假跳空阻断（合法市场波动不是不可成交；数据异常检测
  以后作为独立数据质量层实现，不混入交易规则）；
- ST 禁买为**策略股票池政策**（enable_st_filter），不是交易所申报限制；
- 停牌五态：NO_MARKET_DATA / LISTING_NOT_STARTED / SUSPENDED / ZERO_VOLUME，
  外加 T+1 与 NO_SELLABLE_POSITION 在账务层判定；
- LimitRuleResolver 按执行日解析涨跌幅规则（pit 表优先，derived 补充缺口）。

规则只输出「可否 + 原因」，不改动数据。原因字符串进入 Order.reason /
TargetDeviation.reason，保证未成交可解释。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .models import Bar

ALLOW = ""
R_NO_DATA = "NO_MARKET_DATA"
R_NOT_LISTED = "LISTING_NOT_STARTED"
R_SUSPENDED = "SUSPENDED"
R_ZERO_VOLUME = "ZERO_VOLUME"
R_OPEN_LIMIT_UP = "OPEN_LIMIT_UP"
R_OPEN_LIMIT_DOWN = "OPEN_LIMIT_DOWN"
R_ST = "ST"                 # 语义：策略股票池政策过滤（非交易所限制）
R_T_PLUS_1 = "T_PLUS_1"
R_NO_SELLABLE = "NO_SELLABLE_POSITION"

# 涨跌停价相等判定容差：价格按 2 位小数舍入，1e-4 覆盖浮点表示误差。
LIMIT_PRICE_EPS = 1e-4


@dataclass(frozen=True)
class LimitRule:
    """单次规则解析结果。ratio 为涨跌幅比例（0.10 = 10%）。

    source: 'pit_table'（来自 public.limit_rule_pit）或 'derived'
    （官方规则编码，覆盖 PIT 表缺口；Phase 0 真实样本手算验证）。
    """
    ratio: float
    no_limit: bool = False
    source: str = "derived"


@dataclass(frozen=True)
class LimitRuleResolver:
    """按执行日解析涨跌幅规则。

    pit_rules: {(exchange, security_type): (ratio, effective_from_int)}，
    由调用方从 public.limit_rule_pit 加载传入。Backtest 是纯计算库，不连库。

    security_type 由代码前缀显式推断（canonical_symbol 无 board 列，Phase 0
    记录在案；推断集中在本类单一实现，禁止散落）：
      exchange=='bj' -> bse；code 前缀 '68' -> star_market；
      code 前缀 '30' -> chi_next；其余 -> main_board。

    derived 规则（官方规则 + Phase 0 样本验证）：
      - 主板 ST（风险警示）5%（上交所 2026-07-06 起主板风险警示调整为 10%）；
      - BSE 30%（样本 bj920002 2025-02-26 收盘 188.50 = 145×1.30 手算验证）；
      - IPO 无涨跌幅限制期：主板上市首日、STAR/ChiNext 前 5 个交易日、
        BSE 上市首日（样本 600930 首日 +30.5%、601026 +19.8%）。
    """

    SSE_ST_MAIN_10PCT_DATE: int = 20260706   # 上交所主板风险警示 5% -> 10%
    IPO_NO_LIMIT_DAYS: dict = field(default_factory=lambda: {
        "main_board": 1, "star_market": 5, "chi_next": 5, "bse": 1,
    })
    pit_rules: dict = field(default_factory=dict)

    @staticmethod
    def security_type(exchange: str, code: str) -> str:
        exch = (exchange or "").lower()
        if exch == "bj":
            return "bse"
        c = code or ""
        if c.startswith("68"):
            return "star_market"
        if c.startswith("30"):
            return "chi_next"
        return "main_board"

    def resolve(
        self,
        exchange: str,
        code: str,
        is_st: bool,
        exec_date: int,
        trading_days_since_list: int | None,
    ) -> LimitRule:
        """按执行日返回适用的涨跌幅规则。"""
        stype = self.security_type(exchange, code)

        # 1) IPO 无涨跌幅限制期（上市后前 N 个交易日，按交易日历计数）
        if trading_days_since_list is not None:
            n = self.IPO_NO_LIMIT_DAYS.get(stype, 1)
            if trading_days_since_list <= n:
                return LimitRule(ratio=0.0, no_limit=True, source="derived")

        # 2) ST 状态：PIT 表当前无 ST 维度（Phase 0 记录在案），ST 规则走 derived。
        #    未来 limit_rule_pit 扩充 ST 维度后，这里改为 PIT 优先。
        if is_st:
            ratio, source = self._derive(exchange, stype, True, exec_date)
            return LimitRule(ratio=ratio, no_limit=False, source=source)

        # 3) PIT 表优先（按生效日过滤，取最晚生效且 <= exec_date 的规则）
        pit = self._pit_lookup(exchange.lower(), stype, exec_date)
        if pit is not None:
            ratio, source = pit, "pit_table"
        else:
            # 4) derived 补充 PIT 缺口
            ratio, source = self._derive(exchange, stype, is_st, exec_date)
        return LimitRule(ratio=ratio, no_limit=False, source=source)

    def _pit_lookup(
        self, exchange: str, stype: str, exec_date: int,
    ) -> float | None:
        best: tuple[int, float] | None = None
        for (ex, st), (ratio, eff_from) in self.pit_rules.items():
            if ex == exchange and st == stype and eff_from <= exec_date:
                if best is None or eff_from > best[0]:
                    best = (eff_from, ratio)
        return best[1] if best else None

    def _derive(
        self, exchange: str, stype: str, is_st: bool, exec_date: int,
    ) -> tuple[float, str]:
        if stype == "bse":
            return 0.30, "derived"
        if stype in ("star_market", "chi_next"):
            return 0.20, "derived"
        # 主板
        if is_st:
            if (exchange or "").lower() == "sh" and exec_date >= self.SSE_ST_MAIN_10PCT_DATE:
                # 上交所 2026-07-06 起主板风险警示调整为 10%；深交所暂无变更证据，
                # SZ 主板 ST 维持 5%。报告中披露该 derived 假设。
                return 0.10, "derived"
            return 0.05, "derived"
        return 0.10, "derived"


def limit_prices(
    pre_close: float | None,
    rule: LimitRule,
) -> tuple[float | None, float | None]:
    """由 Raw 前收盘价与规则计算涨跌停价（A 股按 2 位小数四舍五入）。"""
    if rule.no_limit or pre_close is None or pre_close <= 0:
        return None, None
    lu = round(pre_close * (1 + rule.ratio), 2)
    ld = round(pre_close * (1 - rule.ratio), 2)
    return lu, ld


@dataclass(frozen=True)
class RuleSet:
    """执行判定规则集。

    enable_st_filter：策略股票池政策——ST 股票是否允许买入由策略声明，
    不是交易所限制。卖出不受影响。
    enable_t_plus_1：A 股 T+1 规则开关（Fixture 测试可关闭）。
    limit_resolver：涨跌幅规则解析器（按执行日）。
    """
    enable_t_plus_1: bool = True
    enable_st_filter: bool = True
    limit_resolver: LimitRuleResolver = field(default_factory=LimitRuleResolver)

    def _no_trade_day(self, bar: Bar) -> str:
        """不可交易日判定：未上市 / 停牌 / 零成交 / 无行情。"""
        if bar is None:
            return R_NO_DATA
        if bar.list_date is not None and bar.date < bar.list_date:
            return R_NOT_LISTED
        if bar.is_suspended:
            return R_SUSPENDED
        if bar.volume is not None and bar.volume <= 0:
            return R_ZERO_VOLUME
        return ALLOW

    def _effective_limit(self, bar: Bar) -> tuple[float | None, float | None, bool]:
        """取得执行日有效涨跌停价。

        V0.5 修复（Phase C）：真实 Strategy→Engine 路径的 Bar 不含
        limit_up_price/limit_down_price（Strategy 不重复实现规则）。
        RuleSet 作为规则权威，在此用 LimitRuleResolver 按执行日计算：
          - Bar 已带限价（手工 Fixture / 特殊调用）优先使用；
          - 否则用 (exchange, canonical_id, is_st, date, listing day,
            raw pre_close) 现场解析，规则由 Resolver 单一实现。
        """
        no_limit = bar.no_limit
        lu, ld = bar.limit_up_price, bar.limit_down_price
        if (lu is None and ld is None) and not no_limit and bar.pre_close is not None:
            rule = self.limit_resolver.resolve(
                exchange=bar.exchange,
                code=bar.canonical_id or (bar.symbol[2:] if len(bar.symbol) > 2 else ""),
                is_st=bar.is_st,
                exec_date=bar.date,
                trading_days_since_list=bar.trading_days_since_list,
            )
            lu, ld = limit_prices(bar.pre_close, rule)
            no_limit = rule.no_limit
        return lu, ld, no_limit

    def _open_limit(self, bar: Bar, side: str) -> str:
        """开盘涨跌停判定（NEXT_OPEN 语义，不用收盘标记）。

        no_limit 日（IPO 初期等）跳过判定；
        limit price 未知（pre_close 缺失）时不拒绝，诚实留待数据修复。
        """
        lu, ld, no_limit = self._effective_limit(bar)
        if no_limit:
            return ALLOW
        if side == "BUY" and lu is not None:
            if bar.open >= lu - LIMIT_PRICE_EPS:
                return R_OPEN_LIMIT_UP
        if side == "SELL" and ld is not None:
            if bar.open <= ld + LIMIT_PRICE_EPS:
                return R_OPEN_LIMIT_DOWN
        return ALLOW

    def can_buy(self, bar: Bar | None) -> str:
        blocked = self._no_trade_day(bar)
        if blocked:
            return blocked
        return self._open_limit(bar, "BUY") or (R_ST if self.enable_st_filter and bar.is_st else ALLOW)

    def can_sell(
        self, bar: Bar | None, sellable_shares: int, total_shares: int,
    ) -> str:
        """卖出判定。sellable_shares/total_shares 由账务层（T+1 批次）给出。"""
        if total_shares <= 0:
            return R_NO_SELLABLE
        if self.enable_t_plus_1 and sellable_shares <= 0:
            return R_T_PLUS_1
        blocked = self._no_trade_day(bar)
        if blocked:
            return blocked
        return self._open_limit(bar, "SELL")
