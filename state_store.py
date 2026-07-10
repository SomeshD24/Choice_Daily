"""
state_store.py — Persist and restore portfolio state to/from SQLite.

State file includes:
  - Open slot positions (basket_id, entry_time, quantities, prices, etc.)
  - Realized PnL
  - Full trade log
  - Per-ticker rolling buffer (last N 5-min bars) for indicator continuity
"""

import sqlite3
import json
import logging
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from config import STATE_FILE, TRADE_LOG_FILE, IST

logger = logging.getLogger(__name__)


# ── SQLite Setup ─────────────────────────────────────────────────────────────

def _init_db(conn: sqlite3.Connection):
    conn.execute('''
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT
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
            pnl_pct REAL
        )
    ''')
    for col_def in [
        "quantities TEXT",
        "entry_prices TEXT",
        "exit_prices TEXT",
        "hold_minutes REAL",
        "hold_days REAL",
        "status TEXT"
    ]:
        try:
            conn.execute(f"ALTER TABLE trade_log ADD COLUMN {col_def}")
        except sqlite3.OperationalError:
            pass
    conn.execute('''
        CREATE TABLE IF NOT EXISTS basket_close_series (
            basket_id INTEGER,
            timestamp TEXT,
            value REAL,
            PRIMARY KEY (basket_id, timestamp)
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS pending_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            basket_id INTEGER,
            entry_type TEXT,
            capital REAL,
            needs_eviction INTEGER,
            evict_idx INTEGER
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS pending_exits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slot_idx INTEGER,
            reason TEXT
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS ticker_buffers (
            ticker TEXT PRIMARY KEY,
            records TEXT
        )
    ''')
    conn.commit()


# ── Save ──────────────────────────────────────────────────────────────────────

