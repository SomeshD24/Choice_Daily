import sqlite3
import pandas as pd
import json
from datetime import datetime
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from state_store import _deserialize_buffer
from data_manager_daily import DailyTickerBuffer
from portfolio_engine import _pairwise_correlation, _eviction_target

def test_eviction():
    db_path = "state/portfolio_state.db"
    if not os.path.exists(db_path):
        print(f"File not found: {db_path}")
        return
        
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    
    # Load ticker buffers to build basket close series if they were saved,
    # OR we can just load basket_close_series directly from DB!
    print("Loading basket_close_series from DB...")
    cursor = conn.cursor()
    cursor.execute("SELECT basket_id, timestamp, value FROM basket_close_series")
    rows = cursor.fetchall()
    
    basket_close_series = {}
    for r in rows:
        bid, ts_str, val = r
        if bid not in basket_close_series:
            basket_close_series[bid] = pd.Series(dtype=float)
        
        # parse timestamp
        ts = pd.Timestamp(ts_str)
        if ts.tzinfo is None:
            ts = ts.tz_localize('Asia/Kolkata')
        basket_close_series[bid].at[ts] = float(val)
        
    for bid in basket_close_series:
        basket_close_series[bid].sort_index(inplace=True)
        
    # We want to test eviction for held_ids = [6, 7] and incoming = 12
    # At what time? The time Basket 12 was evaluated. Let's use the most recent time in the series.
    all_times = set()
    for s in basket_close_series.values():
        for t in s.index:
            all_times.add(t)
            
    now = max(all_times) if all_times else pd.Timestamp.now(tz='Asia/Kolkata')
    print(f"Evaluating eviction at time: {now}")
    
    members = [6, 7, 12]
    print(f"\nMembers: {members}")
    
    # Check pairwise correlations
    from itertools import combinations
    corr = {}
    for a, b in combinations(members, 2):
        try:
            c = _pairwise_correlation(a, b, basket_close_series, now)
            corr[(a, b)] = c
            corr[(b, a)] = c
            print(f"Corr(B{a}, B{b}) = {c:.4f}")
        except Exception as e:
            print(f"Error calculating corr for B{a}, B{b}: {e}")
            return
            
    # Calculate mean correlations
    for i, m in enumerate(members):
        mean_c = sum(corr[(m, other)] for other in members if other != m) / (len(members) - 1)
        print(f"Mean Corr for B{m}: {mean_c:.4f}")
        
    worst_idx = _eviction_target([6, 7], 12, basket_close_series, now)
    
    if worst_idx is None:
        print("\n=> Result: The incoming basket (B12) is the most redundant. It should be REJECTED.")
        print("=> B6 and B7 should NOT be evicted.")
    else:
        evicted_bid = [6, 7][worst_idx]
        print(f"\n=> Result: B{evicted_bid} is the most redundant and should be EVICTED.")
        
if __name__ == "__main__":
    test_eviction()
