"""绩效指标实现契约。

年化口径与 nooshare-strategy 基线一致：244 交易日/年。
每个指标都在 docs/execution-model.md 中给出公式，便于复核与基线对照。
"""
from __future__ import annotations

from statistics import mean, stdev

from .models import EquityPoint, Fill

TRADING_DAYS_PER_YEAR = 244


def compute_metrics(
    points: list[EquityPoint], fills: list[Fill], initial_cash: float
) -> dict[str, float]:
    if not points:
        return {}

    navs = [p.nav for p in points]
    assets = [p.total_asset for p in points]
    returns = [navs[i] / navs[i - 1] - 1.0 for i in range(1, len(navs)) if navs[i - 1] > 0]

    final_nav = navs[-1]
    total_return = final_nav - 1.0
    years = len(points) / TRADING_DAYS_PER_YEAR
    annualized_return = (
        final_nav ** (1.0 / years) - 1.0 if final_nav > 0 and years > 0 else -1.0
    )

    peak = navs[0]
    max_drawdown = 0.0
    for nav in navs:
        peak = max(peak, nav)
        max_drawdown = min(max_drawdown, nav / peak - 1.0) if peak else max_drawdown

    volatility = stdev(returns) * (TRADING_DAYS_PER_YEAR ** 0.5) if len(returns) > 1 else 0.0
    sharpe = annualized_return / volatility if volatility else 0.0

    traded = sum(abs(f.price * f.shares) for f in fills)
    avg_asset = mean(assets) if assets else 0.0
    turnover = traded / avg_asset if avg_asset else 0.0
    annualized_turnover = turnover / years if years > 0 else 0.0

    return {
        "initial_cash": float(initial_cash),
        "final_nav": round(final_nav, 6),
        "total_return": round(total_return, 6),
        "annualized_return": round(annualized_return, 6),
        "max_drawdown": round(max_drawdown, 6),
        "annualized_volatility": round(volatility, 6),
        "sharpe_ratio": round(sharpe, 6),
        "turnover": round(turnover, 6),
        "annualized_turnover": round(annualized_turnover, 6),
        "trade_count": float(len(fills)),
        "trading_days": float(len(points)),
        "annualization_days": float(TRADING_DAYS_PER_YEAR),
    }


def drawdown_series(points: list[EquityPoint]) -> list[tuple[int, float]]:
    """返回 (date, drawdown) 序列，供静态报告画图。"""
    peak = 0.0
    out: list[tuple[int, float]] = []
    for p in points:
        peak = max(peak, p.nav)
        out.append((p.date, (p.nav / peak - 1.0) if peak else 0.0))
    return out
