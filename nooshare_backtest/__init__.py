"""nooshare-backtest: reproducible A-share daily backtest engine."""

__version__ = "0.1.0"

from .api import run_backtest

__all__ = ["run_backtest", "__version__"]
