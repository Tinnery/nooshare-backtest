"""ExitEvent identity tracking tests."""
import pytest
from datetime import date
from nooshare_backtest.engine import run_backtest
from nooshare_backtest.models import BarData, Signal, Decision


def make_bars(prices: list[float], volumes: list[int] | None = None) -> list[BarData]:
    """Helper to create bar data."""
    dates = list(range(20260101, 20260101 + len(prices)))
    if volumes is None:
        volumes = [1000000] * len(prices)
    return [
        BarData(
            date=d, symbol="000001", exchange="sz",
            open=p, high=p*1.02, low=p*0.98, close=p,
            volume=v, amount=v*p,
            canonical_id="000001",
        )
        for d, p, v in zip(dates, prices, volumes)
    ]


def test_sell_order_has_trade_id():
    """SELL Order should carry trade_id from the position."""
    bars = make_bars([10.0]*5 + [9.0]*5 + [10.5]*5)
    
    signals = [
        Signal(date=20260101, symbol="000001", exchange="sz", canonical_id="000001",
               signal_type="ENTRY", weight=0.1, decision_reason="test"),
    ]
    
    def strategy(ctx):
        if ctx.date == 20260101:
            return [Decision(symbol="000001", target_weight=0.1, decision_date=20260101, reason="entry")]
        if ctx.date == 20260106:
            return [Decision(symbol="000001", target_weight=0.0, decision_date=20260106, reason="exit")]
        return []
    
    result = run_backtest(
        bars=bars, strategy_fn=strategy, signals=signals,
        initial_cash=100000, start_date=20260101, end_date=20260115,
    )
    
    sell_orders = [o for o in result.orders if o.side == "SELL"]
    assert len(sell_orders) > 0, "Should have SELL orders"
    for order in sell_orders:
        assert order.trade_id != "", f"SELL Order should have trade_id, got empty"
        assert len(order.trade_id) == 36, f"trade_id should be UUID format"


def test_sell_fill_has_exit_event_id():
    """SELL Fill should carry exit_event_id from the order."""
    bars = make_bars([10.0]*5 + [9.0]*5 + [10.5]*5)
    
    signals = [
        Signal(date=20260101, symbol="000001", exchange="sz", canonical_id="000001",
               signal_type="ENTRY", weight=0.1, decision_reason="test"),
    ]
    
    def strategy(ctx):
        if ctx.date == 20260101:
            return [Decision(symbol="000001", target_weight=0.1, decision_date=20260101, reason="entry")]
        if ctx.date == 20260106:
            return [Decision(symbol="000001", target_weight=0.0, decision_date=20260106, reason="exit")]
        return []
    
    result = run_backtest(
        bars=bars, strategy_fn=strategy, signals=signals,
        initial_cash=100000, start_date=20260101, end_date=20260115,
    )
    
    sell_fills = [f for f in result.fills if f.side == "SELL"]
    assert len(sell_fills) > 0, "Should have SELL fills"
    for fill in sell_fills:
        assert fill.exit_event_id != "", f"SELL Fill should have exit_event_id"
        assert len(fill.exit_event_id) == 36, f"exit_event_id should be UUID format"


def test_backtest_result_has_exit_events():
    """BacktestResult should include exit_events list."""
    bars = make_bars([10.0]*5 + [9.0]*5 + [10.5]*5)
    
    signals = [
        Signal(date=20260101, symbol="000001", exchange="sz", canonical_id="000001",
               signal_type="ENTRY", weight=0.1, decision_reason="test"),
    ]
    
    def strategy(ctx):
        if ctx.date == 20260101:
            return [Decision(symbol="000001", target_weight=0.1, decision_date=20260101, reason="entry")]
        if ctx.date == 20260106:
            return [Decision(symbol="000001", target_weight=0.0, decision_date=20260106, reason="exit")]
        return []
    
    result = run_backtest(
        bars=bars, strategy_fn=strategy, signals=signals,
        initial_cash=100000, start_date=20260101, end_date=20260115,
    )
    
    assert hasattr(result, 'exit_events'), "BacktestResult should have exit_events"
    assert isinstance(result.exit_events, list), "exit_events should be a list"
    assert len(result.exit_events) > 0, "Should have at least one ExitEvent"


def test_exit_event_status_complete():
    """ExitEvent status should be 'complete' after full fill."""
    bars = make_bars([10.0]*5 + [9.0]*5 + [10.5]*5)
    
    signals = [
        Signal(date=20260101, symbol="000001", exchange="sz", canonical_id="000001",
               signal_type="ENTRY", weight=0.1, decision_reason="test"),
    ]
    
    def strategy(ctx):
        if ctx.date == 20260101:
            return [Decision(symbol="000001", target_weight=0.1, decision_date=20260101, reason="entry")]
        if ctx.date == 20260106:
            return [Decision(symbol="000001", target_weight=0.0, decision_date=20260106, reason="exit")]
        return []
    
    result = run_backtest(
        bars=bars, strategy_fn=strategy, signals=signals,
        initial_cash=100000, start_date=20260101, end_date=20260115,
    )
    
    completed = [e for e in result.exit_events if e.status == "complete"]
    assert len(completed) > 0, "Should have completed ExitEvents"


def test_exit_event_ids_are_uuids():
    """All exit_event_ids should be valid UUID format."""
    bars = make_bars([10.0]*5 + [9.0]*5 + [10.5]*5)
    
    signals = [
        Signal(date=20260101, symbol="000001", exchange="sz", canonical_id="000001",
               signal_type="ENTRY", weight=0.1, decision_reason="test"),
    ]
    
    def strategy(ctx):
        if ctx.date == 20260101:
            return [Decision(symbol="000001", target_weight=0.1, decision_date=20260101, reason="entry")]
        if ctx.date == 20260106:
            return [Decision(symbol="000001", target_weight=0.0, decision_date=20260106, reason="exit")]
        return []
    
    result = run_backtest(
        bars=bars, strategy_fn=strategy, signals=signals,
        initial_cash=100000, start_date=20260101, end_date=20260115,
    )
    
    for event in result.exit_events:
        assert event.exit_event_id != "", "exit_event_id should not be empty"
        assert len(event.exit_event_id) == 36, f"exit_event_id should be UUID format: {event.exit_event_id}"
        assert event.trade_id != "", "trade_id should not be empty"
        assert len(event.trade_id) == 36, f"trade_id should be UUID format: {event.trade_id}"
