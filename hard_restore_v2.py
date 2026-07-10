import sqlite3
import json
import os

def hard_restore():
    db_path = "state/daily_portfolio_state.db"
    
    # Make sure we have the right schema first
    conn = sqlite3.connect(db_path)
    
    # Re-create tables if missing
    conn.execute('''
        CREATE TABLE IF NOT EXISTS trade_log (
            trade_id INTEGER PRIMARY KEY AUTOINCREMENT,
            basket_id INTEGER,
            entry_time TEXT,
            exit_time TEXT,
            entry_type TEXT,
            exit_reason TEXT,
            investment REAL,
            exit_value REAL,
            pnl REAL,
            pnl_pct REAL,
            quantities TEXT,
            entry_prices TEXT,
            exit_prices TEXT,
            hold_minutes REAL,
            hold_days REAL,
            status TEXT
        )
    ''')
    
    conn.execute('''
        CREATE TABLE IF NOT EXISTS slots (
            slot_idx INTEGER PRIMARY KEY,
            basket_id INTEGER,
            entry_time TEXT,
            entry_type TEXT,
            tickers TEXT,
            quantities TEXT,
            entry_prices TEXT,
            investment REAL,
            capital_allocated REAL,
            entry_basket_close REAL,
            peak_basket_close REAL,
            returns_ref_value REAL,
            returns_ref_time TEXT
        )
    ''')
    
    # Clear wiped tables
    conn.execute("DELETE FROM slots")
    conn.execute("DELETE FROM trade_log")
    
    b9_prices = {'COLPAL.NS': 2069.4, 'DIVISLAB.NS': 6870.0, 'ETERNAL.NS': 294.7, 'FEDERALBNK.NS': 330.3, 'KALYANKJIL.NS': 451.1, 'ONGC.NS': 243.65}
    b9_qtys = {"FEDERALBNK.NS": 20252, "DIVISLAB.NS": 356, "ETERNAL.NS": 13227, "COLPAL.NS": 1041, "ONGC.NS": 19749, "KALYANKJIL.NS": 23239}
    b9_investment = sum(b9_prices[t] * b9_qtys[t] for t in b9_qtys)
    
    slots_data = [
      {
        'basket_id': 7, 
        'entry_time': '2026-04-23T09:17:00+05:30', 
        'entry_type': 'ema_crossover', 
        'tickers': ['PFC.NS', 'BRITANNIA.NS', 'KPITTECH.NS', 'ADANIGREEN.NS', 'LUPIN.NS', 'SUPREMEIND.NS'], 
        'quantities': {'PFC.NS': 1782, 'BRITANNIA.NS': 145, 'KPITTECH.NS': 1133, 'ADANIGREEN.NS': 698, 'LUPIN.NS': 361, 'SUPREMEIND.NS': 224}, 
        'entry_prices': {'PFC.NS': 467.5, 'BRITANNIA.NS': 5724.0, 'KPITTECH.NS': 735.0, 'ADANIGREEN.NS': 1193.0, 'LUPIN.NS': 2307.9, 'SUPREMEIND.NS': 3709.1}, 
        'investment': 4992524.3, 
        'capital_allocated': 5000000.0, 
        'entry_basket_close': 95311466.44958496, 
        'peak_basket_close': 95311466.44958496, 
        'returns_ref_value': 4994570.0, 
        'returns_ref_time': '2026-04-23T09:17:00+05:30'
      },
      {
        'basket_id': 9, 
        'entry_time': '2026-07-10T09:17:00+05:30', 
        'entry_type': 'ema_crossover', 
        'tickers': ['FEDERALBNK.NS', 'DIVISLAB.NS', 'ETERNAL.NS', 'COLPAL.NS', 'ONGC.NS', 'KALYANKJIL.NS'], 
        'quantities': b9_qtys, 
        'entry_prices': b9_prices, 
        'investment': b9_investment, 
        'capital_allocated': 5000000.0, 
        'entry_basket_close': b9_investment, 
        'peak_basket_close': b9_investment, 
        'returns_ref_value': b9_investment, 
        'returns_ref_time': '2026-07-10T09:17:00+05:30'
      }
    ]
    
    for i, s in enumerate(slots_data):
        conn.execute("""
            INSERT INTO slots (slot_idx, basket_id, entry_time, entry_type, tickers, quantities, entry_prices, investment, capital_allocated, entry_basket_close, peak_basket_close, returns_ref_value, returns_ref_time)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            i, s["basket_id"], s["entry_time"], s["entry_type"], 
            json.dumps(s["tickers"]), json.dumps(s["quantities"]), json.dumps(s["entry_prices"]),
            s["investment"], s["capital_allocated"], s["entry_basket_close"], s["peak_basket_close"], 
            s["returns_ref_value"], s["returns_ref_time"]
        ))
        
    closed_trades = [
      {
        'basket_id': 17, 'entry_time': '2026-06-19T09:15:00+05:30', 'exit_time': '2026-07-02T10:07:32.340010+05:30',
        'entry_type': 'ema_crossover', 'exit_reason': 'nan', 'investment': 4996433.0, 'exit_value': 5176453.72,
        'pnl': 180020.72, 'pnl_pct': 3.6, 'hold_days': None, 'hold_minutes': None, 'status': 'closed'
      },
      {
        'basket_id': 6, 'entry_time': '2026-07-02T10:07:32.340010+05:30', 'exit_time': '2026-07-08T12:36:12.418988+05:30',
        'entry_type': 'ema_crossover', 'exit_reason': 'nan', 'investment': 5184180.65, 'exit_value': 5242844.95,
        'pnl': 58664.3, 'pnl_pct': 1.13, 'hold_days': None, 'hold_minutes': None, 'status': 'closed'
      },
      {
        'basket_id': 19, 'entry_time': '2026-07-08 12:36:12.418988+05:30', 'exit_time': '2026-07-10 10:40:55.845614+05:30',
        'entry_type': 'ema_crossover', 'exit_reason': 'evicted_new_entry', 'investment': 5218325.7, 'exit_value': 5193348.7,
        'pnl': -24977, 'pnl_pct': -0.48, 'hold_days': 1.92, 'hold_minutes': None, 'status': 'closed'
      }
    ]
    
    for t in closed_trades:
        conn.execute("""
            INSERT INTO trade_log (basket_id, entry_time, exit_time, entry_type, exit_reason, investment, exit_value, pnl, pnl_pct, hold_days, hold_minutes, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            t["basket_id"], t["entry_time"], t["exit_time"], t["entry_type"], t["exit_reason"], 
            t["investment"], t["exit_value"], t["pnl"], t["pnl_pct"], t["hold_days"], t["hold_minutes"], t["status"]
        ))
        
    conn.execute("INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)", ("realized_pnl", "213708.02"))
    
    conn.commit()
    conn.close()
    print(f"State 100% recovered manually using user inputs! B7 and B9 are OPEN.")

if __name__ == "__main__":
    hard_restore()
