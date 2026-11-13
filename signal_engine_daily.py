"""
signal_engine_daily.py — Per-basket signal evaluation on daily OHLCV.

Identical OR-gate logic to the 5-min signal_engine.py; the only
difference is it calls build_basket_daily_ohlc() instead of
build_basket_5m_ohlc().

Basket info dict format:
    {basket_id: {"tickers": [...], "symbols": [...], "sectors": [...]}}
"""

import logging
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config_daily import (
    EMA_FAST, EMA_SLOW, ROLLING_WINDOW, MIN_ROLLING_POINTS,
    POSITION_SIZE, MIN_ALIGNED_DAYS,
)
from data_manager_daily import build_basket_daily_ohlc, DailyTickerBuffer

# Reuse parent indicator engine unchanged
from indicators import IndicatorCache

logger = logging.getLogger(__name__)


class DailySignalEngine:
    """
    Manages daily indicator state for all baskets.
    Called once per day at EOD after today's bar is appended.
    """

    def __init__(self, basket_info: dict, ticker_buffers: dict,
                 instrument_map: dict, position_size: float = POSITION_SIZE):
        self.basket_info    = basket_info
        self.ticker_buffers = ticker_buffers   # {ticker: DailyTickerBuffer}
        self.instrument_map = instrument_map
        self.position_size  = position_size

        self._caches: dict      = {bid: IndicatorCache(bid) for bid in basket_info}
        self._basket_ohlc: dict = {}

        # Pre-warm indicator caches from already-seeded buffers so that
        # warmup_status() reflects the true readiness at startup.
        self._prewarm_caches()

    # ── Startup pre-warm ──────────────────────────────────────────────────────

    def _prewarm_caches(self):
        """
        Run indicator computation for every basket using the already-seeded
        ticker buffers.  Called once at __init__ so warmup_status() reports
        the correct number of ready baskets immediately after startup.
        """
        ready = 0
        for bid, info in self.basket_info.items():
            tickers = info["tickers"]
            ohlc = build_basket_daily_ohlc(self.ticker_buffers, tickers, self.position_size)
            if ohlc is None or ohlc.empty:
                continue
            self._basket_ohlc[bid] = ohlc
            if self._caches[bid].update(ohlc["Close"]):
                ready += 1
        logger.info(f"  Pre-warmed indicators: {ready}/{len(self.basket_info)} baskets ready")

    # ── EOD evaluation ────────────────────────────────────────────────────────

    def on_day_close(self, day_date: pd.Timestamp, since_date=None) -> dict:
        """
        Called once per day after today's bar is appended to all buffers.

        since_date: calendar date of the last bar already evaluated for entries
                    (None = never).  A basket only emits an entry when its
                    latest bar is NEWER than this, so a holiday, a missing
                    bar or a restart can never re-issue yesterday's signal.

        Entry signals are the backtest's accepted entries: the OR-gate buy fires
        AND the basket's virtual sequential trade (run_or_gate_strategy) is flat.

        Returns:
            entry_signals  : [(basket_id, entry_type), ...]
            basket_closes  : {basket_id: float}
            indicator_ready: {basket_id: bool}
            latest_bar_date: newest bar date seen across baskets (or None)
        """
        entry_signals = []
        basket_closes = {}
        ready_map     = {}
        latest_bar    = None

        for bid, info in self.basket_info.items():
            tickers = info["tickers"]

            ohlc = build_basket_daily_ohlc(self.ticker_buffers, tickers, self.position_size)
            if ohlc is None or ohlc.empty:
                ready_map[bid] = False
                continue
            self._basket_ohlc[bid] = ohlc

            cache = self._caches[bid]
            ready = cache.update(ohlc["Close"])
            ready_map[bid] = ready
            if not ready:
                continue

            basket_closes[bid] = float(ohlc["Close"].iloc[-1])

            sig = cache.latest_signal()
            if not sig:
                continue

            bar_date = pd.Timestamp(sig["bar_time"]).date()
            latest_bar = bar_date if latest_bar is None else max(latest_bar, bar_date)
            if since_date is not None and bar_date <= since_date:
                continue          # no new bar for this basket — nothing new to act on

            if sig.get("entry_accepted"):
                entry_type = sig.get("accepted_type", "")
                entry_signals.append((bid, entry_type))
                logger.info(f"  SIGNAL ENTRY B{bid} ({entry_type}) on bar {bar_date}")
            elif sig.get("buy_signal"):
                logger.info(f"  B{bid} buy signal ignored: basket's virtual trade still open "
                            f"(backtest rule) on bar {bar_date}")

        return {
            "entry_signals":   entry_signals,
            "basket_closes":   basket_closes,
            "indicator_ready": ready_map,
            "latest_bar_date": latest_bar,
        }

    def exit_signal_since(self, basket_id: int, entry_time) -> tuple | None:
        """
        (bar_time, exit_type) of the first OR-gate exit on/after the entry day,
        or None.  See IndicatorCache.first_exit_since().
        """
        cache = self._caches.get(basket_id)
        if cache is None:
            return None
        return cache.first_exit_since(entry_time)

    def latest_bar_date(self):
        """Newest bar date across all baskets' current OHLC (or None)."""
        dates = [o.index[-1].date() for o in self._basket_ohlc.values() if not o.empty]
        return max(dates) if dates else None

    def latest_component_closes(self) -> dict:
        """{ticker: last close} across all baskets — for accurate MTM."""
        out = {}
        for ohlc in self._basket_ohlc.values():
            comp = ohlc.attrs.get("component_close")
            if comp is not None and not comp.empty:
                out.update({t: float(v) for t, v in comp.iloc[-1].items()})
        return out

    @property
    def basket_ohlc(self) -> dict:
        return self._basket_ohlc

    def get_exec_prices(self, open_prices: dict) -> dict:
        """
        Build {basket_id: {ticker: open_price}} from today's open prices.
        open_prices: {ticker: float} fetched at 09:17 from 1-min bar.
        """
        result = {}
        for bid, info in self.basket_info.items():
            tickers = info["tickers"]
            prices  = {t: open_prices[t] for t in tickers if t in open_prices}
            if len(prices) == len(tickers):
                result[bid] = prices
        return result

    def get_basket_close_prices(self) -> dict:
        result = {}
        for bid, ohlc in self._basket_ohlc.items():
            if not ohlc.empty:
                result[bid] = float(ohlc["Close"].iloc[-1])
        return result

    def n_ready_baskets(self) -> int:
        return sum(1 for c in self._caches.values() if c.signals is not None)

    def warmup_status(self) -> str:
        return (f"{self.n_ready_baskets()}/{len(self.basket_info)} baskets ready "
                f"(need {MIN_ROLLING_POINTS} daily bars)")


def build_basket_info(config: dict) -> dict:
    """Extract {basket_id: {"tickers", "symbols", "sectors"}} from basket config."""
    members = config["members"]
    info = {}
    for bid, grp in members.groupby("basket_id"):
        grp = grp.sort_values("stock_position")
        info[int(bid)] = {
            "tickers":   grp["ticker"].tolist(),
            "symbols":   grp["symbol"].tolist(),
            "sectors":   grp["sector"].tolist() if "sector" in grp.columns else [],
            "companies": grp["company_name"].tolist() if "company_name" in grp.columns else [],
        }
    return info
