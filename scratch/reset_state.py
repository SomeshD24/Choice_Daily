import sqlite3
import pandas as pd
import json
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def reset_db(db_path):
    if not os.path.exists(db_path):
        print(f"File not found: {db_path}")
        return
        
    print(f"\n======================================")
    print(f"RESETTING DATABASE: {db_path}")
    print(f"======================================")
    
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    
    # 1. Clear pending entries and exits
    conn.execute("DELETE FROM pending_entries")
    conn.execute("DELETE FROM pending_exits")
    print("Cleared pending_entries and pending_exits.")
    
    # 2. Identify current slots
    slots = conn.execute("SELECT slot_idx, basket_id FROM slots").fetchall()
    active_bids = {s['basket_id']: s['slot_idx'] for s in slots}
    
    # We want ONLY 6 and 7 in slots.
    target_bids = [6, 7]
    
    # 3. Delete any slots that are NOT 6 or 7
    for bid in active_bids:
        if bid not in target_bids:
            conn.execute("DELETE FROM slots WHERE basket_id=?", (bid,))
            print(f"Deleted Basket {bid} from active slots.")
            
    # 4. Restore 6 or 7 if they are missing from slots
    for target in target_bids:
        if target not in active_bids:
            # Need to restore from trade_log
            trade = conn.execute(f"SELECT * FROM trade_log WHERE basket_id={target} ORDER BY trade_id DESC LIMIT 1").fetchone()
            if not trade:
                print(f"WARNING: Basket {target} not found in trade_log! Cannot restore.")
                continue
                
            # Find empty slot
            current_slots = conn.execute("SELECT slot_idx FROM slots").fetchall()
            occupied = [s['slot_idx'] for s in current_slots]
            empty_slot = 0 if 0 not in occupied else 1
            
            tickers = list(json.loads(trade['quantities']).keys())
            conn.execute("""
                INSERT INTO slots (
                    slot_idx, basket_id, entry_time, entry_type, tickers, 
                    quantities, entry_prices, investment, capital_allocated, 
                    entry_basket_close, peak_basket_close, returns_ref_value, returns_ref_time
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                empty_slot,
                target,
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
            print(f"Restored Basket {target} from trade_log to slot {empty_slot}.")
            
    # 5. Clean up trade log (remove any buggy recent entries for 12, 19, 21, etc.)
    # Any trade that started on or after July 7, 2026 should be deleted just to be safe
    # But wait, we only want to delete buggy entries, so basket 12, 19, 21.
    res = conn.execute("DELETE FROM trade_log WHERE basket_id IN (12, 19, 21) AND entry_time >= '2026-07-06'")
    if res.rowcount > 0:
        print(f"Deleted {res.rowcount} buggy entries for B12/B19/B21 from trade_log.")
        
    # Also remove any exits for 6 or 7 that happened recently just in case they are lingering
    # If we restored them above, their exit was deleted. But if they were in slots AND trade_log? 
    res = conn.execute("DELETE FROM trade_log WHERE basket_id IN (6, 7) AND exit_time >= '2026-07-06'")
    if res.rowcount > 0:
        print(f"Deleted {res.rowcount} buggy exits for B6/B7 from trade_log.")
        
    conn.commit()
    print("Database reset successfully.")

if __name__ == "__main__":
    reset_db("state/portfolio_state.db")
    reset_db("state/daily_portfolio_state.db")
    print("\nState is completely clean! You can now safely run python scratch/trigger_yesterday_eod.py")
