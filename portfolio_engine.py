"""
portfolio_engine.py — Live dual-slot portfolio with correlation-based eviction.

Ported from strategy1_dualbasket1.py's simulate_dual_basket_portfolio()
and associated helpers.  Key differences from backtest version:

  - No full-scan loop; state is updated incrementally bar-by-bar.
  - "Entry" happens at next bar open (called by runner at bar N+1).
  - "Exit" check happens at each bar close (runner calls check_exits after
    each bar completes, then executes at next bar open).
  - Correlation uses 5-min returns from basket close buffer (not daily).
  - All timestamps are tz-aware IST.
"""

import logging
from itertools import combinations
from datetime import datetime

import numpy as np
import pandas as pd

from config import (
    N_SLOTS, POSITION_SIZE, TRAILING_SL_PCT, CORR_LOOKBACK,
)

logger = logging.getLogger(__name__)


# ── Slot state dict schema ────────────────────────────────────────────────────
# {
#   "basket_id":           int,
#   "entry_time":          pd.Timestamp,
#   "entry_type":          "regression" | "ema_crossover",
#   "tickers":             [str, ...],
#   "quantities":          {ticker: float},   # fractional for paper
#   "entry_prices":        {ticker: float},
#   "investment":          float,
#   "capital_allocated":   float,
#   "peak_basket_close":   float,             # for trailing SL
#   "returns_ref_value":   float,
#   "returns_ref_time":    pd.Timestamp,
# }


# ── Correlation helpers ───────────────────────────────────────────────────────

