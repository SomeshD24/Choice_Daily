import sqlite3
import pandas as pd
import yfinance as yf
from datetime import datetime
import json

def restore_buffers():
    print("Fetching 5-minute historical data for active slots to rebuild buffers...")
    
    b7_tickers = ['PFC.NS', 'BRITANNIA.NS', 'KPITTECH.NS', 'ADANIGREEN.NS', 'LUPIN.NS', 'SUPREMEIND.NS']
    b7_qtys = {'PFC.NS': 1782, 'BRITANNIA.NS': 145, 'KPITTECH.NS': 1133, 'ADANIGREEN.NS': 698, 'LUPIN.NS': 361, 'SUPREMEIND.NS': 224}
    
    b9_tickers = ['FEDERALBNK.NS', 'DIVISLAB.NS', 'ETERNAL.NS', 'COLPAL.NS', 'ONGC.NS', 'KALYANKJIL.NS']
    b9_qtys = {"FEDERALBNK.NS": 2634, "DIVISLAB.NS": 126, "ETERNAL.NS": 2952, "COLPAL.NS": 420, "ONGC.NS": 3571, "KALYANKJIL.NS": 1929}
    
    all_tickers = list(set(b7_tickers + b9_tickers))
    
    # Fetch last 7 days of 5-min data (this covers plenty of bars for CORR_LOOKBACK)
    data = yf.download(all_tickers, period="7d", interval="5m")
    
    if data.empty or 'Close' not in data:
        print("Failed to fetch 5m data from yfinance.")
        return
        
    closes = data['Close']
    
    # Fill forward any NaNs within the day
    closes = closes.ffill()
    
    b7_series = []
    b9_series = []
    
    for ts, row in closes.iterrows():
        # yf timestamps are usually timezone aware. We format them to string.
        ts_str = ts.isoformat()
        
        # Calculate B7 close
        try:
            b7_close = sum(b7_qtys[t] * row[t] for t in b7_tickers if not pd.isna(row[t]))
            if b7_close > 0:
                b7_series.append((7, ts_str, b7_close))
        except KeyError:
            pass
            
        # Calculate B9 close
        try:
            b9_close = sum(b9_qtys[t] * row[t] for t in b9_tickers if not pd.isna(row[t]))
            if b9_close > 0:
                b9_series.append((9, ts_str, b9_close))
        except KeyError:
            pass
            
    if not b7_series and not b9_series:
        print("No valid series data generated.")
        return
        
    print(f"Generated {len(b7_series)} bars for B7 and {len(b9_series)} bars for B9.")
    
    db_path = "state/daily_portfolio_state.db"
    conn = sqlite3.connect(db_path)
    
    # Clear existing to avoid duplicates
    conn.execute("DELETE FROM basket_close_series")
    
    # Insert new data
    for item in b7_series + b9_series:
        conn.execute("INSERT OR REPLACE INTO basket_close_series (basket_id, timestamp, value) VALUES (?, ?, ?)", item)
        
    conn.commit()
    conn.close()
    
    print("Successfully restored basket_close_series into the SQLite database!")

if __name__ == "__main__":
    restore_buffers()
