"""Hand-calculated acceptance fixtures for Raw execution / HFQ mark separation."""
import pytest

from nooshare_backtest.engine import EngineConfig, run
from nooshare_backtest.models import Bar, TargetPortfolio
from nooshare_backtest.rules import RuleSet, R_NO_RAW_OPEN, R_NO_PRE_CLOSE, R_UNRESOLVED_LIMIT_RULE


def make_bar(date, *, raw_open=10.0, raw_close=10.0, hfq_mark=10.0,
             pre_close=10.0, up=0.10, down=0.10, no_limit=False,
             is_limit_up=False, is_limit_down=False, ret=0.0, is_st=False, limit_source="fixture_pit"):
    return Bar(
        date=date, symbol="sh600519",
        open=raw_open, high=raw_close, low=raw_close, close=raw_close,
        volume=100_000, ret=ret, mark_price=hfq_mark,
        pre_close=pre_close, limit_up_ratio=up, limit_down_ratio=down,
        no_limit=no_limit, limit_rule_source=limit_source,
        is_limit_up=is_limit_up, is_limit_down=is_limit_down, is_st=is_st,
        canonical_id="600519", exchange="sh",
    )


def test_engine_sizes_and_fills_from_raw_but_marks_from_hfq():
    d1, d2 = 20240102, 20240103
    bars = {
        d1: {"sh600519": make_bar(d1, raw_open=10, raw_close=10, hfq_mark=10)},
        # Corporate-action-style price divergence: raw is 10, HFQ mark is 5.
        d2: {"sh600519": make_bar(d2, raw_open=10, raw_close=10, hfq_mark=5)},
    }
    result = run(
        bars,
        {d1: TargetPortfolio(decision_date=d1, weights={"sh600519": 0.5})},
        EngineConfig(initial_cash=10_000, run_id="raw-hfq-fixture"),
    )

    order = next(o for o in result.orders if o.date == d2 and o.side == "BUY")
    fill = next(f for f in result.fills if f.date == d2 and f.side == "BUY")
    snapshot = next(p for p in result.positions if p.date == d2)

    # 50% of 10,000 / Raw open 10 = 500 shares, rounded to A-share board lots.
    assert order.requested_shares == 500
    assert fill.shares == 500
    assert fill.price == pytest.approx(10 * (1 + 0.0005))
    # Position and NAV use HFQ close 5, not Raw close 10 or Raw fill price.
    assert snapshot.last_price == 5
    assert snapshot.market_value == 2_500
    assert result.equity_curve[1].market_value == 2_500
    assert "Raw OHLC execution" in result.metadata.fill_policy
    assert "HFQ close mark-to-market" in result.metadata.fill_policy


def test_open_limit_rules_use_raw_open_and_raw_previous_close_not_close_flags():
    rules = RuleSet()
    # The close-state flag is true, but the normal Raw open was executable.
    normal_open = make_bar(20240103, raw_open=10, pre_close=10, is_limit_up=True)
    assert rules.can_buy(normal_open) == ""

    # At a 10% limit, Raw open at 11 rejects a buy even when the close flag is false.
    limit_up_open = make_bar(20240103, raw_open=11, pre_close=10, is_limit_up=False)
    assert rules.can_buy(limit_up_open) == "OPEN_LIMIT_UP"

    # Raw open at the 10% lower limit rejects a sell even when the close flag is false.
    limit_down_open = make_bar(20240103, raw_open=9, pre_close=10, is_limit_down=False)
    assert rules.can_sell(limit_down_open, bought_today=False) == "OPEN_LIMIT_DOWN"


def test_effective_dated_pit_ratio_is_respected_and_no_limit_session_is_allowed():
    rules = RuleSet(enable_st_filter=False)
    # Different effective-date rules are supplied as resolved PIT ratios by the data adapter.
    old_rule = make_bar(20260703, raw_open=10.7, pre_close=10, up=0.05, down=0.05, is_st=True)
    new_rule = make_bar(20260706, raw_open=10.7, pre_close=10, up=0.10, down=0.10, is_st=True)
    assert rules.can_buy(old_rule) == "OPEN_LIMIT_UP"
    assert rules.can_buy(new_rule) == ""

    ipo_no_limit = make_bar(20240103, raw_open=15, pre_close=None, up=None, down=None,
                            no_limit=True, ret=0.50)
    assert rules.can_buy(ipo_no_limit) == ""


def test_missing_raw_open_previous_close_or_limit_rule_is_explicit():
    rules = RuleSet()
    assert rules.can_buy(make_bar(20240103, raw_open=None)) == R_NO_RAW_OPEN
    assert rules.can_buy(make_bar(20240103, pre_close=None)) == R_NO_PRE_CLOSE
    assert rules.can_buy(make_bar(20240103, up=None)) == R_UNRESOLVED_LIMIT_RULE
    assert rules.can_buy(make_bar(20240103, limit_source="")) == R_UNRESOLVED_LIMIT_RULE


def test_missing_raw_open_is_recorded_as_skipped_order_not_a_fill():
    d1, d2 = 20240102, 20240103
    bars = {
        d1: {"sh600519": make_bar(d1)},
        d2: {"sh600519": make_bar(d2, raw_open=None)},
    }
    result = run(
        bars,
        {d1: TargetPortfolio(decision_date=d1, weights={"sh600519": 0.5})},
        EngineConfig(initial_cash=10_000, run_id="missing-raw-open"),
    )
    order = next(o for o in result.orders if o.date == d2 and o.side == "BUY")
    assert order.status == "SKIPPED"
    assert order.reason == R_NO_RAW_OPEN
    assert result.fills == []
    assert any(d.reason == R_NO_RAW_OPEN for d in result.deviations)
