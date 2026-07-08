import sqlite3
import pandas as pd
from datetime import datetime
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config_daily import IST, POSITION_SIZE
from run_daily import _load_basket_config, build_basket_info, BASKET_CSV_PATH, TARGET_BASKET_SIZE
from data_manager_daily import DailyTickerBuffer, build_basket_daily_ohlc
from indicators import IndicatorCache
import json

def check_yesterday_signals():
    print("Loading databases and checking signals for yesterday...")
    
    # Load config and basket info
    config = _load_basket_config(BASKET_CSV_PATH, TARGET_BASKET_SIZE)
    basket_info = build_basket_info(config)
    
    # We will use daily_portfolio_state to get the ticker buffers
    db_path = "state/daily_portfolio_state.db"
    if not os.path.exists(db_path):
        print(f"File not found: {db_path}")
        return
        
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    
    buffer_rows = conn.execute("SELECT ticker, records FROM ticker_buffers").fetchall()
    ticker_buffers = {}
    from state_store import _deserialize_buffer
    for r in buffer_rows:
        buf = DailyTickerBuffer(r["ticker"])
        df = _deserialize_buffer(json.loads(r["records"]))
        buf.seed(df)
        ticker_buffers[r["ticker"]] = buf
        
    signals_found = []
    
    for bid, info in basket_info.items():
        tickers = info["tickers"]
        ohlc = build_basket_daily_ohlc(ticker_buffers, tickers, POSITION_SIZE)
        if ohlc is None or ohlc.empty:
            continue
            
        cache = IndicatorCache(bid)
        ready = cache.update(ohlc["Close"])
        if not ready:
            continue
            
        # Get the latest signal (which is yesterday's completed daily bar, i.e., iloc[-1])
        # Because the daily script fetches bars up to yesterday 15:30.
        sig = cache.latest_signal()
        
        if sig and (sig.get("buy_signal") or sig.get("sell_signal")):
            signals_found.append((bid, sig))
            
    print("\n==============================")
    print("SIGNALS ON THE LATEST COMPLETED DAILY BAR (YESTERDAY)")
    print("==============================")
    if not signals_found:
        print("No signals found.")
    else:
        for bid, sig in signals_found:
            date_str = str(sig.get('bar_time', ''))
            print(f"Basket {bid} | Date: {date_str}")
            print(f"  Buy Signal : {sig.get('buy_signal')} ({sig.get('entry_type')})")
            print(f"  Sell Signal: {sig.get('sell_signal')} ({sig.get('exit_type')})")
            print("-" * 30)

if __name__ == "__main__":
    check_yesterday_signals()