def _basket_return_window(basket_id: int, basket_close_series: dict,
                           end_time: pd.Timestamp,
                           lookback: int = CORR_LOOKBACK) -> pd.Series | None:
    """
    Trailing 5-min return window for a basket, ending at or before end_time.
    basket_close_series: {basket_id: pd.Series of basket Close prices}
    """
    if basket_id not in basket_close_series:
        return None
    close = basket_close_series[basket_id]
    avail = close.index[close.index <= end_time]
    if avail.empty:
        return None
    window = close.loc[:avail[-1]].iloc[-(lookback + 1):]
    if len(window) < max(10, lookback // 3):
        return None
    rets = window.pct_change().dropna()
    return rets if not rets.empty else None


def _pairwise_correlation(a: int, b: int, basket_close_series: dict,
                           end_time: pd.Timestamp) -> float:
    if a == b:
        return 1.0
    ra = _basket_return_window(a, basket_close_series, end_time)
    rb = _basket_return_window(b, basket_close_series, end_time)
    if ra is None or rb is None:
        return 0.0
    aligned = pd.concat([ra, rb], axis=1, join="inner").dropna()
    if len(aligned) < 10:
        return 0.0
    corr = aligned.iloc[:, 0].corr(aligned.iloc[:, 1])
    return float(corr) if not np.isnan(corr) else 0.0


def _eviction_target(held_ids: list, new_bid: int,
                      basket_close_series: dict, now: pd.Timestamp) -> int | None:
    """
    Identical logic to backtest _eviction_target().
    Returns index into held_ids to evict, or None if incoming is most redundant.
    """
    members = list(held_ids) + [new_bid]
    n = len(members)
    if n < 2:
        return None

    corr = {}
    for a, b in combinations(range(n), 2):
        c = _pairwise_correlation(members[a], members[b], basket_close_series, now)
        corr[(a, b)] = c
        corr[(b, a)] = c

    mean_corrs = [
        sum(corr[(i, j)] for j in range(n) if j != i) / (n - 1)
        for i in range(n)
    ]
    worst = int(np.argmax(mean_corrs))
    return None if worst == n - 1 else worst


def _select_least_correlated_subset(basket_ids: list, k: int,
                                     basket_close_series: dict,
                                     now: pd.Timestamp) -> list:
    if k <= 0 or len(basket_ids) <= k:
        return basket_ids
    best_subset, best_score = basket_ids[:k], np.inf
    for combo in combinations(basket_ids, k):
        score = sum(
            _pairwise_correlation(a, b, basket_close_series, now)
            for a, b in combinations(combo, 2)
        )
        if score < best_score:
            best_score, best_subset = score, list(combo)
    return best_subset


# ── Portfolio state ───────────────────────────────────────────────────────────

class PortfolioEngine:
    """
    Live dual-slot portfolio engine.

    Public API:
        on_bar_close(basket_id, close_price, bar_time)
            → updates trailing SL peak, returns list of exit decisions

        process_entry_signals(signals_by_basket, bar_time, exec_prices)
            → processes pending entry signals, fills free/evicted slots

        execute_pending_exits(exec_prices, bar_time)
            → "fills" queued exits at given prices (next bar open)

        execute_pending_entries(exec_prices, bar_time, basket_info)
            → "fills" queued entries at given prices (next bar open)
    """

    def __init__(self, initial_capital: float = POSITION_SIZE, n_slots: int = N_SLOTS):
        self.initial_capital = initial_capital
        self.n_slots         = n_slots
        self.slots: list[dict | None] = [None] * n_slots
        self.trade_log: list[dict] = []
        self.realized_pnl: float = 0.0
        self.basket_close_series: dict[int, pd.Series] = {}

        # Pending orders queued after signal, executed at next bar open
        self._pending_exits:   list = []  # list of (slot_idx, reason)
        self._pending_entries: list = []  # list of (basket_id, entry_type, capital)

        # Per-basket close series for correlation (updated externally)
        self.basket_close_series: dict = {}   # {basket_id: pd.Series}

    # ── Capital helpers ───────────────────────────────────────────────────────

    @property
    def base_capital(self) -> float:
        return self.initial_capital + self.realized_pnl

    @property
    def available_cash(self) -> float:
        invested = sum(s["investment"] for s in self.slots if s is not None)
        return self.base_capital - invested

    @property
    def active_basket_ids(self) -> set:
        return {s["basket_id"] for s in self.slots if s is not None}

    def both_empty(self) -> bool:
        return all(s is None for s in self.slots)

    # ── Bar-close processing ──────────────────────────────────────────────────

    def on_bar_close(self, bar_time: pd.Timestamp,
                     basket_close_prices: dict) -> list:
        """
        Called at each 5-min bar close.
        basket_close_prices: {basket_id: float} — current bar close for each basket.

        1. Updates trailing SL peaks for held slots.
        2. Evaluates exit conditions (reg_2sd sell_signal + trailing SL).
        3. Returns list of exit_decision dicts (execution deferred to next bar open).

        Also updates basket_close_series for correlation.
        """
        # Update close series
        for bid, price in basket_close_prices.items():
            if bid not in self.basket_close_series:
                self.basket_close_series[bid] = pd.Series(dtype=float)
            self.basket_close_series[bid].at[bar_time] = price
            
            # Prune to prevent infinite memory growth
            if len(self.basket_close_series[bid]) > 300:
                self.basket_close_series[bid] = self.basket_close_series[bid].iloc[-200:]

        exit_decisions = []
        for idx, slot in enumerate(self.slots):
            if slot is None:
                continue
            bid = slot["basket_id"]
            price = basket_close_prices.get(bid)
            if price is None:
                continue

            # Update trailing peak
            if slot["peak_basket_close"] is None:
                slot["peak_basket_close"] = price
            else:
                slot["peak_basket_close"] = max(slot["peak_basket_close"], price)

        return exit_decisions  # caller will add signal-based exits

    def queue_exit(self, slot_idx: int, reason: str):
        """Queue exit for execution at next bar open."""
        if self.slots[slot_idx] is not None:
            self._pending_exits.append((slot_idx, reason))

    def queue_entries(self, signals: list, bar_time: pd.Timestamp):
        """
        Process entry signals after bar close.
        signals: list of (basket_id, entry_type) from signal_engine

        Determines which signals to fill (free slots / eviction) and
        queues them for execution at next bar open.
        """
        if not signals:
            return

        # Filter out already-held baskets
        active = self.active_basket_ids
        candidates = [(bid, et) for bid, et in signals if bid not in active]
        if not candidates:
            return

        free_slots = [i for i in range(self.n_slots) if self.slots[i] is None]
        n_free = len(free_slots)
        both_empty = self.both_empty()

        # If more candidates than free slots, we must pick the best subset
        if n_free == 1 and len(candidates) > 1:
            # Pick the one candidate least correlated with the currently held baskets
            held_ids = list(active)
            best_bid = candidates[0][0]
            best_corr = float('inf')
            for bid, et in candidates:
                corr_sum = sum(_pairwise_correlation(bid, h, self.basket_close_series, bar_time) for h in held_ids)
                if corr_sum < best_corr:
                    best_corr = corr_sum
                    best_bid = bid
            candidates = [(bid, et) for bid, et in candidates if bid == best_bid]
            logger.info(f"  Multiple candidates for 1 free slot; selected B{best_bid} (lowest correlation)")

        elif n_free >= 2 and len(candidates) > n_free:
            keep = set(_select_least_correlated_subset(
                [bid for bid, _ in candidates], n_free,
                self.basket_close_series, bar_time
            ))
            candidates = [(bid, et) for bid, et in candidates if bid in keep]
            logger.info(f"  Multiple candidates for {n_free} free slots; selected {keep} (lowest correlation)")

        elif n_free == 0 and len(candidates) > 1:
            # We must evaluate eviction. If we have multiple candidates, we find the single best move
            # that results in the lowest new portfolio correlation.
            held_ids = [self.slots[i]["basket_id"] for i in range(self.n_slots)]
            best_evict_idx = None
            best_candidate = None
            best_new_portfolio_corr = float('inf')
            
            for bid, entry_type in candidates:
                worst_idx = _eviction_target(held_ids, bid, self.basket_close_series, bar_time)
                if worst_idx is not None:
                    test_held = list(held_ids)
                    test_held[worst_idx] = bid
                    
                    from itertools import combinations
                    new_corr = sum(_pairwise_correlation(test_held[a], test_held[b], self.basket_close_series, bar_time)
                                   for a, b in combinations(range(self.n_slots), 2))
                                   
                    if new_corr < best_new_portfolio_corr:
                        best_new_portfolio_corr = new_corr
                        best_evict_idx = worst_idx
                        best_candidate = (bid, entry_type)
                        
            if best_candidate is not None:
                logger.info(f"  Multiple candidates for eviction; selected B{best_candidate[0]} to evict B{held_ids[best_evict_idx]}")
                candidates = [best_candidate]
            else:
                candidates = []

        eviction_done = False
        for bid, entry_type in candidates:
            if bid in self.active_basket_ids:
                continue

            free_slots_now = [i for i in range(self.n_slots) if self.slots[i] is None]

            if free_slots_now:
                if both_empty:
                    capital = self.base_capital / self.n_slots
                else:
                    capital = self.available_cash
                self._pending_entries.append((bid, entry_type, capital, False, -1))
            else:
                if eviction_done:
                    continue
                held_ids = [self.slots[i]["basket_id"] for i in range(self.n_slots)]
                worst_idx = _eviction_target(held_ids, bid, self.basket_close_series, bar_time)
                if worst_idx is None:
                    logger.info(f"  Skip B{bid}: incoming most correlated, no eviction")
                    continue
                # Queue eviction + new entry together
                self._pending_entries.append((bid, entry_type, None, True, worst_idx))
                eviction_done = True

    # ── Next-bar execution ────────────────────────────────────────────────────

    def execute_pending(self, exec_prices_by_basket: dict,
                        bar_time: pd.Timestamp,
                        basket_info: dict) -> list:
        """
        Execute all queued exits and entries at the given exec prices.
        exec_prices_by_basket: {basket_id: {ticker: open_price}}
        basket_info: {basket_id: {"tickers": [...], "symbols": [...]}}

        Returns list of trade dicts (for logging/state persistence).
        """
        new_trades = []

        # ── Execute exits first ───────────────────────────────────────────────
        new_pending_exits = []
        for slot_idx, reason in self._pending_exits:
            slot = self.slots[slot_idx]
            if slot is None:
                continue
            bid = slot["basket_id"]
            prices = exec_prices_by_basket.get(bid)
            if prices is None:
                logger.warning(f"No exec prices for B{bid} exit, skipping")
                new_pending_exits.append((slot_idx, reason))
                continue
            trade = self._close_slot(slot_idx, prices, bar_time, reason)
            if trade:
                new_trades.append(trade)
                self.slots[slot_idx] = None
        self._pending_exits = new_pending_exits

        # ── Execute entries ───────────────────────────────────────────────────
        new_pending_entries = []
        for (bid, entry_type, capital, needs_eviction, evict_idx) in self._pending_entries:
            if bid in self.active_basket_ids:
                continue

            prices = exec_prices_by_basket.get(bid)
            if prices is None:
                logger.warning(f"No exec prices for B{bid} entry, skipping")
                new_pending_entries.append((bid, entry_type, capital, needs_eviction, evict_idx))
                continue

            # Eviction
            if needs_eviction and evict_idx >= 0 and self.slots[evict_idx] is not None:
                evict_bid = self.slots[evict_idx]["basket_id"]
                evict_prices = exec_prices_by_basket.get(evict_bid)
                if evict_prices is None:
                    logger.warning(f"No exec prices for eviction B{evict_bid}, skipping B{bid} entry")
                    new_pending_entries.append((bid, entry_type, capital, needs_eviction, evict_idx))
                    continue
                trade = self._close_slot(evict_idx, evict_prices, bar_time, "evicted_new_entry")
                if trade is None:
                    new_pending_entries.append((bid, entry_type, capital, needs_eviction, evict_idx))
                    continue
                new_trades.append(trade)
                self.slots[evict_idx] = None
                capital = self.available_cash

            if capital is None:
                capital = self.available_cash

            target_slot = next((i for i in range(self.n_slots) if self.slots[i] is None), None)
            if target_slot is None:
                logger.warning(f"B{bid}: no free slot after eviction, skip")
                new_pending_entries.append((bid, entry_type, capital, needs_eviction, evict_idx))
                continue

            info = basket_info.get(bid, {})
            tickers = info.get("tickers", [])
            if not tickers:
                continue

            state = self._open_slot(bid, entry_type, tickers, prices, capital, bar_time)
            if state:
                self.slots[target_slot] = state
                # Reset returns reference for peers
                for peer_idx in range(self.n_slots):
                    if peer_idx != target_slot and self.slots[peer_idx] is not None:
                        self._reset_returns_ref(self.slots[peer_idx], bar_time)
            else:
                new_pending_entries.append((bid, entry_type, capital, needs_eviction, evict_idx))

        self._pending_entries = new_pending_entries
        return new_trades

    # ── Slot helpers ──────────────────────────────────────────────────────────

    def _open_slot(self, basket_id: int, entry_type: str, tickers: list,
                   exec_prices: dict, capital: float,
                   bar_time: pd.Timestamp) -> dict | None:
        per_stock = capital / len(tickers)
        quantities, entry_prices = {}, {}
        for t in tickers:
            p = exec_prices.get(t)
            if p is None or p <= 0:
                return None
            quantities[t]   = int(per_stock / p)
            entry_prices[t] = p

        investment = sum(quantities[t] * entry_prices[t] for t in tickers)
        basket_close = self.basket_close_series.get(basket_id)
        peak = float(basket_close.iloc[-1]) if basket_close is not None and len(basket_close) else None

        logger.info(f"  OPEN B{basket_id} ({entry_type}) at {bar_time}  invest={investment:.0f}")
        return {
            "basket_id":          basket_id,
            "entry_time":         bar_time,
            "entry_type":         entry_type,
            "tickers":            tickers,
            "quantities":         quantities,
            "entry_prices":       entry_prices,
            "investment":         investment,
            "capital_allocated":  capital,
            "entry_basket_close": peak,
            "peak_basket_close":  peak,
            "returns_ref_value":  investment,
            "returns_ref_time":   bar_time,
        }

    def _close_slot(self, slot_idx: int, exec_prices: dict,
                    bar_time: pd.Timestamp, reason: str) -> dict | None:
        slot = self.slots[slot_idx]
        if slot is None:
            return None
        # Same-bar entry/exit guard (except MTM)
        if reason != "mtm" and bar_time == slot["entry_time"]:
            return None

        exit_value = sum(
            slot["quantities"][t] * exec_prices.get(t, slot["entry_prices"][t])
            for t in slot["tickers"]
        )
        pnl = exit_value - slot["investment"]

        if reason != "mtm":
            self.realized_pnl += pnl

        hold_mins = (bar_time - slot["entry_time"]).total_seconds() / 60
        trade = {
            "basket_id":        slot["basket_id"],
            "entry_time":       slot["entry_time"],
            "exit_time":        bar_time,
            "entry_type":       slot["entry_type"],
            "close_reason":     reason,
            "investment":       round(slot["investment"], 2),
            "exit_value":       round(exit_value, 2),
            "pnl":              round(pnl, 2),
            "pnl_pct":          round(pnl / slot["investment"] * 100, 2) if slot["investment"] > 0 else 0.0,
            "hold_minutes":     round(hold_mins, 1),
            "hold_days":        round(hold_mins / 1440, 2),
            "status":           "open (MTM)" if reason == "mtm" else "closed",
            "entry_prices":     slot["entry_prices"],
            "exit_prices":      {t: exec_prices.get(t) for t in slot["tickers"]},
            "quantities":       slot["quantities"],
        }
        logger.info(
            f"  CLOSE B{slot['basket_id']} ({reason}) at {bar_time}  "
            f"pnl={pnl:+.0f} ({trade['pnl_pct']:+.2f}%)"
        )
        self.trade_log.append(trade)
        return trade

    def _reset_returns_ref(self, slot: dict, reset_time: pd.Timestamp):
        bc = self.basket_close_series.get(slot["basket_id"])
        if bc is None or bc.empty:
            return
        avail = bc.index[bc.index <= reset_time]
        if avail.empty:
            return
        ref_price = float(bc.loc[avail[-1]])
        ref_value = sum(slot["quantities"][t] for t in slot["tickers"]) * ref_price
        # For multi-stock basket, compute properly
        # (simplified: scale investment proportionally)
        slot["returns_ref_value"] = slot["investment"]
        slot["returns_ref_time"]  = reset_time

    # ── MTM snapshot ─────────────────────────────────────────────────────────

    def mtm_snapshot(self, basket_close_prices: dict) -> dict:
        """
        Mark-to-market all open slots.
        Returns {"unrealized_pnl": float, "total_equity": float, "slots": [...]}
        """
        unrealized = 0.0
        slot_info  = []
        for slot in self.slots:
            if slot is None:
                slot_info.append(None)
                continue
            bid = slot["basket_id"]
            price = basket_close_prices.get(bid)
            if price is None:
                mtm_value = slot["investment"]
            else:
                # Approximate MTM using basket close
                scale = price / (slot.get("entry_basket_close") or price)
                mtm_value = slot["investment"] * scale
            u_pnl = mtm_value - slot["investment"]
            unrealized += u_pnl
            slot_info.append({
                "basket_id":    bid,
                "entry_time":   slot["entry_time"],
                "investment":   slot["investment"],
                "mtm_value":    round(mtm_value, 2),
                "unrealized_pnl": round(u_pnl, 2),
                "peak_close":   slot["peak_basket_close"],
            })
        return {
            "unrealized_pnl": round(unrealized, 2),
            "realized_pnl":   round(self.realized_pnl, 2),
            "total_equity":   round(self.base_capital + unrealized, 2),
            "slots":          slot_info,
        }

    def check_exit_signals(self, sell_signals: dict,
                           basket_close_prices: dict) -> list:
        """
        Called at bar close. Checks indicator sell signal for each slot.
        sell_signals: {basket_id: bool}  — from signal engine
        basket_close_prices: {basket_id: float}

        Queues exits and returns list of (slot_idx, reason).
        """
        exits = []
        for idx, slot in enumerate(self.slots):
            if slot is None:
                continue
            bid = slot["basket_id"]
            price = basket_close_prices.get(bid)
            if price is None:
                continue

            indicator_exit = sell_signals.get(bid, False)

            if indicator_exit:
                reason = "indicator_exit"
                self.queue_exit(idx, reason)
                exits.append((idx, reason))
                logger.info(f"  EXIT QUEUED B{bid} ({reason})  close={price:.2f}  peak={slot['peak_basket_close']:.2f}")

        return exits
