"""
data_manager.py — Fetch 5-min candles directly from Choice FinX (kkunal library)
and maintain per-ticker rolling buffers.
"""

import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from config import (
    IST, WARMUP_BARS, WARMUP_DAYS, MIN_ALIGNED_BARS,
    CHOICE_CHART_INTERVAL, BASKET_QUANTITIES_PATH,
)
from choice_api import ChoiceClient

logger = logging.getLogger(__name__)


# ── Single-instrument fetch ───────────────────────────────────────────────────

def fetch_5m_historical(
    client: ChoiceClient,
    segment_id: int,
    token: int,
    from_dt: datetime,
    to_dt: datetime,
    max_retries: int = 5,
) -> pd.DataFrame:
    """
    Fetch 5-min OHLCV bars for one instrument from Choice FinX using kkunal.
    Returns DataFrame with IST-aware DatetimeIndex and columns
    Open/High/Low/Close/Volume. Empty DataFrame on failure.
    """
    from_str = from_dt.strftime("%Y-%m-%d %H:%M:%S")
    to_str = to_dt.strftime("%Y-%m-%d %H:%M:%S")

    for attempt in range(max_retries):
        try:
            df = client.historical.get_historical_data(
                segment_id=segment_id,
                token=token,
                from_date=from_str,
                to_date=to_str,
                resolution=CHOICE_CHART_INTERVAL,
            )
            if df is None or df.empty:
                return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

            df = df.copy()
            if "Time" in df.columns:
                df["Time"] = pd.to_datetime(df["Time"])
                if df["Time"].dt.tz is None:
                    df["Time"] = df["Time"].dt.tz_localize(IST, nonexistent="shift_forward", ambiguous="NaT")
                else:
                    df["Time"] = df["Time"].dt.tz_convert(IST)
                df.set_index("Time", inplace=True)
            elif df.index.tz is None:
                df.index = df.index.tz_localize(IST, nonexistent="shift_forward", ambiguous="NaT")
            else:
                df.index = df.index.tz_convert(IST)

            df.index.name = None
            if df.index.has_duplicates:
                df = df[~df.index.duplicated(keep="last")]

            cols = ["Open", "High", "Low", "Close", "Volume"]
            for c in cols:
                if c not in df.columns:
                    df[c] = 0.0

            return df[cols].apply(pd.to_numeric, errors="coerce").dropna(subset=["Close"]).sort_index()

        except Exception as e:
            if "429" in str(e) or "Too Many Requests" in str(e):
                if attempt < max_retries - 1:
                    sleep_time = 2 ** attempt
                    logger.warning(f"Rate limited (429) for segment={segment_id} token={token}. Retrying in {sleep_time}s...")
                    time.sleep(sleep_time)
                    continue
            logger.error(f"Historical 5m fetch failed for segment={segment_id} token={token}: {e}")
            break

    return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])


# ── Rolling buffer ────────────────────────────────────────────────────────────

