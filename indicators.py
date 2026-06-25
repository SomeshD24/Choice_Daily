"""
indicators.py — Rolling OLS regression bands + EMA crossover on 5-min bars.

Logic is identical to strategy1_dualbasket1.py; adapted to work on a
pandas Series of any length (live rolling buffer instead of full history).

Key invariant (no lookahead):
  Signal at bar[i-1] → execution at bar[i] open.
  All indicator functions compute values THROUGH bar i and return full Series;
  the caller reads signal at index [-2] (i.e. "previous bar").
"""

import numpy as np
import pandas as pd
from scipy import stats

from config import (
    ROLLING_WINDOW, MIN_ROLLING_POINTS,
    EMA_FAST, EMA_SLOW, MIN_SLOPE_PCT,
)


# ── Rolling OLS regression bands ─────────────────────────────────────────────

def rolling_regression_bands(close: pd.Series, window: int = ROLLING_WINDOW,
                              min_points: int = MIN_ROLLING_POINTS) -> pd.DataFrame:
    """
    Rolling OLS regression bands.  Identical to backtest implementation.
    Returns DataFrame: trend_line, std_res, lower2, upper1, upper2.
    """
    close = close.dropna()
    n = len(close)
    trend   = np.full(n, np.nan)
    std_res = np.full(n, np.nan)

    for i in range(min_points - 1, n):
        start = max(0, i - window + 1)
        y     = close.iloc[start:i + 1].values.astype(float)
        if len(y) < min_points:
            continue
        x      = np.arange(len(y), dtype=float)
        sl, ic, *_ = stats.linregress(x, y)
        fitted      = sl * x + ic
        trend[i]    = fitted[-1]
        std_res[i]  = (y - fitted).std()

    return pd.DataFrame({
        "trend_line": trend,
        "std_res":    std_res,
        "lower2":     trend - 2 * std_res,
        "upper1":     trend + 1 * std_res,
        "upper2":     trend + 2 * std_res,
    }, index=close.index)


# ── EMA crossover signals ─────────────────────────────────────────────────────

def ema_crossover_signals(close: pd.Series,
                           ema_fast: int = EMA_FAST,
                           ema_slow: int = EMA_SLOW) -> pd.DataFrame:
    """
    EMA fast/slow crossover.
    buy_signal[i]  = crossover confirmed at bar[i-1] → execute at bar[i] open
    sell_signal[i] = death-cross confirmed at bar[i-1] → execute at bar[i] open
    """
    ema_f = close.ewm(span=ema_fast, adjust=False).mean()
    ema_s = close.ewm(span=ema_slow, adjust=False).mean()

    buy = (
        (ema_f.shift(2) <  ema_s.shift(2)) &
        (ema_f.shift(1) >= ema_s.shift(1)) &
        (close.shift(1) >  ema_s.shift(1))
    )
    sell = (
        (ema_f.shift(2) >  ema_s.shift(2)) &
        (ema_f.shift(1) <= ema_s.shift(1))
    )
    return pd.DataFrame({
        "Close":       close,
        "EMA_fast":    ema_f,
        "EMA_slow":    ema_s,
        "buy_signal":  buy.fillna(False),
        "sell_signal": sell.fillna(False),
    }, index=close.index)


# ── OR-gate combined signals ──────────────────────────────────────────────────

def or_gate_combined_signals(close: pd.Series,
                              bands: pd.DataFrame,
                              ema_sig: pd.DataFrame) -> pd.DataFrame:
    """
    Combine regression-band and EMA crossover into OR-gate buy signal.
    Exit: regression +2σ level OR trailing SL (trailing SL tracked live
    in portfolio engine, not here).

    Identical to backtest; works on 5-min close series.
    """
    yest_close  = close.shift(1)
    prev_close  = close.shift(2)
    yest_lower2 = bands["lower2"].shift(1)
    prev_lower2 = bands["lower2"].shift(2)
    yest_upper2 = bands["upper2"].shift(1)
    prev_upper2 = bands["upper2"].shift(2)

    reg_buy  = yest_lower2.notna() & (yest_close >= yest_lower2) & (prev_close < prev_lower2)
    reg_sell = yest_upper2.notna() & (yest_close >= yest_upper2) & (prev_close < prev_upper2)
    ema_buy  = ema_sig["buy_signal"]
    ema_sell = ema_sig["sell_signal"]

    reg_buy_f  = reg_buy.fillna(False)
    ema_buy_f  = ema_buy.fillna(False)
    reg_sell_f = reg_sell.fillna(False)
    ema_sell_f = ema_sell.fillna(False)

    buy_signal  = reg_buy_f | ema_buy_f
    sell_signal = reg_sell_f | ema_sell_f

    entry_type = np.where(reg_buy_f, "regression", np.where(ema_buy_f, "ema_crossover", ""))
    exit_type  = np.where(reg_sell_f, "regression_2sd", np.where(ema_sell_f, "ema_death_cross", ""))

    out = ema_sig.copy()
    out["buy_signal"]  = buy_signal
    out["sell_signal"] = sell_signal
    out["entry_type"]  = entry_type
    out["exit_type"]   = exit_type
    return out


