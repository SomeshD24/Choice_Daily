import sqlite3
import json
import logging
from datetime import datetime

import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def check_and_fix_db(db_path):
    print(f"\n==============================")
    print(f"Checking DB: {db_path}")
    print(f"==============================")
    
    if not os.path.exists(db_path):
        print(f"File not found: {db_path}")
        return
        
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    
    print("\n--- ACTIVE SLOTS ---")
    slots = conn.execute("SELECT * FROM slots").fetchall()
    for s in slots:
        print(f"Slot {s['slot_idx']}: Basket {s['basket_id']} | Entry Time: {s['entry_time']} | Type: {s['entry_type']}")
        
    print("\n--- PENDING ENTRIES ---")
    pending = conn.execute("SELECT * FROM pending_entries").fetchall()
    for p in pending:
        print(f"Basket {p['basket_id']} | Type: {p['entry_type']}")
        
    print("\n--- REMOVING BASKET 12 ---")
    # Delete from slots
    res = conn.execute("DELETE FROM slots WHERE basket_id=12")
    if res.rowcount > 0:
        print(f"[SUCCESS] Deleted Basket 12 from active slots.")
    else:
        print("Basket 12 not found in active slots.")
        
    # Delete from pending_entries
    res = conn.execute("DELETE FROM pending_entries WHERE basket_id=12")
    if res.rowcount > 0:
        print(f"[SUCCESS] Deleted Basket 12 from pending_entries.")
        
    # Delete from pending_exits (just in case slot_idx matches, but we don't know slot idx)
    # We will just delete all pending exits for empty slots next.
        
    # Delete from trade_log
    res = conn.execute("DELETE FROM trade_log WHERE basket_id=12")
    if res.rowcount > 0:
        print(f"[SUCCESS] Deleted Basket 12 from trade_log.")
        
    conn.commit()
    print(f"Finished processing {db_path}\n")


if __name__ == "__main__":
    # Check both the daily and the paper trade databases
    check_and_fix_db("state/portfolio_state.db")
    check_and_fix_db("state/daily_portfolio_state.db")
