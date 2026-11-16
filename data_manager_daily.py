"""
data_manager_daily.py — Daily OHLCV data via yfinance and Choice FinX (kkunal library).

Key features:
  - Uses yfinance as the primary source for full historical depth (756+ trading days / 3.5 years).
  - Uses kkunal HistoricalAPI for today's bar, live LTP, and open price fetching.
  - EOD update: called at 15:35; appends today's completed daily bar.
  - Open-price fetch: called at 09:17 via 1-min bar for execution price.
  - Basket OHLC built from daily buffers using fixed backtest component quantities.
"""

import json
import logging
import sys
import types
from datetime import datetime, timedelta, time as dtime
from pathlib import Path

import numpy as np
import pandas as pd

from config_daily import (
    IST, MARKET_OPEN_H, MARKET_OPEN_M, EOD_EVAL_H, EOD_EVAL_M,
    WARMUP_TRADING_DAYS, WARMUP_CALENDAR_DAYS,
    MIN_ALIGNED_DAYS, POSITION_SIZE,
    BASKET_QUANTITIES_PATH,
)

# Prevent ModuleNotFoundError: No module named 'websocket' if websocket-client is not yet installed
try:
    import websocket
except ImportError:
    sys.modules["websocket"] = types.ModuleType("websocket")

logger = logging.getLogger(__name__)


# ── Chart data helper via kkunal ──────────────────────────────────────────────

def _fetch_choice_chart_data(
    client,
    segment_id: int,
    token: int,
    from_dt: datetime,
    to_dt: datetime,
    interval: str = "D",
) -> pd.DataFrame:
    """
    Fetch OHLCV chart data using kkunal ChoiceClient.historical.
    Returns DataFrame indexed by IST-aware Timestamp with columns
    Open, High, Low, Close, Volume. Empty DataFrame on failure.
    """
    try:
        from_str = from_dt.strftime("%Y-%m-%d %H:%M:%S")
        to_str = to_dt.strftime("%Y-%m-%d %H:%M:%S")

        df = client.historical.get_historical_data(
            segment_id=segment_id,
            token=token,
            from_date=from_str,
            to_date=to_str,
            resolution=interval,
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
        logger.debug(f"Choice chart fetch failed for segment={segment_id} token={token}: {e}")
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])


# ── Daily OHLCV fetch ─────────────────────────────────────────────────────────

def fetch_daily_historical(
    client,
    segment_id: int,
    token: int,
    yf_ticker: str,
    from_dt: datetime,
    to_dt: datetime,
) -> pd.DataFrame:
    """
    Fetch daily historical OHLCV. Uses yfinance as the primary source for full
    historical depth (756+ trading days / 3.5 years), ensuring clean and robust daily series.
    """
    import yfinance as yf

    # 1. Fetch from yfinance (guarantees multi-year daily history)
    try:
        df = yf.download(
            yf_ticker,
            start=from_dt.strftime("%Y-%m-%d"),
            end=(to_dt + pd.Timedelta(days=1)).strftime("%Y-%m-%d"),
            progress=False,
        )
        if not df.empty:
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            if df.index.tz is None:
                df.index = df.index.tz_localize(IST)
            else:
                df.index = df.index.tz_convert(IST)
            df.index.name = None
            cols = ["Open", "High", "Low", "Close", "Volume"]
            for c in cols:
                if c not in df.columns:
                    df[c] = 0.0
            df = df[cols].apply(pd.to_numeric, errors="coerce").dropna(subset=["Close"]).sort_index()
            if not df.empty and len(df) > 50:
                return df
    except Exception as e:
        logger.warning(f"yfinance daily fetch failed for {yf_ticker}: {e}")

    # 2. Fallback to Choice FinX API if available
    if client is not None:
        try:
            df = _fetch_choice_chart_data(client, segment_id, token, from_dt, to_dt, interval="D")
            if not df.empty:
                return df
        except Exception as e:
            logger.debug(f"Choice API daily fetch failed for {yf_ticker}: {e}")

    return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])


def fetch_todays_open(
    client,
    segment_id: int,
    token: int,
) -> float | None:
    """Fetch today's open price at 09:15 via 1-minute intraday bar."""
    now = datetime.now(IST)
    from_dt = now.replace(hour=MARKET_OPEN_H, minute=MARKET_OPEN_M - 1, second=0, microsecond=0)
    to_dt = now

    df = _fetch_choice_chart_data(client, segment_id, token, from_dt, to_dt, interval="1")
    if df.empty:
        return None
    return float(df["Open"].iloc[0])


