import sys
import json
import logging
from pathlib import Path
import pandas as pd

# Add current dir to path to import state_store
sys.path.append(str(Path(__file__).parent))
from state_store import save_state, _decode_ts, _deserialize_buffer

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class DummyPortfolio:
    pass

class DummyBuffer:
    def __init__(self, df):
        self.df = df
    def get_df(self):
        return self.df

def migrate(json_path: str, db_path: str):
    logger.info(f"Migrating {json_path} -> {db_path}")
    if not Path(json_path).exists():
        logger.error(f"Input file not found: {json_path}")
        return
        
    with open(json_path, 'r') as f:
        state = json.load(f)
        
    engine = DummyPortfolio()
    engine.realized_pnl = state.get("realized_pnl", 0.0)
    
    engine.slots = []
    for s in state.get("slots", []):
        if s is None:
            engine.slots.append(None)
        else:
            engine.slots.append({
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
            
    engine.trade_log = state.get("trade_log", [])
    
    bcs_raw = state.get("basket_close_series", {})
    engine.basket_close_series = {}
    for bid_str, kv in bcs_raw.items():
        bid = int(bid_str)
        engine.basket_close_series[bid] = pd.Series({pd.Timestamp(k): float(v) for k, v in kv.items()})
        
    engine.historical_equity = state.get("historical_equity", {})
    
    engine._pending_entries = [(p["basket_id"], p["entry_type"], p.get("capital"), p.get("needs_eviction", False), p.get("evict_idx", -1)) for p in state.get("pending_entries", [])]
    engine._pending_exits   = [(p["slot_idx"], p["reason"]) for p in state.get("pending_exits", [])]
    
    buffers_raw = state.get("ticker_buffers", {})
    ticker_buffers = {}
    for ticker, records in buffers_raw.items():
        df = _deserialize_buffer(records)
        ticker_buffers[ticker] = DummyBuffer(df)
        
    save_state(engine, ticker_buffers, db_path, None)
    logger.info("Migration successful!")

if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python migrate_to_sqlite.py <input.json> <output.db>")
        sys.exit(1)
    migrate(sys.argv[1], sys.argv[2])
