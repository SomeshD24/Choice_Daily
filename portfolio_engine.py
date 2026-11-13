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

        # Scheduler bookkeeping (persisted by state_store) — ISO date strings
        self.last_eod_date:        str | None = None   # day the EOD evaluation last ran
        self.last_exec_date:       str | None = None   # day the open execution last ran
        self.last_signal_bar_date: str | None = None   # newest bar already evaluated

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
        """Queue exit for execution at next bar open (never twice for one slot)."""
        if self.slots[slot_idx] is None:
            return
        if any(i == slot_idx for i, _ in self._pending_exits):
            return
        self._pending_exits.append((slot_idx, reason))

    def queue_entries(self, signals: list, bar_time: pd.Timestamp):
        """
        Plan tomorrow's entries from tonight's signals, mirroring the backtest's
        simulate_dual_basket_portfolio() for one date:

          1. Slots with a queued exit are treated as FREE (backtest runs exits
             before entries on the same open).
          2. Candidates are processed in basket_id order.
          3. >= 2 free slots and more candidates than free slots -> keep the
             mutually least-correlated subset.
          4. A candidate takes the first free slot.  If none is free, at most one
             eviction per day: mean correlation among {held..., new}; the most
             redundant HELD basket is evicted (a basket opened in this same plan
             can't be evicted — backtest same-day guard); if the newcomer is the
             most redundant, it's skipped.

        Capital is NOT fixed here: it's computed at execution time from the
        realised cash after exits (see execute_pending), exactly like the backtest.

        Entries left over from an earlier day (e.g. no price at the open) are
        dropped — a stale signal must not fill days later.

        Pending-entry tuple: (basket_id, entry_type, None, needs_eviction, slot_idx)
        """
        if self._pending_entries:
            logger.warning(f"  Dropping {len(self._pending_entries)} stale pending entr"
                           f"{'y' if len(self._pending_entries) == 1 else 'ies'}: "
                           f"{[e[0] for e in self._pending_entries]}")
        self._pending_entries = []
        if not signals:
            return

        exiting = {i for i, _ in self._pending_exits if self.slots[i] is not None}
        proj = [None if (s is None or i in exiting) else s["basket_id"]
                for i, s in enumerate(self.slots)]

        seen, candidates = set(), []
        for bid, et in sorted(signals, key=lambda x: x[0]):
            if bid not in seen:
                seen.add(bid)
                candidates.append((bid, et))

        free = [i for i in range(self.n_slots) if proj[i] is None]
        if len(free) >= 2 and len(candidates) > len(free):
            keep = set(_select_least_correlated_subset(
                [bid for bid, _ in candidates], len(free),
                self.basket_close_series, bar_time,
            ))
            candidates = [(bid, et) for bid, et in candidates if bid in keep]
            logger.info(f"  {len(seen)} candidates for {len(free)} free slots; "
                        f"kept least-correlated {sorted(keep)}")

        opened_in_plan: set = set()
        eviction_done = False
        for bid, entry_type in candidates:
            if bid in proj:
                continue
            free = [i for i in range(self.n_slots) if proj[i] is None]
            if free:
                target = free[0]
                proj[target] = bid
                opened_in_plan.add(bid)
                self._pending_entries.append((bid, entry_type, None, False, target))
                logger.info(f"  ENTRY QUEUED B{bid} ({entry_type}) -> slot {target}")
                continue

            if eviction_done:
                continue
            worst_idx = _eviction_target(proj, bid, self.basket_close_series, bar_time)
            if worst_idx is None:
                logger.info(f"  Skip B{bid}: incoming basket is the most correlated, no eviction")
                continue
            if proj[worst_idx] in opened_in_plan:
                logger.info(f"  Skip B{bid}: would evict B{proj[worst_idx]} opened the same day")
                continue
            logger.info(f"  ENTRY QUEUED B{bid} ({entry_type}) evicting B{proj[worst_idx]} "
                        f"in slot {worst_idx}")
            self._pending_entries.append((bid, entry_type, None, True, worst_idx))
            proj[worst_idx] = bid
            opened_in_plan.add(bid)
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
                logger.warning(f"No exec prices for B{bid} exit, kept pending")
                new_pending_exits.append((slot_idx, reason))
                continue
            trade = self._close_slot(slot_idx, prices, bar_time, reason)
            if trade:
                new_trades.append(trade)
                self.slots[slot_idx] = None
            else:
                new_pending_exits.append((slot_idx, reason))
        self._pending_exits = new_pending_exits

        # Backtest: capital split is decided AFTER the day's exits.
        both_empty_at_entry_phase = self.both_empty()

        # ── Execute entries ───────────────────────────────────────────────────
        new_pending_entries = []
        for entry in self._pending_entries:
            bid, entry_type, _capital, needs_eviction, slot_idx = entry
            if bid in self.active_basket_ids:
                continue

            tickers = basket_info.get(bid, {}).get("tickers", [])
            if not tickers:
                logger.warning(f"B{bid}: unknown basket, entry dropped")
                continue

            prices = exec_prices_by_basket.get(bid)
            if prices is None:
                logger.warning(f"No exec prices for B{bid} entry, kept pending")
                new_pending_entries.append(entry)
                continue

            if needs_eviction:
                if not (0 <= slot_idx < self.n_slots):
                    continue
                if self.slots[slot_idx] is not None:
                    evict_bid = self.slots[slot_idx]["basket_id"]
                    evict_prices = exec_prices_by_basket.get(evict_bid)
                    if evict_prices is None:
                        logger.warning(f"No exec prices for eviction B{evict_bid}; B{bid} kept pending")
                        new_pending_entries.append(entry)
                        continue
                    trade = self._close_slot(slot_idx, evict_prices, bar_time, "evicted_new_entry")
                    if trade is None:
                        new_pending_entries.append(entry)
                        continue
                    new_trades.append(trade)
                    self.slots[slot_idx] = None
                target_slot = slot_idx
                capital = self.available_cash
            else:
                if 0 <= slot_idx < self.n_slots and self.slots[slot_idx] is None:
                    target_slot = slot_idx
                else:
                    target_slot = next((i for i in range(self.n_slots) if self.slots[i] is None), None)
                capital = (self.base_capital / self.n_slots if both_empty_at_entry_phase
                           else self.available_cash)

            if target_slot is None:
                logger.warning(f"B{bid}: no free slot (exit not filled yet?), kept pending")
                new_pending_entries.append(entry)
                continue

            state = self._open_slot(bid, entry_type, tickers, prices, capital, bar_time)
            if state:
                self.slots[target_slot] = state
                # Reset returns reference for peers
                for peer_idx in range(self.n_slots):
                    if peer_idx != target_slot and self.slots[peer_idx] is not None:
                        self._reset_returns_ref(self.slots[peer_idx], bar_time)
            else:
                new_pending_entries.append(entry)

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
        if investment <= 0:
            logger.warning(f"  B{basket_id}: capital {capital:,.0f} buys no shares, entry skipped")
            return None
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

    def sync_close_series(self, close_by_basket: dict, keep: int = 200):
        """
        Replace the correlation history with each basket's latest closes
        (the backtest correlates on the full basket Close series, so the live
        engine must not start from an empty history after a restart).
        """
        for bid, close in close_by_basket.items():
            if close is not None and len(close):
                tail = close.iloc[-keep:]
                # Plain Series: basket OHLC carries DataFrame `attrs` (component
                # prices) that make pd.concat() raise when two baskets are compared.
                self.basket_close_series[bid] = pd.Series(
                    tail.to_numpy(dtype=float), index=tail.index)

    def mtm_snapshot(self, basket_close_prices: dict,
                     component_prices: dict | None = None) -> dict:
        """
        Mark-to-market all open slots.
        component_prices: {ticker: last price}. When every ticker of a slot has a
        price, MTM = sum(qty * price) (exact).  Otherwise falls back to scaling
        the investment by the basket-close ratio (approximate).
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
            comp = component_prices or {}
            if slot["tickers"] and all(comp.get(t) for t in slot["tickers"]):
                mtm_value = sum(slot["quantities"][t] * comp[t] for t in slot["tickers"])
            elif price is None:
                mtm_value = slot["investment"]
            else:
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
        Called at bar close. Queues an exit for every held slot whose basket has
        an exit signal.  Matches the backtest: the OR-gate exit is
        regression_2sd (close crosses above +2σ) OR ema_death_cross (EMA fast
        crosses below EMA slow) — whichever comes first, for ANY entry type.
        There is no other exit rule (no trailing SL, no "EMA below" state exit).

        sell_signals: {basket_id: bool | str}; a string is used as the reason
                      (e.g. 'regression_2sd' / 'ema_death_cross').
        Returns list of (slot_idx, reason) newly queued.
        """
        exits = []
        for idx, slot in enumerate(self.slots):
            if slot is None:
                continue
            bid = slot["basket_id"]
            sig = sell_signals.get(bid, False)
            if not sig:
                continue
            if any(i == idx for i, _ in self._pending_exits):
                continue
            reason = sig if isinstance(sig, str) else "indicator_exit"
            self.queue_exit(idx, reason)
            exits.append((idx, reason))
            price = basket_close_prices.get(bid)
            peak = slot.get("peak_basket_close")
            logger.info(
                f"  EXIT QUEUED B{bid} ({reason})"
                + (f"  close={price:.2f}" if price is not None else "")
                + (f"  peak={peak:.2f}" if peak is not None else "")
            )

        return exits
