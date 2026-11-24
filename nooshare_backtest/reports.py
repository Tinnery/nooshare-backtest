"""静态 HTML 报告：纯标准库生成，Plotly 等依赖全部可选。

报告是结果的可视化视图，不参与回测计算。交互式工作台由 nooshare 产品层承载。
"""
from __future__ import annotations

import html
from pathlib import Path

from .metrics import drawdown_series
from .models import BacktestResult


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def _table(headers: list[str], rows: list[list[object]], limit: int | None = None) -> str:
    shown = rows if limit is None else rows[:limit]
    head = "".join(f"<th>{_esc(h)}</th>" for h in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{_esc(c)}</td>" for c in row) + "</tr>" for row in shown
    )
    suffix = ""
    if limit is not None and len(rows) > limit:
        suffix = f"<p class='note'>仅显示前 {limit} 行，共 {len(rows)} 行；完整数据见 result.json。</p>"
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>{suffix}"


def _chart_svg(result: BacktestResult) -> str:
    points = result.equity_curve
    if not points:
        return "<p class='note'>无净值数据。</p>"
    w, h_top, h_bottom, pad = 900, 250, 80, 46
    h = h_top + h_bottom + pad * 2
    navs = [p.nav for p in points]
    lo, hi = min(navs + [1.0]), max(navs + [1.0])
    span = hi - lo or 1.0
    n = len(points)

    def x(i: int) -> float:
        return pad + (w - 2 * pad) * (i / (n - 1)) if n > 1 else w / 2

    def y(nav: float) -> float:
        return pad + (h_top - pad) * (1.0 - (nav - lo) / span)

    poly = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(navs))
    dd = drawdown_series(points)
    dd_min = min((d for _, d in dd), default=0.0)
    dd_span = abs(dd_min) or 1.0
    dd_y0 = h_top + pad + 20
    dd_poly = " ".join(
        f"{x(i):.1f},{dd_y0 + (-d / dd_span) * (h_bottom - 20):.1f}"
        for i, (_, d) in enumerate(dd)
    )
    grid = "".join(
        f"<line x1='{pad}' y1='{y(v):.1f}' x2='{w - pad}' y2='{y(v):.1f}' class='grid'/>"
        f"<text x='{pad - 8}' y='{y(v) + 4:.1f}' class='axis' text-anchor='end'>{v:.3f}</text>"
        for v in (lo, 1.0, hi)
    )
    return (
        f"<svg viewBox='0 0 {w} {h}' role='img' aria-label='净值与回撤'>"
        f"{grid}<polyline points='{poly}' class='nav'/>"
        f"<line x1='{pad}' y1='{dd_y0}' x2='{w - pad}' y2='{dd_y0}' class='grid'/>"
        f"<polyline points='{dd_poly}' class='dd'/>"
        f"<text x='{pad}' y='{dd_y0 - 6}' class='axis'>回撤 0</text>"
        f"<text x='{pad}' y='{h - 6}' class='axis'>{points[0].date} — {points[-1].date}</text>"
        f"</svg>"
    )


def render_html(result: BacktestResult) -> str:
    md = result.metadata
    metric_rows = [[k, v] for k, v in sorted(result.metrics.items())]
    meta_rows = [
        ["run_id", md.run_id], ["engine_version", md.engine_version],
        ["strategy_version", md.strategy_version], ["data_version", md.data_version],
        ["fill_policy", md.fill_policy],
        ["config", ", ".join(f"{k}={v}" for k, v in sorted(md.config.items()))],
    ]
    order_rows = [[o.date, o.symbol, o.side, o.shares, o.status, o.reason] for o in result.orders]
    fill_rows = [
        [f.date, f.symbol, f.side, f.shares, f.price,
         f.commission, f.stamp_duty, f.transfer_fee, f.slippage]
        for f in result.fills
    ]
    cost_rows = [
        ["commission", result.costs.commission], ["stamp_duty", result.costs.stamp_duty],
        ["transfer_fee", result.costs.transfer_fee], ["slippage", result.costs.slippage],
        ["total", result.costs.total],
    ]
    dev_rows = [
        [d.date, d.symbol, d.target_weight, d.target_shares, d.filled_shares, d.reason]
        for d in result.deviations
    ]
    snap_by_date: dict[int, list] = {}
    for p in result.positions:
        snap_by_date.setdefault(p.date, []).append(p)
    last_date = max(snap_by_date) if snap_by_date else None
    pos_rows = [
        [p.symbol, p.shares, p.avg_cost, p.last_price, p.market_value]
        for p in snap_by_date.get(last_date, [])
    ]
    ledger_rows = [
        [e.date, e.kind, e.symbol, e.shares, e.cash_delta, e.note] for e in result.ledger
    ]

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"/>
<title>Backtest Report - {_esc(md.run_id)}</title>
<style>
body{{font-family:system-ui,-apple-system,Segoe UI,sans-serif;margin:24px;color:#1f2430;background:#fff}}
h1,h2{{color:#141a26}} h2{{margin-top:32px;border-bottom:1px solid #e3e7ee;padding-bottom:6px}}
table{{border-collapse:collapse;width:100%;font-size:13px;margin:8px 0 16px}}
th,td{{border:1px solid #e3e7ee;padding:6px 9px;text-align:left}}
th{{background:#f5f7fa}} .note{{color:#697386;font-size:12px}}
.warn{{background:#fff8e6;border:1px solid #f0d9a0;padding:10px 14px;border-radius:6px}}
svg{{width:100%;height:auto;background:#fbfcfe;border:1px solid #e3e7ee;border-radius:6px}}
.nav{{fill:none;stroke:#c0392b;stroke-width:2}} .dd{{fill:none;stroke:#2e7d5b;stroke-width:1.5}}
.grid{{stroke:#e3e7ee;stroke-width:1}} .axis{{font-size:11px;fill:#697386}}
</style></head><body>
<h1>Backtest Report · {_esc(md.run_id)}</h1>
<div class="warn"><strong>成交假设：</strong>{_esc(md.fill_policy)}。日线 OHLC 无法还原盘中成交队列与部分成交，本报告不表示真实逐笔撮合。</div>
<h2>绩效指标</h2>{_table(["metric", "value"], metric_rows)}
<h2>净值与回撤</h2>{_chart_svg(result)}
<h2>运行元数据</h2>{_table(["field", "value"], meta_rows)}
<h2>成本汇总</h2>{_table(["item", "amount"], cost_rows)}
<h2>最后一日持仓（{_esc(last_date or '-')}）</h2>{_table(["symbol", "shares", "avg_cost", "last_price", "market_value"], pos_rows)}
<h2>订单</h2>{_table(["date", "symbol", "side", "shares", "status", "reason"], order_rows)}
<h2>成交</h2>{_table(["date", "symbol", "side", "shares", "price", "commission", "stamp_duty", "transfer_fee", "slippage"], fill_rows)}
<h2>目标与实际差异</h2>{_table(["date", "symbol", "target_weight", "target_shares", "filled_shares", "reason"], dev_rows)}
<h2>账本</h2>{_table(["date", "kind", "symbol", "shares", "cash_delta", "note"], ledger_rows, limit=200)}
</body></html>
"""


def write_html_report(result: BacktestResult, path: str | Path) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_html(result), encoding="utf-8")
    return out