# ── Incremental indicator snapshot ────────────────────────────────────────────

class IndicatorCache:
    """
    Caches computed indicators for a basket.  Recomputes from scratch when
    a new bar is appended (acceptable for 5-min: ~10ms for 756-bar window).

    For large basket counts, consider incremental EMA update; regression
    recompute is unavoidable.
    """

    def __init__(self, basket_id):
        self.basket_id = basket_id
        self._bands: pd.DataFrame | None = None
        self._signals: pd.DataFrame | None = None
        self._last_len: int = 0

    def update(self, close: pd.Series) -> bool:
        """
        Recompute indicators on the tail of the close series.
        Returns True if update happened and signals are valid.

        Performance note: only the last (ROLLING_WINDOW + MIN_ROLLING_POINTS)
        bars are needed for a valid signal at the current bar:
          - OLS uses at most a window of ROLLING_WINDOW bars.
          - EMA with span=EMA_SLOW=100 converges in ~500 bars (0.98^500 ≈ 10^-9).
          - Passing a tail avoids recomputing 8000+ historical OLS regressions
            on every bar close.
        """
        if len(close) == self._last_len:
            return self._signals is not None
        if len(close) < MIN_ROLLING_POINTS:
            return False

        # Use only the tail — enough for full indicator accuracy
        tail_len = ROLLING_WINDOW + MIN_ROLLING_POINTS   # = 756 + 500 = 1256
        close_tail = close.iloc[-tail_len:] if len(close) > tail_len else close

        self._bands   = rolling_regression_bands(close_tail)
        ema_sig       = ema_crossover_signals(close_tail)
        self._signals = or_gate_combined_signals(close_tail, self._bands, ema_sig)
        self._last_len = len(close)
        return True

    @property
    def bands(self) -> pd.DataFrame | None:
        return self._bands

    @property
    def signals(self) -> pd.DataFrame | None:
        return self._signals

    def latest_signal(self) -> dict:
        """
        Return signal state at the CURRENT bar (index [-1]).
        Caller checks index[-2] for "previous bar" signal — see evaluate_signal().
        """
        if self._signals is None or len(self._signals) < 2:
            return {}
        return {
            "buy_signal":  bool(self._signals["buy_signal"].iloc[-1]),
            "sell_signal": bool(self._signals["sell_signal"].iloc[-1]),
            "entry_type":  str(self._signals["entry_type"].iloc[-1]),
            "exit_type":   str(self._signals["exit_type"].iloc[-1]),
            "close":       float(self._signals["Close"].iloc[-1]),
            "ema_fast":    float(self._signals["EMA_fast"].iloc[-1]),
            "ema_slow":    float(self._signals["EMA_slow"].iloc[-1]),
        }

    def prev_bar_signal(self) -> dict:
        """
        Signal that should trigger an order on the NEXT bar open.
        Reads at index[-2] (previous confirmed bar).

        In live 5-min trading:
          bar N closes → eval prev_bar_signal at [-1] of NEXT update
          (after bar N+1 opens, its first tick becomes current bar,
           so bar N is at [-2]).

        Simpler approach used here: after bar N closes, call update() with
        the buffer including bar N. Then read index[-1] as "bar N signal".
        The runner acts on this signal at bar N+1 open.
        """
        if self._signals is None or len(self._signals) < 1:
            return {}
        last = self._signals.iloc[-1]
        return {
            "buy_signal":  bool(last["buy_signal"]),
            "sell_signal": bool(last["sell_signal"]),
            "entry_type":  str(last["entry_type"]),
            "exit_type":   str(last["exit_type"]),
            "close":       float(last["Close"]),
            "bar_time":    self._signals.index[-1],
        }
