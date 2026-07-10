import sqlite3
import os
import subprocess

def fix_schema(db_path):
    if not os.path.exists(db_path):
        return
    print(f"Fixing schema for {db_path}...")
    conn = sqlite3.connect(db_path)
    
    # We will try to add each column individually so one failure doesn't block the rest
    columns_to_add = [
        "quantities TEXT",
        "entry_prices TEXT",
        "exit_prices TEXT",
        "hold_minutes REAL",
        "hold_days REAL",
        "status TEXT"
    ]
    
    for col_def in columns_to_add:
        try:
            conn.execute(f"ALTER TABLE trade_log ADD COLUMN {col_def}")
            print(f"  -> Added column: {col_def.split()[0]}")
        except sqlite3.OperationalError:
            pass # Column already exists
            
    conn.commit()
    conn.close()

if __name__ == "__main__":
    print("=== Step 1: Upgrading all Database Schemas ===")
    fix_schema("state/daily_portfolio_state.db")
    fix_schema("state/portfolio_state.db")
    
    print("\n=== Step 2: Restoring Positions from JSON Backups ===")
    if os.path.exists("state/daily_portfolio_state.json"):
        print("Restoring daily engine state...")
        subprocess.run(["python", "migrate_to_sqlite.py", "state/daily_portfolio_state.json", "state/daily_portfolio_state.db"])
        print("Daily state restored.")
        
    if os.path.exists("state/portfolio_state.json"):
        print("Restoring 5-min engine state...")
        subprocess.run(["python", "migrate_to_sqlite.py", "state/portfolio_state.json", "state/portfolio_state.db"])
        print("5-min state restored.")
        
    print("\nAll done! You can now start the engines safely.")
