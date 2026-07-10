import sqlite3
import json
import os

def restore_buffers():
    db_path = "state/daily_portfolio_state.db"
    json_path = "state/daily_portfolio_state.json"
    
    if not os.path.exists(db_path) or not os.path.exists(json_path):
        print("Missing state files!")
        return

    print("Loading buffers from JSON backup...")
    with open(json_path, "r") as f:
        state_json = json.load(f)
        
    bcs = state_json.get("basket_close_series", {})
    buffers = state_json.get("ticker_buffers", {})
    
    print(f"Found {len(bcs)} basket close series and {len(buffers)} ticker buffers.")
    
    conn = sqlite3.connect(db_path)
    
    # Insert basket_close_series
    conn.execute("DELETE FROM basket_close_series")
    for bid, series in bcs.items():
        for ts, val in series.items():
            conn.execute("INSERT INTO basket_close_series (basket_id, timestamp, value) VALUES (?, ?, ?)",
                           (int(bid), str(ts), float(val)))
                           
    # Insert ticker_buffers
    conn.execute("DELETE FROM ticker_buffers")
    for ticker, records in buffers.items():
        conn.execute("INSERT INTO ticker_buffers (ticker, records) VALUES (?, ?)", 
                     (ticker, json.dumps(records)))
                     
    conn.commit()
    conn.close()
    
    print("Successfully restored all historical 5-minute buffers into SQLite!")

if __name__ == "__main__":
    restore_buffers()
