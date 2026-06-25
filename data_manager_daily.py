"""
data_manager_daily.py — Daily OHLCV data via IntegrateData.

Key differences from the 5-min data_manager:
  - Uses conn.TIMEFRAME_TYPE_DAY  → daily bars directly, no resampling.
  - Warmup: ~1300 calendar days (covers 756+ trading days for regression).
  - EOD update: called at 15:35; appends today's completed daily bar.
  - Open-price fetch: called at 09:17 via 1-min bar for execution price.
  - Basket OHLC built from daily buffers (identical equal-weight logic).
"""

import logging
from datetime import datetime, timedelta, time as dtime

import numpy as np
import pandas as pd

from config_daily import (
    IST, EXCHANGE, MARKET_OPEN_H, MARKET_OPEN_M,
    WARMUP_TRADING_DAYS, WARMUP_CALENDAR_DAYS,
    HISTORICAL_TF_DAY, HISTORICAL_TF_MIN,
    MIN_ALIGNED_DAYS, POSITION_SIZE,
)

logger = logging.getLogger(__name__)


# ── Response parser (reused from 5-min engine) ────────────────────────────────

def _parse_hist_row(row: dict) -> dict | None:
    """Normalise one dict from ic.historical_data() — handles varied key names."""
    ts = (row.get("datetime") or row.get("time") or
          row.get("date")     or row.get("ts"))
    if ts is None:
        return None

    def _f(keys):
        for k in keys:
            v = row.get(k)
            if v is not None:
                try:
                    return float(v)
                except (TypeError, ValueError):
                    pass
        return None

    o = _f(["open",   "Open",   "o"])
    h = _f(["high",   "High",   "h"])
    l = _f(["low",    "Low",    "l"])
    c = _f(["close",  "Close",  "c"])
    v = _f(["volume", "Volume", "v", "vol"])

    if None in (o, h, l, c):
        return None
    return {"ts": ts, "Open": o, "High": h, "Low": l, "Close": c, "Volume": v or 0.0}


def _records_to_df(records: list, tz=IST) -> pd.DataFrame:
    """Convert parsed row-list to a DatetimeIndex OHLCV DataFrame (IST-aware)."""
    if not records:
        return pd.DataFrame()
    df = pd.DataFrame(records)
    df.index = pd.to_datetime(df["ts"])
    df.drop(columns=["ts"], inplace=True)
    if df.index.tzinfo is None:
        df.index = df.index.tz_localize(tz, nonexistent="shift_forward", ambiguous="NaT")
    else:
        df.index = df.index.tz_convert(tz)
    df = df[["Open", "High", "Low", "Close", "Volume"]].apply(pd.to_numeric, errors="coerce")
    return df.dropna(subset=["Close"]).sort_index()


# ── Daily OHLCV fetch ─────────────────────────────────────────────────────────

def fetch_daily_historical(
    client,
    segment_id: int,
    token: int,
    yf_ticker: str,
    from_dt: datetime,
    to_dt: datetime,
) -> pd.DataFrame:
    import yfinance as yf
    try:
        df = client.get_chart_data(segment_id, token, from_dt, to_dt, interval="D")
        if not df.empty and len(df) > 50:
            return df
    except Exception as e:
        logger.debug(f"Choice API daily fetch failed for {yf_ticker}, falling back to yfinance: {e}")
        
    try:
        df = yf.download(yf_ticker, start=from_dt.strftime('%Y-%m-%d'), end=(to_dt + pd.Timedelta(days=1)).strftime('%Y-%m-%d'), progress=False)
        if df.empty:
            return pd.DataFrame()
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        if df.index.tz is None:
            df.index = df.index.tz_localize(IST)
        else:
            df.index = df.index.tz_convert(IST)
        df.index.name = None
        return df[["Open", "High", "Low", "Close", "Volume"]]
    except Exception as e:
        logger.error(f"daily historical_data failed for {yf_ticker}: {e}")
        return pd.DataFrame()


