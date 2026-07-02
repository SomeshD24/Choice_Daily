import sqlite3
import pandas as pd

DB_PATH = r"c:\Choice_Daily\state\daily_portfolio_state.db"

def repair():
    with sqlite3.connect(DB_PATH) as conn:
        print("--- Pending entries before ---")
        try:
            df = pd.read_sql("SELECT * FROM pending_entries", conn)
            print(df)
        except Exception as e:
            print("Error reading:", e)
            return
            
        print("\nInserting B6 entry...")
        # Check if B6 is already there
        if not df.empty and 6 in df['basket_id'].values:
            print("B6 is already in pending_entries!")
        else:
            conn.execute(
                "INSERT INTO pending_entries (basket_id, entry_type, capital, needs_eviction, evict_idx) VALUES (?, ?, ?, ?, ?)",
                (6, "ema50x100", None, 0, -1)
            )
            conn.commit()
            print("Inserted B6 successfully.")
            
        print("\n--- Pending entries after ---")
        print(pd.read_sql("SELECT * FROM pending_entries", conn))

if __name__ == "__main__":
    repair()
