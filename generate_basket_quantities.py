import json
from pathlib import Path
import numpy as np
import pandas as pd
import yfinance as yf

# Paths
HERE = Path(__file__).resolve().parent
ROOT = HERE
CSV_PATH = ROOT / "data" / "baskets_nifty200_all_sizes.csv"
OUT_PATH = ROOT / "state" / "basket_quantities_6.json"

POSITION_SIZE = 10_000_000
START_DATE = "2003-01-01"

def load_ohlc(ticker: str) -> pd.DataFrame:
    df = yf.download(ticker, start=START_DATE, auto_adjust=True, progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.droplevel(1)
    
    # Normalize columns to match backtest logic
    rename_map = {}
    for c in df.columns:
        c_lower = c.lower()
        if 'open' in c_lower: rename_map[c] = 'Open'
        elif 'high' in c_lower: rename_map[c] = 'High'
        elif 'low' in c_lower: rename_map[c] = 'Low'
        elif 'close' in c_lower: rename_map[c] = 'Close'
        elif 'vol' in c_lower: rename_map[c] = 'Volume'
    df = df.rename(columns=rename_map)
    return df

def main():
    print(f"Loading baskets from {CSV_PATH}")
    df = pd.read_csv(CSV_PATH)
    df_6 = df[df['basket_size'] == 6]
    
    basket_groups = df_6.groupby("basket_id")
    quantities_map = {}
    
    for bid, grp in basket_groups:
        tickers = grp['ticker'].tolist()
        print(f"\nProcessing Basket {bid} (Size {len(tickers)})")
        
        fields = ['Open', 'High', 'Low', 'Close', 'Volume']
        loaded = []
        for t in tickers:
            print(f"  Fetching {t}...", end="", flush=True)
            hist = load_ohlc(t)
            missing = [f for f in fields if f not in hist.columns]
            if missing:
                print(f" ERROR: Missing {missing}")
                continue
            hist_sub = hist[fields].rename(columns={c: (t, c) for c in fields})
            loaded.append(hist_sub)
            print(f" ({len(hist)} rows)")
            
        panel = pd.concat(loaded, axis=1).dropna()
        if panel.empty:
            print(f"  -> ERROR: Empty panel after dropna!")
            continue
            
        first_date = panel.index[0]
        print(f"  -> Common start date: {first_date.date()} ({len(panel)} overlapping days)")
        
        weights = np.repeat(1.0 / len(tickers), len(tickers))
        first_close = pd.Series({t: panel[(t, 'Close')].iloc[0] for t in tickers}, dtype=float)
        
        quantities = np.floor((weights * POSITION_SIZE) / first_close).astype(int)
        
        quantities_map[str(bid)] = {t: int(q) for t, q in quantities.items()}
        print(f"  -> Quantities: {quantities_map[str(bid)]}")
        
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump(quantities_map, f, indent=2)
    print(f"\nSaved exact backtest quantities to {OUT_PATH}")

if __name__ == "__main__":
    main()