def fetch_todays_open(
    client,
    segment_id: int,
    token: int,
) -> float | None:
    now     = datetime.now(IST)
    from_dt = now.replace(hour=MARKET_OPEN_H, minute=MARKET_OPEN_M - 1, second=0, microsecond=0)
    to_dt = now

    try:
        df = client.get_chart_data(segment_id, token, from_dt, to_dt, interval="1")
        if df.empty:
            return None
        return float(df["Open"].iloc[0])
    except Exception as e:
        logger.error(f"fetch_todays_open failed for token {token}: {e}")
        return None


def fetch_live_ltp(
    client,
    segment_id: int,
    token: int,
) -> float | None:
    now     = datetime.now(IST)
    from_dt = now - timedelta(minutes=10)
    to_dt   = now

    try:
        df = client.get_chart_data(segment_id, token, from_dt, to_dt, interval="1")
        if df.empty:
            return None
        return float(df["Close"].iloc[-1])
    except Exception as e:
        logger.error(f"fetch_live_ltp failed for token {token}: {e}")
        return None


# ── Rolling daily buffer ──────────────────────────────────────────────────────

class DailyTickerBuffer:
    """
    Rolling buffer of completed daily OHLCV bars for one ticker.
    Keeps at most MAX_BARS bars (trims oldest on append).
    """
    MAX_BARS = WARMUP_TRADING_DAYS + 50

    def __init__(self, ticker: str):
        self.ticker = ticker
        self._df = pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

    def seed(self, df_daily: pd.DataFrame):
        self._df = df_daily.copy().tail(self.MAX_BARS)

    def append_bar(self, bar: dict):
        """
        Append one completed daily bar.
        bar keys: datetime (pd.Timestamp), Open, High, Low, Close, Volume.
        """
        ts = bar.get("datetime")
        if ts is None:
            return
        new_row = pd.DataFrame(
            [[bar["Open"], bar["High"], bar["Low"], bar["Close"], bar.get("Volume", 0)]],
            columns=["Open", "High", "Low", "Close", "Volume"],
            index=[ts],
        )
        self._df = pd.concat([self._df, new_row])
        self._df = self._df[~self._df.index.duplicated(keep="last")]
        self._df.sort_index(inplace=True)
        if len(self._df) > self.MAX_BARS:
            self._df = self._df.iloc[-self.MAX_BARS:]

    def append_today(self, client, segment_id: int, token: int, yf_ticker: str):
        now  = datetime.now(IST)
        from_dt = now - timedelta(days=3)
        to_dt   = now

        df = fetch_daily_historical(client, segment_id, token, yf_ticker, from_dt, to_dt)
        if df.empty:
            return False

        today = now.date()
        today_bars = df[df.index.date == today]
        if today_bars.empty:
            logger.debug(f"{yf_ticker}: today's daily bar not yet available")
            return False

        row = today_bars.iloc[-1]
        bar = {
            "datetime": today_bars.index[-1],
            "Open":   float(row["Open"]),
            "High":   float(row["High"]),
            "Low":    float(row["Low"]),
            "Close":  float(row["Close"]),
            "Volume": float(row["Volume"]),
        }
        self.append_bar(bar)
        return True

    @property
    def df(self) -> pd.DataFrame:
        return self._df.copy()

    @property
    def n_bars(self) -> int:
        return len(self._df)


# ── Basket OHLC builder (daily) ───────────────────────────────────────────────

