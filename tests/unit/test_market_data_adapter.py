from nooshare_backtest.adapters.market_data import normalize_bars
from nooshare_backtest.models import Bar


def test_normalize_bars_preserves_missing_raw_open_for_fail_closed_execution():
    """open=None 保留为 None（不抛 TypeError），执行层按缺失行情保守处理。

    V0.7 契约：limit_up_price/limit_down_price 为调用方按 PIT 规则算好的价格；
    规则来源由引擎内 LimitRuleResolver 记录，不在 Bar 上。"""
    bars = normalize_bars([
        {
            "date": 20240103,
            "symbol": "sh600519",
            "open": None,
            "high": 10.1,
            "low": 9.9,
            "close": 10.0,
            "mark_price": 5.0,
            "pre_close": 10.0,
            "limit_up_price": 11.0,
            "limit_down_price": 9.0,
        }
    ])

    bar = bars[20240103]["sh600519"]
    assert isinstance(bar, Bar)
    assert bar.open is None
    assert bar.mark_price == 5.0
    assert bar.pre_close == 10.0
    assert bar.limit_up_price == 11.0
    assert bar.limit_down_price == 9.0


def test_normalize_bars_mapping_accepts_bar_payload_with_nullable_ohlc():
    bars = normalize_bars({
        20240103: {
            "sh600519": {
                "date": 20240103,
                "symbol": "sh600519",
                "open": None,
                "high": None,
                "low": None,
                "close": None,
                "mark_price": None,
            }
        }
    })

    assert bars[20240103]["sh600519"].open is None
    assert bars[20240103]["sh600519"].close is None