def fetch_live_ltp(
    client,
    segment_id: int,
    token: int,
) -> float | None:
    """Fetch the latest price (LTP) via 1-minute intraday bar."""
    now = datetime.now(IST)
    from_dt = now - timedelta(minutes=15)
    to_dt = now

    df = _fetch_choice_chart_data(client, segment_id, token, from_dt, to_dt, interval="1")
    if df.empty:
        return None
    return float(df["Close"].iloc[-1])


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

    def append_today(self, client, segment_id: int, token: int, yf_ticker: str,
                     lookback_days: int = 15) -> bool:
        """
        Append EVERY completed daily bar newer than the last buffered one
        (not only today's).  This picks up:
          - special sessions on non-weekdays (e.g. Budget-day Saturday
            2025-02-01, Muhurat trading) that the backtest data contains,
          - days missed while the engine was offline.
        Today's bar is only taken once the EOD evaluation time has passed
        (before that Yahoo's bar for today is still forming).
        Returns True if today's bar was appended.
        """
        now = datetime.now(IST)
        df = fetch_daily_historical(client, segment_id, token, yf_ticker,
                                    now - timedelta(days=lookback_days), now)
        if df.empty:
            return False

        today = now.date()
        eod_cutoff = now.replace(hour=EOD_EVAL_H, minute=EOD_EVAL_M, second=0, microsecond=0)
        last_date = self._df.index[-1].date() if len(self._df) else None

        new = df[df.index.date <= today]
        if now < eod_cutoff:
            new = new[new.index.date < today]

        # Re-write the last few buffered days too: a close taken at 15:35 can
        # still be the last traded price, and the data source later replaces it
        # with the official close.  append_bar() keeps the newest value per date.
        refresh_from = (pd.Timestamp(last_date) - pd.Timedelta(days=5)).date() if last_date else None
        if refresh_from is not None:
            for ts, row in new[(new.index.date >= refresh_from) & (new.index.date <= last_date)].iterrows():
                self.append_bar({
                    "datetime": ts,
                    "Open": float(row["Open"]), "High": float(row["High"]),
                    "Low": float(row["Low"]), "Close": float(row["Close"]),
                    "Volume": float(row["Volume"]),
                })
        if last_date is not None:
            new = new[new.index.date > last_date]

        for ts, row in new.iterrows():
            self.append_bar({
                "datetime": ts,
                "Open": float(row["Open"]), "High": float(row["High"]),
                "Low": float(row["Low"]), "Close": float(row["Close"]),
                "Volume": float(row["Volume"]),
            })
        if len(new) > 1 or (len(new) == 1 and new.index[-1].date() != today):
            logger.info(f"  {yf_ticker}: filled {len(new)} bar(s) "
                        f"{[d.date().isoformat() for d in new.index]}")
        return bool(len(new)) and new.index[-1].date() == today

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
    Equal-weight basket daily OHLC matching strategy1_dualbasket2.py.
    Uses fixed component quantities from basket_quantities_6.json.
    """
    if not all(t in ticker_buffers for t in tickers):
        return None

    fields = ["Open", "High", "Low", "Close", "Volume"]
    dfs = {t: ticker_buffers[t].df for t in tickers}
    if any(d.empty for d in dfs.values()):
        return None

    loaded = [
        dfs[t][fields].rename(columns={c: (t, c) for c in fields})
        for t in tickers
    ]
    panel = pd.concat(loaded, axis=1).dropna()

    if len(panel) < MIN_ALIGNED_DAYS:
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
            quantities = q_map
            break

    if quantities is None:
        logger.error(
            "No basket in %s matches tickers %s.",
            q_file,
            sorted(tickers),
        )
        return None

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
    now = datetime.now(IST)
    from_dt = now - timedelta(days=calendar_days)
    to_dt = now

    # Before the EOD evaluation time, Yahoo's bar for today is still forming
    # (it moves with the live price).  Never seed indicators with it — today's
    # completed bar is appended by append_today() at EOD.
    eod_cutoff = now.replace(hour=EOD_EVAL_H, minute=EOD_EVAL_M, second=0, microsecond=0)
    drop_today = now.weekday() < 5 and now < eod_cutoff

    buffers = {}
    for ticker, info in instrument_map.items():
        logger.info(f"  Warming up daily {ticker}…")
        df = fetch_daily_historical(client, info["segment_id"], info["token"], ticker, from_dt, to_dt)
        if drop_today and not df.empty:
            df = df[df.index.date < now.date()]
        buf = DailyTickerBuffer(ticker)
        if df.empty:
            logger.warning(f"  {ticker}: no daily history returned")
        else:
            buf.seed(df)
            logger.info(f"  {ticker}: {buf.n_bars} daily bars seeded")
        buffers[ticker] = buf

    return buffers