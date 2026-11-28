"""测试 Fixture 工具：按 symbol 回填缺失的 pre_close。

V0.7 P0-4 后，涨跌停参考价缺失的订单被保守拒绝
（LIMIT_REFERENCE_UNKNOWN）。手工 Fixture 的执行日 bar 若未显式
提供 pre_close，需按前一交易日 close 回填，与真实数据路径
（panel groupby(symbol).shift(1)）语义一致。首日无前日，保持 None
——与真实路径一致（首日仅作决策日，不执行订单）。
"""
import dataclasses

from nooshare_backtest.models import Bar


def backfill_pre_close(bars: dict[int, dict[str, Bar]]) -> dict[int, dict[str, Bar]]:
    """按 symbol 回填缺失 pre_close = 前一交易日 close。

    - 显式指定 pre_close 或已预填限价的 bar 不动（测试者意图优先）；
    - no_limit 的 bar 不动（无涨跌停约束）；
    - bar 序列中缺某日行情时，prev_close 跨日保留（Fixture 简化；
      显式测试停牌恢复语义的 Fixture 应自行指定 pre_close）。
    """
    for sym in {s for day in bars.values() for s in day}:
        prev_close = None
        for d in sorted(bars):
            bar = bars[d].get(sym)
            if bar is None:
                continue
            if (bar.pre_close is None
                    and bar.limit_up_price is None
                    and bar.limit_down_price is None
                    and not bar.no_limit):
                bars[d][sym] = dataclasses.replace(bar, pre_close=prev_close)
            prev_close = bar.close
    return bars
