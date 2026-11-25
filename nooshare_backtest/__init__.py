"""nooshare-backtest: reproducible A-share daily backtest engine."""

__version__ = "0.2.0"

from .api import run_backtest
from .models import (
    SCHEMA_VERSION,
    BacktestResult,
    Fill,
    Order,
    TargetPortfolio,
    Trade,
    TradeFillAllocation,
    result_from_dict,
)

__all__ = [
    "run_backtest", "__version__", "SCHEMA_VERSION",
    "BacktestResult", "Order", "Fill", "Trade", "TradeFillAllocation",
    "TargetPortfolio", "result_from_dict",
]
