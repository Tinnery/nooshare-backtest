"""Z-score 信号的交易模拟模块。

实现 T+1 买入 + OLHC 止损止盈的交易逻辑。
"""
import logging
import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

import pandas as pd

from .infrastructure import database as db

log = logging.getLogger(__name__)


@dataclass
class TradeConfig:
    """交易配置参数。"""
    trade_amount: float = 100_000.0  # 每笔交易金额
    stop_loss: float = 0.05  # 5% 止损
    take_profit: float = 0.10  # 10% 止盈
    max_hold_days: int = 20  # 最大持仓天数


def simulate_trades(
    run_id: str,
    signals: pd.DataFrame,
    bars_by_symbol_date: dict,
    config: TradeConfig,
) -> list[dict]:
    """模拟交易。

    对每个信号：
    1. T 日信号 → T+1 开盘买入
    2. T+2 起按 OLHC 检查止损止盈
    3. 记录交易结果

    Args:
        run_id: 实验运行 ID
        signals: 信号 DataFrame
        bars_by_symbol_date: {symbol: {date: Bar}} 行情数据
        config: 交易配置

    Returns:
        交易记录列表
    """
    trades = []

    for _, signal in signals.iterrows():
        symbol = signal["symbol"]
        signal_date = signal["signal_date"]

        # 1. 找到 T+1 交易日
        entry_date = get_next_trading_day(signal_date, bars_by_symbol_date.get(symbol, {}))
        if not entry_date:
            log.debug("No T+1 trading day for %s on %s", symbol, signal_date)
            continue

        # 2. 检查 T+1 是否可买入（停牌、涨停等）
        entry_bar = bars_by_symbol_date.get(symbol, {}).get(entry_date)
        if not entry_bar or not can_buy(entry_bar):
            log.debug("Cannot buy %s on %s (suspended or limit up)", symbol, entry_date)
            continue

        # 3. 按开盘价买入
        entry_price = entry_bar["open"]
        stop_price = entry_price * (1 - config.stop_loss)
        take_price = entry_price * (1 + config.take_profit)

        # 4. 从 T+2 开始检查退出
        exit_date, exit_price, exit_reason = check_exit(
            symbol, entry_date, stop_price, take_price,
            config.max_hold_days, bars_by_symbol_date
        )

        # 5. 计算收益
        if exit_price:
            gross_return = (exit_price - entry_price) / entry_price
            holding_days = (exit_date - entry_date).days if exit_date else None
        else:
            gross_return = None
            holding_days = None

        trades.append({
            "trade_id": str(uuid.uuid4()),
            "run_id": run_id,
            "signal_id": signal["signal_id"],
            "symbol": symbol,
            "entry_date": entry_date,
            "entry_price": entry_price,
            "stop_price": stop_price,
            "take_price": take_price,
            "exit_date": exit_date,
            "exit_price": exit_price,
            "exit_reason": exit_reason,
            "holding_days": holding_days,
            "gross_return": gross_return,
            "net_return": gross_return,  # 暂不扣除成本
            "status": "closed" if exit_price else "open",
        })

    log.info("Simulated %d trades for run %s", len(trades), run_id)
    return trades


