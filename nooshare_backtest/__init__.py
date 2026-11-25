"""nooshare-backtest: reproducible A-share daily backtest engine."""

__version__ = "0.3.0"

from .api import run_backtest
from .engine import DecisionContext, run
from .models import (
    SCHEMA_VERSION,
    BacktestResult,
    Fill,
    Order,
    PositionView,
    TargetPortfolio,
    Trade,
    TradeFillAllocation,
    result_from_dict,
)

__all__ = [
    "run_backtest", "run", "__version__", "SCHEMA_VERSION",
    "BacktestResult", "Order", "Fill", "Trade", "TradeFillAllocation",
    "TargetPortfolio", "PositionView", "DecisionContext", "result_from_dict",
]