def save_state(portfolio_engine, ticker_buffers: dict,
               state_file: str = STATE_FILE, trade_log_file: str = TRADE_LOG_FILE):
    """
    Persist portfolio engine state + all ticker buffers to SQLite.
    Creates parent dirs if needed.
    """
    Path(state_file).parent.mkdir(parents=True, exist_ok=True)
    
    with sqlite3.connect(state_file, timeout=10) as conn:
        _init_db(conn)
        cursor = conn.cursor()
        
        # 1. Metadata
        cursor.execute("INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)", 
                       ("realized_pnl", str(portfolio_engine.realized_pnl)))
        cursor.execute("INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)", 
                       ("saved_at", datetime.now(IST).isoformat()))
        
        # 2. Slots
        cursor.execute("DELETE FROM slots")
        for i, slot in enumerate(portfolio_engine.slots):
            if slot is not None:
                cursor.execute("""
                    INSERT INTO slots (slot_idx, basket_id, entry_time, entry_type, tickers, quantities, entry_prices, investment, capital_allocated, entry_basket_close, peak_basket_close, returns_ref_value, returns_ref_time)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    i,
                    slot["basket_id"],
                    slot["entry_time"].isoformat() if hasattr(slot["entry_time"], "isoformat") else str(slot["entry_time"]),
                    slot["entry_type"],
                    json.dumps(slot["tickers"]),
                    json.dumps(slot["quantities"]),
                    json.dumps(slot["entry_prices"]),
                    slot["investment"],
                    slot["capital_allocated"],
                    slot.get("entry_basket_close"),
                    slot.get("peak_basket_close"),
                    slot.get("returns_ref_value"),
                    slot["returns_ref_time"].isoformat() if slot.get("returns_ref_time") and hasattr(slot["returns_ref_time"], "isoformat") else None
                ))
        
        # 3. Trade Log
        cursor.execute("DELETE FROM trade_log")
        for trade in portfolio_engine.trade_log:
            cursor.execute("""
                INSERT INTO trade_log (basket_id, entry_time, exit_time, entry_type, exit_reason, investment, exit_value, pnl, pnl_pct, quantities, entry_prices, exit_prices, hold_minutes, hold_days, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                trade.get("basket_id", 0),
                trade.get("entry_time", "").isoformat() if hasattr(trade.get("entry_time"), "isoformat") else str(trade.get("entry_time", "")),
                trade.get("exit_time", "").isoformat() if hasattr(trade.get("exit_time"), "isoformat") else str(trade.get("exit_time", "")),
                trade.get("entry_type", ""),
                trade.get("close_reason", ""),
                trade.get("investment", 0.0),
                trade.get("exit_value", 0.0),
                trade.get("pnl", 0.0),
                trade.get("pnl_pct", 0.0),
                json.dumps(trade.get("quantities", {})),
                json.dumps(trade.get("entry_prices", {})),
                json.dumps(trade.get("exit_prices", {})),
                trade.get("hold_minutes"),
                trade.get("hold_days"),
                trade.get("status", "closed")
            ))
            
        # 4. Basket Close Series
        cursor.execute("DELETE FROM basket_close_series")
        for bid, series in portfolio_engine.basket_close_series.items():
            for ts, val in series.items():
                cursor.execute("INSERT INTO basket_close_series (basket_id, timestamp, value) VALUES (?, ?, ?)",
                               (int(bid), ts.isoformat() if hasattr(ts, 'isoformat') else str(ts), float(val)))
                               
        # 6. Pending Orders
        cursor.execute("DELETE FROM pending_entries")
        for (bid, entry_type, capital, needs_evict, evict_idx) in portfolio_engine._pending_entries:
            cursor.execute("INSERT INTO pending_entries (basket_id, entry_type, capital, needs_eviction, evict_idx) VALUES (?, ?, ?, ?, ?)",
                           (int(bid), str(entry_type), float(capital) if capital is not None else None, int(needs_evict), int(evict_idx)))
                           
        cursor.execute("DELETE FROM pending_exits")
        for (slot_idx, reason) in portfolio_engine._pending_exits:
            cursor.execute("INSERT INTO pending_exits (slot_idx, reason) VALUES (?, ?)", (int(slot_idx), str(reason)))
            
        # 7. Ticker Buffers
        cursor.execute("DELETE FROM ticker_buffers")
        for ticker, buf in ticker_buffers.items():
            if hasattr(buf, "get_df"):
                df = buf.get_df()
            elif hasattr(buf, "df"):
                # Handle DailyTickerBuffer which uses a df() method or df property
                df = buf.df() if callable(buf.df) else buf.df
            else:
                continue
            if not df.empty:
                df = df.tail(1000).copy()
                if isinstance(df.index, pd.DatetimeIndex):
                    # Keep raw index format, convert to string
                    df["datetime"] = df.index.strftime('%Y-%m-%dT%H:%M:%S%z') if df.index.tz is not None else df.index.strftime('%Y-%m-%dT%H:%M:%S')
                else:
                    df["datetime"] = df.index.astype(str)
                records = df.to_dict(orient="records")
                cursor.execute("INSERT INTO ticker_buffers (ticker, records) VALUES (?, ?)", (ticker, json.dumps(records)))
                
        conn.commit()
    
    # Save CSV copy of trade log for potential dashboard use
    if trade_log_file:
        try:
            if portfolio_engine.trade_log:
                df_trades = pd.DataFrame(portfolio_engine.trade_log)
                Path(trade_log_file).parent.mkdir(parents=True, exist_ok=True)
                df_trades.to_csv(trade_log_file, index=False)
        except Exception as e:
            logger.error(f"Failed to save CSV trade log: {e}")


# ── Load Helpers ──────────────────────────────────────────────────────────────

def _decode_ts(s: str | None) -> pd.Timestamp | None:
    if not s:
        return None
    try:
        ts = pd.Timestamp(s)
        if ts.tzinfo is None:
            ts = ts.tz_localize(IST)
        return ts
    except Exception:
        return None

def _deserialize_buffer(records: list) -> pd.DataFrame:
    """Rebuild DataFrame from records"""
    if not records:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
    df = pd.DataFrame(records)
    if "datetime" in df.columns:
        parsed = pd.to_datetime(df["datetime"], errors="coerce", utc=False)
        df.index = parsed
        df.drop(columns=["datetime"], inplace=True, errors="ignore")
        df = df[df.index.notna()]
        if df.empty:
            return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
        if df.index.tz is None:
            df.index = df.index.tz_localize(IST, nonexistent="shift_forward", ambiguous="NaT")
        else:
            df.index = df.index.tz_convert(IST)
        df = df[df.index.notna()]
    return df[["Open", "High", "Low", "Close", "Volume"]].apply(pd.to_numeric, errors="coerce").dropna()

