import sqlite3
import json

def fix_b9_entry_close():
    db_path = "state/daily_portfolio_state.db"
    conn = sqlite3.connect(db_path)
    
    # B9 fixed reference quantities from basket_quantities_6.json
    b9_ref_qtys = {
        "FEDERALBNK.NS": 20252,
        "DIVISLAB.NS": 356,
        "ETERNAL.NS": 13227,
        "COLPAL.NS": 1041,
        "ONGC.NS": 19749,
        "KALYANKJIL.NS": 23239
    }
    
    # Entry prices for B9 today
    b9_prices = {
        'COLPAL.NS': 2069.4, 
        'DIVISLAB.NS': 6870.0, 
        'ETERNAL.NS': 294.7, 
        'FEDERALBNK.NS': 330.3, 
        'KALYANKJIL.NS': 451.1, 
        'ONGC.NS': 243.65
    }
    
    # Calculate the reference basket close using fixed quantities
    ref_basket_close = sum(b9_prices[t] * b9_ref_qtys[t] for t in b9_prices)
    
    # Update B9's entry_basket_close, peak_basket_close, and returns_ref_value in the slots table
    conn.execute("""
        UPDATE slots
        SET entry_basket_close = ?, peak_basket_close = ?, returns_ref_value = ?
        WHERE basket_id = 9
    """, (ref_basket_close, ref_basket_close, ref_basket_close))
    
    conn.commit()
    conn.close()
    print(f"Fixed B9 entry_basket_close to {ref_basket_close:,.2f}")

if __name__ == "__main__":
    fix_b9_entry_close()