class TickerBuffer:
    """
    Maintains a rolling buffer of completed 5-min bars for one ticker.
    Keeps at most MAX_BARS bars (oldest trimmed on append).
    """
    MAX_BARS = WARMUP_BARS + 50

    def __init__(self, ticker: str):
        self.ticker = ticker
        self._df: pd.DataFrame = pd.DataFrame(
            columns=["Open", "High", "Low", "Close", "Volume"]
        )

    def seed(self, df_5m: pd.DataFrame):
        """Initialize buffer with historical 5-min bars (at startup)."""
        self._df = df_5m.copy().tail(self.MAX_BARS)

    def append_bar(self, bar: dict):
        """
        Append one completed 5-min bar.
        bar must contain: datetime (pd.Timestamp), Open, High, Low, Close, Volume.
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

    @property
    def df(self) -> pd.DataFrame:
        return self._df.copy()

    @property
    def n_bars(self) -> int:
        return len(self._df)


# ── Basket OHLC builder ───────────────────────────────────────────────────────

def build_basket_5m_ohlc(
    ticker_buffers: dict,       # {yf_ticker: TickerBuffer}
    tickers: list[str],
    position_size: float = 10_000_000,
) -> pd.DataFrame | None:
    """
    Build an equal-weight basket 5-min OHLC DataFrame from per-ticker buffers.
    Uses fixed component quantities from basket_quantities_6.json.
    """
    if not all(t in ticker_buffers for t in tickers):
        return None

    dfs = {}
    for t in tickers:
        buf = ticker_buffers[t].df
        if buf.empty:
            return None
        dfs[t] = buf

    closes = pd.DataFrame({t: dfs[t]["Close"] for t in tickers}).dropna()
    if len(closes) < MIN_ALIGNED_BARS:
        return None

    _candidates = [
        Path(BASKET_QUANTITIES_PATH),
        Path(__file__).resolve().parent / "data" / "basket_quantities_6.json",
        Path(__file__).resolve().parent / "state" / "basket_quantities_6.json",
        Path.cwd() / "data" / "basket_quantities_6.json",
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
            quantities = {t: float(q_map[t]) for t in tickers}
            break

    if quantities is None:
        logger.error(
            "No basket in %s matches tickers %s",
            q_file, sorted(tickers),
        )
        return None

    common_idx = closes.index
    basket = pd.DataFrame(index=common_idx)

    for field in ["Open", "High", "Low", "Close"]:
        basket[field] = sum(
            dfs[t][field].reindex(common_idx).astype(float) * quantities[t]
            for t in tickers
        )
    basket["Volume"] = sum(
        dfs[t]["Volume"].reindex(common_idx).astype(float)
        for t in tickers
    )
    basket.dropna(inplace=True)

    basket.attrs["component_close"] = pd.DataFrame(
        {t: dfs[t]["Close"].reindex(basket.index) for t in tickers}
    )
    basket.attrs["component_open"] = pd.DataFrame(
        {t: dfs[t]["Open"].reindex(basket.index) for t in tickers}
    )
    return basket


# ── Warm-up loader ─────────────────────────────────────────────────────────────

class SessionExpiredError(PermissionError):
    """Raised when the broker session is rejected (HTTP 401) during warmup."""


def warmup_ticker_buffers(
    client: ChoiceClient,
    instrument_map: dict,       # {yf_ticker: {"segment_id": int, "token": int}}
    warmup_days: int = WARMUP_DAYS,
) -> dict:
    """
    Fetch historical 5-min data for all tickers and seed TickerBuffer objects.
    Returns {yf_ticker: TickerBuffer}.
    """
    now = datetime.now(IST)
    from_dt = now - timedelta(days=warmup_days * 2)
    to_dt = now

    buffers: dict[str, TickerBuffer] = {}
    consecutive_401s = 0
    _AUTH_FAIL_THRESHOLD = 3

    for ticker, info in instrument_map.items():
        logger.info(f"  Warming up {ticker} (segment={info['segment_id']} token={info['token']})…")

        try:
            df_5m = fetch_5m_historical(
                client, info["segment_id"], info["token"], from_dt, to_dt,
            )
            time.sleep(0.1)
        except PermissionError as e:
            consecutive_401s += 1
            logger.error(f"ChartData 401 for {ticker}: {e}")
            if consecutive_401s >= _AUTH_FAIL_THRESHOLD:
                raise SessionExpiredError(
                    f"Got {consecutive_401s} consecutive 401 errors. Session is invalid."
                ) from e
            df_5m = pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
        except Exception as e:
            consecutive_401s = 0
            logger.error(f"Fetch failed for {ticker}: {e}")
            df_5m = pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

        buf = TickerBuffer(ticker)
        if df_5m.empty:
            logger.warning(f"  {ticker}: no historical data returned")
        else:
            buf.seed(df_5m)
            logger.info(f"  {ticker}: {buf.n_bars} 5-min bars seeded")
        buffers[ticker] = buf

    return buffers


# ── Live polling ───────────────────────────────────────────────────────────────

class LiveBarPoller:
    """
    Polls Choice FinX for new 5-min bars since last poll and appends
    completed bars to TickerBuffers.
    """

    def __init__(self, client: ChoiceClient, instrument_map: dict,
                 ticker_buffers: dict, lookback_bars: int = 5):
        self.client = client
        self.instrument_map = instrument_map
        self.ticker_buffers = ticker_buffers
        self.lookback_bars = lookback_bars
        self._last_poll: dict[str, pd.Timestamp] = {}

    def poll_and_update(self, now: datetime) -> dict[str, pd.DataFrame]:
        """
        Fetch latest 5-min bars for all tickers and append new completed ones.
        Returns {ticker: new_bars_df}.
        """
        from config import BAR_MINUTES

        new_5m: dict[str, pd.DataFrame] = {}
        window_start = now - timedelta(minutes=BAR_MINUTES * (self.lookback_bars + 2))

        for ticker, info in self.instrument_map.items():
            df_5m = fetch_5m_historical(
                self.client, info["segment_id"], info["token"], window_start, now,
            )
            time.sleep(0.15)
            if df_5m.empty:
                continue

            now_ts = pd.Timestamp(now)
            if now_ts.tz is None:
                now_ts = now_ts.tz_localize(IST)
            elif str(now_ts.tz) != str(IST):
                now_ts = now_ts.tz_convert(IST)

            complete = df_5m[
                df_5m.index + pd.Timedelta(minutes=BAR_MINUTES) <= now_ts
            ]
            last_seen = self._last_poll.get(ticker)
            if last_seen is not None:
                complete = complete[complete.index > last_seen]
            if complete.empty:
                continue

            buf = self.ticker_buffers[ticker]
            for ts, row in complete.iterrows():
                bar = {
                    "datetime": ts,
                    "Open": float(row["Open"]),
                    "High": float(row["High"]),
                    "Low": float(row["Low"]),
                    "Close": float(row["Close"]),
                    "Volume": float(row["Volume"]),
                }
                buf.append_bar(bar)

            self._last_poll[ticker] = complete.index[-1]
            new_5m[ticker] = complete

        return new_5m