def load_state_dict(state_file: str) -> dict:
    """
    Read SQLite state and return a dictionary matching the old JSON schema.
    This provides seamless backward compatibility for dashboard.py.
    """
    if not Path(state_file).exists():
        return {}
        
    init_db(state_file)
        
    try:
        with sqlite3.connect(state_file, timeout=10) as conn:
            cursor = conn.cursor()
            
            # Metadata
            cursor.execute("SELECT key, value FROM metadata")
            meta = {k: v for k, v in cursor.fetchall()}
            
            # Slots
            cursor.execute("SELECT slot_idx, basket_id, entry_time, entry_type, tickers, quantities, entry_prices, investment, capital_allocated, entry_basket_close, peak_basket_close, returns_ref_value, returns_ref_time FROM slots")
            slots = {}
            for row in cursor.fetchall():
                slots[row[0]] = {
                    "basket_id": row[1],
                    "entry_time": row[2],
                    "entry_type": row[3],
                    "tickers": json.loads(row[4]),
                    "quantities": json.loads(row[5]),
                    "entry_prices": json.loads(row[6]),
                    "investment": row[7],
                    "capital_allocated": row[8],
                    "entry_basket_close": row[9],
                    "peak_basket_close": row[10],
                    "returns_ref_value": row[11],
                    "returns_ref_time": row[12]
                }
            
            max_slot = max(slots.keys()) if slots else -1
            
            from config import N_SLOTS
            slots_list = [slots.get(i) for i in range(N_SLOTS)]
            
            # Trade Log
            cursor.execute("SELECT basket_id, entry_time, exit_time, entry_type, exit_reason, investment, exit_value, pnl, pnl_pct, quantities, entry_prices, exit_prices, hold_minutes, hold_days, status FROM trade_log ORDER BY trade_id")
            trade_log = [
                {
                    "basket_id": r[0],
                    "entry_time": _decode_ts(r[1]),
                    "exit_time": _decode_ts(r[2]),
                    "entry_type": r[3],
                    "close_reason": r[4],
                    "investment": r[5],
                    "exit_value": r[6],
                    "pnl": r[7],
                    "pnl_pct": r[8],
                    "quantities": json.loads(r[9]) if len(r) > 9 and r[9] else {},
                    "entry_prices": json.loads(r[10]) if len(r) > 10 and r[10] else {},
                    "exit_prices": json.loads(r[11]) if len(r) > 11 and r[11] else {},
                    "hold_minutes": r[12] if len(r) > 12 else None,
                    "hold_days": r[13] if len(r) > 13 else None,
                    "status": r[14] if len(r) > 14 and r[14] else "closed"
                } for r in cursor.fetchall()
            ]
            
            # Pending Entries
            cursor.execute("SELECT basket_id, entry_type, capital, needs_eviction, evict_idx FROM pending_entries ORDER BY id")
            pending_entries = [
                {
                    "basket_id": r[0],
                    "entry_type": r[1],
                    "capital": r[2],
                    "needs_eviction": bool(r[3]),
                    "evict_idx": r[4]
                } for r in cursor.fetchall()
            ]
            
            # Pending Exits
            cursor.execute("SELECT slot_idx, reason FROM pending_exits ORDER BY id")
            pending_exits = [
                {
                    "slot_idx": r[0],
                    "reason": r[1]
                } for r in cursor.fetchall()
            ]
            
            # Basket Close Series
            cursor.execute("SELECT basket_id, timestamp, value FROM basket_close_series")
            bcs = {}
            for bid, ts, val in cursor.fetchall():
                bid_str = str(bid)
                if bid_str not in bcs:
                    bcs[bid_str] = {}
                bcs[bid_str][ts] = val
                
            # Ticker Buffers
            cursor.execute("SELECT ticker, records FROM ticker_buffers")
            buffers = {t: json.loads(r) for t, r in cursor.fetchall()}
            
            return {
                "saved_at": meta.get("saved_at"),
                "realized_pnl": float(meta.get("realized_pnl", 0.0)),
                "slots": slots_list,
                "trade_log": trade_log,
                "pending_entries": pending_entries,
                "pending_exits": pending_exits,
                "basket_close_series": bcs,
                "ticker_buffers": buffers
            }
    except Exception as e:
        logger.error(f"Failed to read SQLite state: {e}")
        return {}


