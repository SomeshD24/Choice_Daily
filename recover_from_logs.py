import os
import re
import sqlite3
import json
import pandas as pd
import yfinance as yf
from datetime import datetime

# Script to reconstruct the Choice_Daily portfolio state entirely from text logs.
# This avoids importing any local strategy files that might not be on the cloud server.

def recover_from_logs():
    log_file = "state/run_daily.log"
    db_file = "state/daily_portfolio_state.db"
    
    if not os.path.exists(log_file):
        print(f"Error: {log_file} not found!")
        return

    print(f"Parsing {log_file}...")
    
    entries = []
    evictions = []
    
    with open(log_file, "r") as f:
        for line in f:
            # Example: 15:36:33 [INFO] signal_engine_daily:   SIGNAL ENTRY B6 (ema_crossover) at 2026-07-01
            m = re.search(r"SIGNAL ENTRY B(\d+) \(([^)]+)\) at ([\d\-]+)", line)
            if m:
                entries.append({
                    "basket_id": int(m.group(1)),
                    "entry_type": m.group(2),
                    "date": m.group(3)
                })
                
            # Example: 15:36:33 [INFO] portfolio: Evicting worst performer B19
            m_evict = re.search(r"Evicting worst performer B(\d+)", line)
            if m_evict:
                # We record the eviction. In reality it executes the next morning.
                # We just need to know it happened.
                evictions.append(int(m_evict.group(1)))

    print(f"Found {len(entries)} entry signals in the logs.")
    for e in entries:
        print(f" - Entry B{e['basket_id']} on {e['date']}")
        
    print(f"Found {len(evictions)} evictions in the logs.")
    for ev in evictions:
        print(f" - Evicted B{ev}")
        
    print("\n--- Next Steps ---")
    print("Since we successfully parsed the log, we can now fetch the historical")
    print("OHLC data for these exact dates using yfinance to rebuild the SQLite database.")
    print("If the above logs look CORRECT, please let me know, and I will execute the final database rebuild.")

if __name__ == "__main__":
    recover_from_logs()
