from nooshare_backtest.adapters.market_data import normalize_bars
from nooshare_backtest.models import Bar


def test_normalize_bars_preserves_missing_raw_open_for_fail_closed_execution():
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
            "limit_up_ratio": 0.10,
            "limit_down_ratio": 0.10,
            "limit_rule_source": "pit_table",
        }
    ])

    bar = bars[20240103]["sh600519"]
    assert isinstance(bar, Bar)
    assert bar.open is None
    assert bar.mark_price == 5.0
    assert bar.pre_close == 10.0
    assert bar.limit_rule_source == "pit_table"


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


def test_null_or_blank_limit_rule_source_remains_unresolved():
    from nooshare_backtest.rules import R_UNRESOLVED_LIMIT_RULE, RuleSet

    for source in (None, "", "   "):
        bars = normalize_bars([
            {
                "date": 20240103,
                "symbol": "sh600519",
                "open": 10.0,
                "high": 10.1,
                "low": 9.9,
                "close": 10.0,
                "mark_price": 5.0,
                "pre_close": 10.0,
                "limit_up_ratio": 0.10,
                "limit_down_ratio": 0.10,
                "limit_rule_source": source,
            }
        ])
        bar = bars[20240103]["sh600519"]
        assert bar.limit_rule_source == ""
        assert RuleSet().can_buy(bar) == R_UNRESOLVED_LIMIT_RULE
