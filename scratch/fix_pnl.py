import sqlite3
import os

def fix_pnl(db_path):
    if not os.path.exists(db_path):
        return
        
    conn = sqlite3.connect(db_path)
    # Calculate sum of pnl from trade log
    res = conn.execute("SELECT sum(pnl) FROM trade_log").fetchone()
    total_pnl = res[0] if res and res[0] is not None else 0.0
    
    # Update metadata
    conn.execute("UPDATE metadata SET value=? WHERE key='realized_pnl'", (str(total_pnl),))
    conn.commit()
    print(f"{db_path}: Set realized_pnl to {total_pnl}")

if __name__ == "__main__":
    fix_pnl("state/portfolio_state.db")
    fix_pnl("state/daily_portfolio_state.db")
