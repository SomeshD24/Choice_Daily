import sqlite3
import json
import logging
from datetime import datetime
import pytz
import pandas as pd

import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config_daily import IST, POSITION_SIZE
from data_manager_daily import DailyTickerBuffer, build_basket_daily_ohlc
from indicators import IndicatorCache

def main():
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger("fix_b7")
    
    db_path = "state/daily_portfolio_state.db"
    if not os.path.exists(db_path):
        logger.error(f"Database not found at {db_path}")
        return

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    
    # 1. Fetch Basket 7
    slot_row = conn.execute("SELECT * FROM slots WHERE basket_id=7").fetchone()
    if not slot_row:
        logger.error("Basket 7 not found in slots.")
        return
        
    slot = dict(slot_row)
    tickers = json.loads(slot["tickers"])
    
    from state_store import _deserialize_buffer
    buffer_rows = conn.execute("SELECT ticker, records FROM ticker_buffers").fetchall()
    ticker_buffers = {}
    for r in buffer_rows:
        buf = DailyTickerBuffer(r["ticker"])
        df = _deserialize_buffer(json.loads(r["records"]))
        buf.seed(df)
        ticker_buffers[r["ticker"]] = buf
        
    # 3. Build Basket OHLC and compute fixed indicators
    ohlc = build_basket_daily_ohlc(ticker_buffers, tickers, POSITION_SIZE)
    cache = IndicatorCache(7)
    cache.update(ohlc["Close"])
    
    signals = cache.signals
    if signals is None:
        logger.error("Could not compute signals.")
        return
        
    # Find the last buy_signal BEFORE the current entry time
    entry_time_dt = pd.to_datetime(slot["entry_time"])
    if entry_time_dt.tzinfo is None:
        entry_time_dt = entry_time_dt.tz_localize(IST)
    else:
        entry_time_dt = entry_time_dt.tz_convert(IST)
    
    # Filter signals up to entry_time's date (so we find the signal that caused it)
    past_signals = signals[signals.index.date <= entry_time_dt.date()]
    buy_signals = past_signals[past_signals["buy_signal"] == True]
    
    if buy_signals.empty:
        logger.error("No buy signals found for Basket 7 before the current entry time.")
        return
        
    # The last buy signal is the true crossover date
    last_signal = buy_signals.iloc[-1]
    signal_date = last_signal.name
    logger.info(f"True unshifted signal date: {signal_date.date()}")
    
    # Execution happens on the next trading day
    idx_loc = ohlc.index.get_loc(signal_date)
    if idx_loc + 1 >= len(ohlc):
        logger.error("Signal happened on the last available day, no execution data.")
        return
        
    exec_date = ohlc.index[idx_loc + 1]
    logger.info(f"Correct execution date: {exec_date.date()}")
    
    # Now we need the OPEN price of each ticker on the execution date to recompute qty and prices
    new_entry_prices = {}
    for t in tickers:
        t_df = ticker_buffers[t].df
        if exec_date in t_df.index:
            new_entry_prices[t] = float(t_df.loc[exec_date, "Open"])
        else:
            logger.error(f"No execution data for {t} on {exec_date}")
            return
            
    # Calculate new quantities (equally weighted)
    slot_cap = float(slot["capital_allocated"])
    weight = 1.0 / len(tickers)
    alloc_per_ticker = slot_cap * weight
    
    new_qty = {}
    new_investment = 0.0
    for t in tickers:
        px = new_entry_prices[t]
        q = int(alloc_per_ticker // px)
        new_qty[t] = q
        new_investment += q * px
        
    logger.info(f"New Entry Prices: {new_entry_prices}")
    logger.info(f"New Quantities: {new_qty}")
    logger.info(f"New Investment: {new_investment}")
    
    # Update Database
    exec_time_str = exec_date.replace(hour=9, minute=17, second=0).isoformat()
    
    conn.execute('''
        UPDATE slots
        SET entry_time = ?,
            entry_prices = ?,
            quantities = ?,
            investment = ?,
            returns_ref_time = ?
        WHERE basket_id = 7
    ''', (
        exec_time_str,
        json.dumps(new_entry_prices),
        json.dumps(new_qty),
        new_investment,
        exec_time_str
    ))
    conn.commit()
    logger.info("Successfully updated Basket 7 entry details in daily_portfolio_state.db!")

if __name__ == "__main__":
    main()
