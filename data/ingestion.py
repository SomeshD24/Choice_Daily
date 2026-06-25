"""
Data Ingestion Module
─────────────────────
Fetches OHLCV data for stock + index using yfinance.
All data is adjusted close based. Strictly no lookahead.

Convention: all returned dataframes are indexed by Date,
columns = [Open, High, Low, Close, Volume, Adj_Close]
"""

import yfinance as yf
import pandas as pd
import numpy as np
from typing import Optional
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


# ── Index map: exchange/region → benchmark index ticker ──────────────────────
INDEX_MAP = {
    "nse":    "^NSEI",       # Nifty 50
    "bse":    "^BSESN",      # Sensex
    "nyse":   "^GSPC",       # S&P 500
    "nasdaq": "^IXIC",       # NASDAQ Composite
    "lse":    "^FTSE",       # FTSE 100
    "default": "^GSPC",      # fallback
}


def fetch_ohlcv(
    ticker: str,
    start: str,
    end: Optional[str] = None,
    interval: str = "1d",
) -> pd.DataFrame:
    """
    Fetch OHLCV for a single ticker.

    Parameters
    ----------
    ticker  : yfinance-compatible ticker string e.g. 'RELIANCE.NS', 'AAPL'
    start   : ISO date string 'YYYY-MM-DD'
    end     : ISO date string 'YYYY-MM-DD', defaults to today
    interval: '1d' for daily (only daily supported in this system)

    Returns
    -------
    DataFrame with columns: Open High Low Close Volume Adj_Close
    Index: DatetimeIndex (timezone-naive, date only)

    Lookahead note
    --------------
    Raw fetch returns data up to `end`. The shift-by-1 rule is enforced
    at the feature engineering stage, NOT here. This module only fetches.
    """
    logger.info(f"Fetching {ticker} from {start} to {end or 'today'}")

    raw = yf.download(
        ticker,
        start=start,
        end=end,
        interval=interval,
        auto_adjust=False,   # keep Adj Close separate so we know exactly what we use
        progress=False,
    )

    if raw.empty:
        raise ValueError(f"No data returned for ticker: {ticker}. Check ticker symbol.")

    # Flatten multi-level columns if present
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)

    # Standardise column names
    raw = raw.rename(columns={"Adj Close": "Adj_Close"})

    # Keep only what we need
    cols = [c for c in ["Open", "High", "Low", "Close", "Volume", "Adj_Close"] if c in raw.columns]
    df = raw[cols].copy()

    # Drop rows where Adj_Close is NaN (holidays, suspensions)
    df = df.dropna(subset=["Adj_Close"])

    # Timezone-naive date index
    df.index = pd.to_datetime(df.index).normalize()
    df.index.name = "Date"

    logger.info(f"  → {len(df)} trading days fetched for {ticker}")
    return df


def fetch_stock_and_index(
    ticker: str,
    index_key: str = "default",
    start: str = "2018-01-01",
    end: Optional[str] = None,
    custom_index_ticker: Optional[str] = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Fetch both stock and its reference index.

    Parameters
    ----------
    ticker              : stock ticker e.g. 'TCS.NS'
    index_key           : key from INDEX_MAP e.g. 'nse', 'nyse'
    start, end          : date range
    custom_index_ticker : override INDEX_MAP with a specific ticker

    Returns
    -------
    (stock_df, index_df) — both aligned to same trading dates
    """
    index_ticker = custom_index_ticker or INDEX_MAP.get(index_key, INDEX_MAP["default"])

    stock_df = fetch_ohlcv(ticker, start=start, end=end)
    index_df = fetch_ohlcv(index_ticker, start=start, end=end)

    # Align to common trading dates (inner join on date index)
    common_dates = stock_df.index.intersection(index_df.index)
    stock_df = stock_df.loc[common_dates]
    index_df = index_df.loc[common_dates]

    if len(stock_df) < 100:
        raise ValueError(
            f"Only {len(stock_df)} common trading days found. "
            "Need at least 100 for reliable z-score computation. "
            "Extend the start date."
        )

    logger.info(
        f"Aligned {ticker} + {index_ticker}: "
        f"{len(stock_df)} common trading days "
        f"({common_dates[0].date()} → {common_dates[-1].date()})"
    )
    return stock_df, index_df


def fetch_india_vix(
    start: str,
    end: Optional[str] = None,
    ticker: str = "^INDIAVIX",
) -> pd.DataFrame:
    """
    Fetch India VIX (^INDIAVIX) — NSE's fear/volatility gauge.

    India VIX measures expected 30-day volatility of Nifty 50 options.
    High VIX → fear, uncertainty → dampen bullish signals.
    Low VIX  → complacency, calm → market may be trending reliably.

    Returns
    -------
    DataFrame with columns: [Close]  (VIX level, in percentage points)
    Index: DatetimeIndex (timezone-naive, date only)

    Note: VIX is not price-based — no volume, no Adj_Close.
    We only use the Close (daily settlement level).
    Returns empty DataFrame if fetch fails (VIX data optional).
    """
    logger.info(f"Fetching India VIX ({ticker}) from {start} to {end or 'today'}")
    try:
        raw = yf.download(
            ticker,
            start=start,
            end=end,
            interval="1d",
            auto_adjust=False,
            progress=False,
        )
        if raw.empty:
            logger.warning(f"India VIX ({ticker}): no data returned. Skipping.")
            return pd.DataFrame()

        # Flatten multi-level columns if present
        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = raw.columns.get_level_values(0)

        df = raw[["Close"]].copy()
        df = df.dropna(subset=["Close"])
        df.index = pd.to_datetime(df.index).normalize()
        df.index.name = "Date"
        df.columns = ["india_vix"]

        logger.info(f"  → {len(df)} trading days fetched for India VIX")
        return df

    except Exception as e:
        logger.warning(f"India VIX fetch failed: {e}. Proceeding without VIX.")
        return pd.DataFrame()

def validate_data(df: pd.DataFrame, ticker: str) -> pd.DataFrame:
    """
    Basic data quality checks.
    - Detects and removes zero/negative prices
    - Detects extreme single-day moves (possible bad ticks)
    - Reports missing dates
    """
    original_len = len(df)

    # Remove zero or negative prices
    df = df[df["Adj_Close"] > 0]

    # Flag extreme returns (>50% single day) as suspect
    log_ret = np.log(df["Adj_Close"] / df["Adj_Close"].shift(1))
    suspect = log_ret.abs() > 0.5
    if suspect.any():
        logger.warning(
            f"{ticker}: {suspect.sum()} extreme daily moves (>50%) detected. "
            "These may be data errors or genuine events. Keeping but flagging."
        )

    removed = original_len - len(df)
    if removed > 0:
        logger.warning(f"{ticker}: Removed {removed} rows with invalid prices.")

    return df