def build_basket_daily_ohlc(
    ticker_buffers: dict,       # {yf_ticker: DailyTickerBuffer}
    tickers: list[str],
    position_size: float = POSITION_SIZE,
) -> pd.DataFrame | None:
    """
    Equal-weight basket daily OHLC — EXACT port of build_equal_weight_basket_ohlc()
    from strategy1_dualbasket2.py.

    Key invariants (matching the backtest):
      1. Build a MultiIndex-style panel by concat of all ticker OHLCV DataFrames.
      2. panel.dropna() → inner join on ALL fields across ALL tickers simultaneously.
         This gives the exact same common-start-date as the backtest.
      3. first_close = panel[(t, 'Close')].iloc[0]  ← first row of JOINT panel.
      4. quantities[t] = (1/N * position_size) / first_close[t]
      5. basket field = sum(panel[(t, field)] * quantities[t])
      6. Volume = simple sum (no weighting).

    Returns None if insufficient aligned data.
    """
    if not all(t in ticker_buffers for t in tickers):
        return None

    fields = ["Open", "High", "Low", "Close", "Volume"]
    dfs    = {t: ticker_buffers[t].df for t in tickers}
    if any(d.empty for d in dfs.values()):
        return None

    # Build full MultiIndex panel — exactly as in backtest:
    #   loaded = [load_ohlc(t)[fields].rename(columns={c: (t,c) for c in fields}) for t in tickers]
    #   panel  = pd.concat(loaded, axis=1).dropna()
    loaded = [
        dfs[t][fields].rename(columns={c: (t, c) for c in fields})
        for t in tickers
    ]
    panel = pd.concat(loaded, axis=1).dropna()

    if len(panel) < MIN_ALIGNED_DAYS:
        return None

    import json
    from pathlib import Path
    
    # Load EXACT backtest quantities from JSON (to prevent rolling window drift)
    # Try multiple candidate paths — __file__ parent may differ from CWD at runtime.
    _candidates = [
        Path(__file__).resolve().parent / "state" / "basket_quantities_6.json",
        Path.cwd() / "state" / "basket_quantities_6.json",
        Path(__file__).resolve().parent / "basket_quantities_6.json",
        Path.cwd() / "basket_quantities_6.json",
    ]
    q_file = next((p for p in _candidates if p.exists()), None)
    if q_file is None:
        logger.error(
            "basket_quantities_6.json not found. Tried: %s",
            ", ".join(str(p) for p in _candidates),
        )
        return None

    try:
        with open(q_file) as f:
            all_q = json.load(f)
    except Exception as e:
        logger.error("Failed to parse %s: %s", q_file, e)
        return None
        
    quantities = None
    for bid, q_map in all_q.items():
        if set(q_map.keys()) == set(tickers):
            quantities = q_map
            break
            
    if quantities is None:
        logger.error(
            "No basket in %s matches tickers %s. Available baskets: %s",
            q_file,
            sorted(tickers),
            {bid: sorted(q.keys()) for bid, q in all_q.items()},
        )
        return None  # Missing fixed quantities for this basket

    basket = pd.DataFrame(index=panel.index)
    for field in ["Open", "High", "Low", "Close"]:
        basket[field] = sum(panel[(t, field)] * quantities[t] for t in tickers)
    basket["Volume"] = sum(panel[(t, "Volume")] for t in tickers)

    basket = basket.dropna().sort_index()

    basket.attrs["component_close"] = pd.DataFrame(
        {t: panel[(t, "Close")] for t in tickers}, index=basket.index
    ).sort_index()
    basket.attrs["component_open"] = pd.DataFrame(
        {t: panel[(t, "Open")] for t in tickers}, index=basket.index
    ).sort_index()
    return basket


# ── Warmup loader ─────────────────────────────────────────────────────────────

def warmup_daily_buffers(
    client,
    instrument_map: dict,
    calendar_days: int = WARMUP_CALENDAR_DAYS,
) -> dict:
    now     = datetime.now(IST)
    from_dt = now - timedelta(days=calendar_days)
    to_dt   = now

    buffers = {}
    for ticker, info in instrument_map.items():
        logger.info(f"  Warming up daily {ticker}…")
        df = fetch_daily_historical(client, info["segment_id"], info["token"], ticker, from_dt, to_dt)
        buf = DailyTickerBuffer(ticker)
        if df.empty:
            logger.warning(f"  {ticker}: no daily history returned")
        else:
            buf.seed(df)
            logger.info(f"  {ticker}: {buf.n_bars} daily bars seeded")
        buffers[ticker] = buf

    return buffers