def check_exit(
    symbol: str,
    entry_date: date,
    stop_price: float,
    take_price: float,
    max_hold_days: int,
    bars_by_symbol_date: dict,
) -> tuple[Optional[date], Optional[float], Optional[str]]:
    """OLHC 退出检查。

    从 T+2 开始，按 O→L→H→C 顺序检查：
    1. open <= stop_price → 按 open 退出（止损）
    2. open >= take_price → 按 open 退出（止盈）
    3. low <= stop_price → 按 stop_price 退出（止损）
    4. high >= take_price → 按 take_price 退出（止盈）
    5. 两者都触及 → 止损优先

    Args:
        symbol: 股票代码
        entry_date: 买入日期
        stop_price: 止损价格
        take_price: 止盈价格
        max_hold_days: 最大持仓天数
        bars_by_symbol_date: 行情数据

    Returns:
        (exit_date, exit_price, exit_reason)
    """
    bars = bars_by_symbol_date.get(symbol, {})
    
    # 从 T+2 开始
    start_date = entry_date + timedelta(days=1)
    current_date = get_next_trading_day(start_date, bars)
    
    holding_days = 0
    while current_date and holding_days < max_hold_days:
        bar = bars.get(current_date)
        if not bar:
            current_date = get_next_trading_day(current_date + timedelta(days=1), bars)
            holding_days += 1
            continue

        # 检查停牌
        if bar.get("is_suspended") or (bar.get("volume") is not None and bar["volume"] <= 0):
            current_date = get_next_trading_day(current_date + timedelta(days=1), bars)
            holding_days += 1
            continue

        # OLHC 检查
        # 1. 开盘止损
        if bar["open"] <= stop_price:
            return current_date, bar["open"], "STOP_LOSS"
        
        # 2. 开盘止盈
        if bar["open"] >= take_price:
            return current_date, bar["open"], "TAKE_PROFIT"
        
        # 3. 低点止损
        if bar["low"] <= stop_price:
            return current_date, stop_price, "STOP_LOSS"
        
        # 4. 高点止盈
        if bar["high"] >= take_price:
            return current_date, take_price, "TAKE_PROFIT"
        
        # 继续持有
        current_date = get_next_trading_day(current_date + timedelta(days=1), bars)
        holding_days += 1

    # 达到最大持仓天数
    if current_date:
        last_bar = bars.get(current_date)
        if last_bar:
            return current_date, last_bar["close"], "MAX_HOLD"
    
    return None, None, None


def can_buy(bar: dict) -> bool:
    """检查是否可以买入。

    Args:
        bar: 行情数据

    Returns:
        True if can buy
    """
    # 停牌不能买
    if bar.get("is_suspended"):
        return False
    
    # 成交量为 0 不能买
    if bar.get("volume") is not None and bar["volume"] <= 0:
        return False
    
    # 涨停不能买（买不进去）
    if bar.get("is_limit_up"):
        return False
    
    return True


def get_next_trading_day(from_date: date, bars: dict) -> Optional[date]:
    """获取下一个交易日。

    Args:
        from_date: 起始日期
        bars: 行情数据（用于判断是否有数据）

    Returns:
        下一个有数据的交易日，或 None
    """
    current = from_date
    max_days = 30  # 最多往后找 30 天
    
    for _ in range(max_days):
        current = current + timedelta(days=1)
        if current in bars:
            return current
    
    return None


def save_trades(run_id: str, trades: list[dict]) -> int:
    """保存交易记录到 strategy_lab.trade 表。

    Args:
        run_id: 实验运行 ID
        trades: 交易记录列表

    Returns:
        保存的交易数量
    """
    if not trades:
        return 0

    sql = """
    INSERT INTO strategy_lab.trade
        (trade_id, run_id, signal_id, symbol, entry_date, entry_price,
         stop_price, take_price, exit_date, exit_price, exit_reason,
         holding_days, gross_return, net_return, status)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """
    rows = [
        (
            t["trade_id"], t["run_id"], t["signal_id"], t["symbol"],
            t["entry_date"], t["entry_price"], t["stop_price"], t["take_price"],
            t["exit_date"], t["exit_price"], t["exit_reason"],
            t["holding_days"], t["gross_return"], t["net_return"], t["status"],
        )
        for t in trades
    ]

    with db.get_connection() as conn:
        with conn.cursor() as cur:
            from psycopg2.extras import execute_values
            execute_values(cur, sql, rows, page_size=1000)
        conn.commit()

    log.info("Saved %d trades for run %s", len(trades), run_id)
    return len(trades)


def load_trades(run_id: str) -> pd.DataFrame:
    """从数据库加载指定 run 的交易记录。

    Args:
        run_id: 实验运行 ID

    Returns:
        交易记录 DataFrame
    """
    sql = """
    SELECT trade_id, run_id, signal_id, symbol, entry_date, entry_price,
           stop_price, take_price, exit_date, exit_price, exit_reason,
           holding_days, gross_return, net_return, status
    FROM strategy_lab.trade
    WHERE run_id = %s
    ORDER BY entry_date, symbol
    """
    return db.read_sql(sql, (run_id,))