# ── Load ──────────────────────────────────────────────────────────────────────

def load_state(portfolio_engine, ticker_buffers: dict,
               state_file: str = STATE_FILE) -> bool:
    """
    Restore portfolio engine and ticker buffers from SQLite state file.
    Returns True if state was loaded, False if no file found.
    """
    from data_manager import TickerBuffer

    if not Path(state_file).exists():
        logger.info(f"No state file at {state_file}, starting fresh")
        return False

    state = load_state_dict(state_file)
    if not state:
        return False

    portfolio_engine.realized_pnl = float(state.get("realized_pnl", 0.0))
    
    # Restore Slots
    portfolio_engine.slots = []
    for s in state.get("slots", []):
        if s is None:
            portfolio_engine.slots.append(None)
        else:
            portfolio_engine.slots.append({
                "basket_id":         s["basket_id"],
                "entry_time":        _decode_ts(s["entry_time"]),
                "entry_type":        s["entry_type"],
                "tickers":           s["tickers"],
                "quantities":        s["quantities"],
                "entry_prices":      s["entry_prices"],
                "investment":        float(s["investment"]),
                "capital_allocated": float(s["capital_allocated"]),
                "entry_basket_close": s.get("entry_basket_close"),
                "peak_basket_close": s.get("peak_basket_close"),
                "returns_ref_value": float(s.get("returns_ref_value", s["investment"])),
                "returns_ref_time":  _decode_ts(s.get("returns_ref_time")),
            })
            
    # Pad slots
    while len(portfolio_engine.slots) < portfolio_engine.n_slots:
        portfolio_engine.slots.append(None)
        
    portfolio_engine.trade_log = state.get("trade_log", [])

    # Restore basket close series
    bcs_raw = state.get("basket_close_series", {})
    for bid_str, kv in bcs_raw.items():
        bid = int(bid_str)
        s = pd.Series({pd.Timestamp(k): float(v) for k, v in kv.items()})
        portfolio_engine.basket_close_series[bid] = s

    # Restore pending orders
    portfolio_engine._pending_entries = [(p["basket_id"], p["entry_type"], p.get("capital"), p.get("needs_eviction", False), p.get("evict_idx", -1)) for p in state.get("pending_entries", [])]
    portfolio_engine._pending_exits   = [(p["slot_idx"], p["reason"]) for p in state.get("pending_exits", [])]

    # Restore ticker buffers
    buffers_raw = state.get("ticker_buffers", {})
    for ticker, records in buffers_raw.items():
        live_buf = ticker_buffers.get(ticker)
        live_bars = live_buf.n_bars if live_buf is not None else 0
        state_df  = _deserialize_buffer(records)
        state_bars = len(state_df)

        if live_bars == 0 and state_bars > 0:
            if ticker not in ticker_buffers:
                ticker_buffers[ticker] = TickerBuffer(ticker)
            ticker_buffers[ticker].seed(state_df)
            logger.debug(f"  {ticker}: restored {state_bars} bars from state (no live warmup)")
        elif state_bars > live_bars:
            ticker_buffers[ticker].seed(state_df)
            logger.debug(f"  {ticker}: restored {state_bars} bars from state (state richer than warmup {live_bars})")
        else:
            logger.debug(f"  {ticker}: keeping {live_bars} live warmup bars (state had {state_bars})")

    saved_at = state.get("saved_at", "unknown")
    active = sum(1 for s in portfolio_engine.slots if s is not None)
    logger.info(f"State loaded from SQLite {state_file} (saved {saved_at})  "
                f"active_slots={active}  realized_pnl={portfolio_engine.realized_pnl:.0f}")
    return True
