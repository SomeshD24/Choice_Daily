import sys
from pathlib import Path
import sqlite3
import pandas as pd
import json

# Add strategy code to path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from strategy_variant_sector_regime import (
    prepare_global_data,
    load_basket_configs,
    run_backtest_for_config,
    N_SLOTS,
    POSITION_SIZE,
    EMA_FAST,
    EMA_SLOW
)

def recover_state():
    print("Reconstructing portfolio state from historical data up to today...")
    
    # Load all data up to today
    data_dir = Path("data")
    if not data_dir.exists():
        print("Error: data/ directory not found. Please run this on the cloud server.")
        return
        
    csv_path = data_dir / "baskets_nifty200_all_sizes.csv"
    if not csv_path.exists():
        print(f"Error: {csv_path} not found.")
        return
        
    print("1. Preparing global data (this may take a moment)...")
    comp_close, comp_open, basket_close, returns_5m, peer_ind = prepare_global_data()
    baskets = load_basket_configs(str(csv_path))
    
    # Filter for size 6 baskets only
    baskets = {bid: b for bid, b in baskets.items() if len(b['tickers']) == 6}
    
    # We want to run the backtest up to the latest available day (today)
    end_date = comp_close.index[-1].strftime('%Y-%m-%d')
    # Start date needs to be early enough to catch the current open slots
    start_date = "2026-01-01" 
    
    print(f"2. Simulating strategy from {start_date} to {end_date}...")
    
    metrics, port_trades, eq_series = run_backtest_for_config(
        baskets,
        start_date,
        end_date,
        comp_close,
        comp_open,
        basket_close,
        returns_5m,
        peer_ind
    )
    
    print("3. Simulation complete. Extracting final state...")
    
    # Find all trades that are currently OPEN
    open_trades = [t for t in port_trades if t['status'] == 'open']
    
    # Format open trades for slots table
    slots_data = []
    for i, t in enumerate(open_trades):
        slots_data.append({
            'slot_idx': i,
            'basket_id': t['basket_id'],
            'entry_time': str(t['buy_date']),
            'entry_type': t['entry_type'],
            'tickers': json.dumps(baskets[t['basket_id']]['tickers']),
            'quantities': json.dumps(t['quantities']),
            'entry_prices': json.dumps(t['entry_prices']),
            'investment': float(t['investment']),
            'capital_allocated': float(POSITION_SIZE / N_SLOTS),
            'entry_basket_close': float(t['entry_basket_close']),
            'peak_basket_close': float(t['peak_basket_close']),
            'returns_ref_value': float(t['returns_ref_value']),
            'returns_ref_time': str(t['returns_ref_time'])
        })
    
    # Get all closed trades
    closed_trades = [t for t in port_trades if t['status'] == 'closed']
    
    print(f"Found {len(open_trades)} currently open slots and {len(closed_trades)} closed trades.")
    
    db_path = "state/daily_portfolio_state.db"
    print(f"4. Writing exact state to {db_path}...")
    
    conn = sqlite3.connect(db_path)
    
    # Recreate tables just in case they are missing columns
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
    
    conn.execute("DELETE FROM slots")
    conn.execute("DELETE FROM trade_log")
    
    for s in slots_data:
        conn.execute("""
            INSERT INTO slots (slot_idx, basket_id, entry_time, entry_type, tickers, quantities, entry_prices, investment, capital_allocated, entry_basket_close, peak_basket_close, returns_ref_value, returns_ref_time)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            s["slot_idx"], s["basket_id"], s["entry_time"], s["entry_type"], 
            s["tickers"], s["quantities"], s["entry_prices"],
            s["investment"], s["capital_allocated"], s["entry_basket_close"], s["peak_basket_close"], 
            s["returns_ref_value"], s["returns_ref_time"]
        ))
        
    for t in closed_trades:
        conn.execute("""
            INSERT INTO trade_log (basket_id, entry_time, exit_time, entry_type, exit_reason, investment, exit_value, pnl, pnl_pct, hold_days, hold_minutes, status, quantities, entry_prices, exit_prices)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            t["basket_id"], str(t["buy_date"]), str(t["sell_date"]), t["entry_type"], t.get("exit_reason", "nan"), 
            float(t["investment"]), float(t["exit_value"]), float(t["pnl"]), float(t["pnl_pct"]), 
            float(t.get("hold_days", 0)), None, t["status"], 
            json.dumps(t.get("quantities", {})), json.dumps(t.get("entry_prices", {})), json.dumps(t.get("exit_prices", {}))
        ))
        
    total_realized_pnl = sum(t["pnl"] for t in closed_trades)
    conn.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)", ("realized_pnl", str(total_realized_pnl)))
    
    conn.commit()
    conn.close()
    
    print("\n✅ State perfectly recovered! You can now refresh the dashboard.")
    print("Open slots dynamically calculated:")
    for s in slots_data:
        print(f" - Basket {s['basket_id']} (Entered {s['entry_time']})")

if __name__ == "__main__":
    recover_state()
