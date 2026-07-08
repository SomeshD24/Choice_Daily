import sqlite3
import pandas as pd
import json
from datetime import datetime
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from portfolio_engine import _pairwise_correlation, _eviction_target

def get_basket_close_series(conn):
    cursor = conn.cursor()
    cursor.execute("SELECT basket_id, timestamp, value FROM basket_close_series")
    rows = cursor.fetchall()
    
    basket_close_series = {}
    for r in rows:
        bid, ts_str, val = r
        if bid not in basket_close_series:
            basket_close_series[bid] = pd.Series(dtype=float)
        
        ts = pd.Timestamp(ts_str)
        if ts.tzinfo is None:
            ts = ts.tz_localize('Asia/Kolkata')
        basket_close_series[bid].at[ts] = float(val)
        
    for bid in basket_close_series:
        basket_close_series[bid].sort_index(inplace=True)
    return basket_close_series

def restore_basket6():
    db_path = "state/portfolio_state.db"
    if not os.path.exists(db_path):
        print(f"File not found: {db_path}")
        return
        
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    
    basket_close_series = get_basket_close_series(conn)
    
    all_times = set()
    for s in basket_close_series.values():
        for t in s.index:
            all_times.add(t)
            
    now = max(all_times) if all_times else pd.Timestamp.now(tz='Asia/Kolkata')
    
    print("\n======================================")
    print("CORRELATION CHECK (Baskets 6, 7, 12)")
    print("======================================")
    members = [6, 7, 12]
    from itertools import combinations
    corr = {}
    for a, b in combinations(members, 2):
        if a in basket_close_series and b in basket_close_series:
            c = _pairwise_correlation(a, b, basket_close_series, now)
            corr[(a, b)] = c
            corr[(b, a)] = c
            print(f"Corr(B{a}, B{b}) = {c:.4f}")
            
    if corr:
        for m in members:
            mean_c = sum(corr[(m, other)] for other in members if other != m) / 2
            print(f"Mean Corr for B{m}: {mean_c:.4f}")
            
        worst_idx = _eviction_target([6, 7], 12, basket_close_series, now)
        if worst_idx is None:
            print("\n=> Math says: B12 is the most redundant (should be rejected).")
        else:
            evicted_bid = [6, 7][worst_idx]
            print(f"\n=> Math says: B{evicted_bid} is the most redundant (should be evicted).")
    
    print("\n======================================")
    print("RESTORING BASKET 6 TO ACTIVE SLOTS")
    print("======================================")
    
    # Check if Basket 6 is already in slots
    slots = conn.execute("SELECT slot_idx, basket_id FROM slots").fetchall()
    active_bids = [s['basket_id'] for s in slots]
    if 6 in active_bids:
        print("Basket 6 is ALREADY in active slots! No restoration needed.")
        return
        
    # Find empty slot
    occupied_slots = [s['slot_idx'] for s in slots]
    empty_slot = None
    for i in range(2): # 2 slots total
        if i not in occupied_slots:
            empty_slot = i
            break
            
    if empty_slot is None:
        print("Error: No empty slots available to restore Basket 6. (Are slots full?)")
        return
        
    # Find Basket 6 in trade_log
    trade = conn.execute("SELECT * FROM trade_log WHERE basket_id=6 ORDER BY trade_id DESC LIMIT 1").fetchone()
    if not trade:
        print("Error: Basket 6 not found in trade_log! Cannot restore.")
        return
        
    print(f"Found Basket 6 in trade_log (Exited at: {trade['exit_time']}). Restoring to Slot {empty_slot}...")
    
    tickers = list(json.loads(trade['quantities']).keys())
    
    conn.execute("""
        INSERT INTO slots (
            slot_idx, basket_id, entry_time, entry_type, tickers, 
            quantities, entry_prices, investment, capital_allocated, 
            entry_basket_close, peak_basket_close, returns_ref_value, returns_ref_time
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        empty_slot,
        6,
        trade['entry_time'],
        trade['entry_type'],
        json.dumps(tickers),
        trade['quantities'],
        trade['entry_prices'],
        trade['investment'],
        trade['investment'], # capital_allocated
        trade['investment'], # entry_basket_close
        None, # peak_basket_close
        trade['investment'], # returns_ref_value
        trade['entry_time'] # returns_ref_time
    ))
    
    conn.execute("DELETE FROM trade_log WHERE trade_id=?", (trade['trade_id'],))
    conn.commit()
    print("SUCCESS: Basket 6 has been moved out of trade_log and back into active slots.")

if __name__ == "__main__":
    restore_basket6()
