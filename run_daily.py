"""
run_daily.py — ADTS daily paper trading engine (Choice FinX / kkunal).

Usage:
    python run_daily.py [--basket-csv path] [--basket-size 6]
                        [--capital 10000000] [--fresh]

Logic (identical to strategy1_dualbasket1.py backtest):
  ┌─ Day N market close (15:30) ──────────────────────────────────────────────┐
  │  15:35  fetch today's completed daily bar → append to buffers             │
  │         recompute indicators → evaluate OR-gate signals                   │
  │         queue exits (reg +2σ or trailing SL) + queue entries              │
  │         save state                                                        │
  └───────────────────────────────────────────────────────────────────────────┘
  ┌─ Day N+1 market open (09:15) ─────────────────────────────────────────────┐
  │  09:17  fetch today's 09:15 open via 1-min bar for each pending ticker    │
  │         execute queued exits + entries at those open prices               │
  │         save state                                                        │
  └───────────────────────────────────────────────────────────────────────────┘

All orders are PAPER ONLY.
"""

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, time as dtime
from pathlib import Path

import types
import pandas as pd

# Prevent ModuleNotFoundError: No module named 'websocket' if websocket-client is not yet installed
try:
    import websocket
except ImportError:
    sys.modules["websocket"] = types.ModuleType("websocket")

from config_daily import (
    IST, MARKET_OPEN_H, MARKET_OPEN_M, MARKET_CLOSE_H, MARKET_CLOSE_M,
    EOD_EVAL_H, EOD_EVAL_M, MORNING_EXEC_H, MORNING_EXEC_M,
    POSITION_SIZE, N_SLOTS, TARGET_BASKET_SIZE,
    BASKET_CSV_PATH, STATE_FILE, TRADE_LOG_FILE, LOG_LEVEL,
    DOTENV_FILE, WARMUP_CALENDAR_DAYS, CHOICE_BASE_URL, SESSION_FILE,
)
from data_manager_daily import (
    warmup_daily_buffers, DailyTickerBuffer, fetch_todays_open, fetch_live_ltp,
)
from signal_engine_daily import DailySignalEngine, build_basket_info
from symbol_mapper import (
    load_symbol_master, build_basket_instrument_map,
    all_unique_tickers_from_configs,
)
from portfolio_engine import PortfolioEngine
from state_store import save_state, load_state

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("run_daily")


# ── .env / credential helpers ─────────────────────────────────────────────────

def _load_env() -> dict:
    try:
        from dotenv import load_dotenv, find_dotenv
        load_dotenv(
            find_dotenv(filename=DOTENV_FILE, raise_error_if_not_found=False) or DOTENV_FILE,
            override=True,
        )
    except ImportError:
        logger.warning("python-dotenv not installed — reading os.environ only.")
    return {k: os.environ.get(k, "") for k in (
        "CHOICE_VENDOR_ID", "CHOICE_API_KEY", "CHOICE_MOBILE_NO",
        "CHOICE_SESSION_ID", "CHOICE_SESSION_SAVED_AT", "CHOICE_BASE_URL",
    )}


def _save_session_to_env(session_id: str, base_url: str):
    """Persist the SessionId and BaseURL back into .env so the next run can reuse it."""
    try:
        from dotenv import set_key, find_dotenv
        env_path = find_dotenv(filename=DOTENV_FILE, raise_error_if_not_found=False) or DOTENV_FILE
        now_iso = datetime.now().isoformat()
        set_key(env_path, "CHOICE_SESSION_ID", session_id)
        set_key(env_path, "CHOICE_SESSION_SAVED_AT", now_iso)
        set_key(env_path, "CHOICE_BASE_URL", base_url)
        logger.info(f"Session saved to {env_path}")
    except Exception as e:
        logger.warning(f"Could not write session to .env: {e}")


def _session_is_same_day(saved_at_iso: str) -> bool:
    try:
        saved_date = datetime.fromisoformat(saved_at_iso).date()
        return saved_date == datetime.now().date()
    except Exception:
        return False


def _load_basket_config(csv_path: str, target_size: int) -> dict:
    REQUIRED = {"basket_id", "stock_position", "symbol", "ticker", "company_name", "sector"}
    df = pd.read_csv(csv_path)
    missing = REQUIRED - set(df.columns)
    if missing:
        raise ValueError(f"Basket CSV missing columns: {missing}")

    if "basket_size" in df.columns:
        df = df[df["basket_size"] == target_size]
    if df.empty:
        raise ValueError(f"No baskets found for basket_size={target_size}")

    return {"label": f"{target_size}-stock", "basket_size": target_size, "members": df}


def _init_connection(force_fresh: bool = False):
    """
    Initialize ChoiceClient from kkunal library.
    Handles session persistence via session file and automated TOTP login.
    """
    from choice_api import ChoiceClient, BASE_URL_FINX

    env = _load_env()
    for required in ("CHOICE_VENDOR_ID", "CHOICE_API_KEY", "CHOICE_MOBILE_NO"):
        if not env.get(required):
            logger.error(f"{required} is not set in .env")
            raise RuntimeError(f"Missing required credential: {required} in .env")

    client = ChoiceClient(
        vendor_id=env["CHOICE_VENDOR_ID"],
        api_key=env["CHOICE_API_KEY"],
        base_url=env.get("CHOICE_BASE_URL") or CHOICE_BASE_URL or BASE_URL_FINX,
    )

    session_path = Path(SESSION_FILE)

    # 1. Try loading today's session file
    if not force_fresh and session_path.exists():
        if client.load_session(str(session_path)):
            logger.info(f"Active session restored from {session_path}")
            return client

    # 2. Try loading from .env if saved today
    saved_at = env.get("CHOICE_SESSION_SAVED_AT", "")
    cached = env.get("CHOICE_SESSION_ID", "").strip()
    if not force_fresh and cached and saved_at and _session_is_same_day(saved_at):
        client.session_id = cached
        logger.info(f"Session restored from .env (saved today at {datetime.fromisoformat(saved_at).strftime('%H:%M')})")
        return client

    # 3. Perform automated login via kkunal
    logger.info("Executing automated Choice FinX login via kkunal...")
    try:
        session_id = client.login(env["CHOICE_MOBILE_NO"])
        client.save_session(str(session_path))
        _save_session_to_env(session_id, client.base_url)
        logger.info("Choice FinX login successful.")
        return client
    except Exception as e:
        logger.error(f"Choice FinX login failed: {e}")
        raise


# ── Timing helpers ────────────────────────────────────────────────────────────

def _is_market_day(dt: datetime) -> bool:
    return dt.weekday() < 5


def _now_ist() -> datetime:
    return datetime.now(IST)


def _next_occurrence(h: int, m: int, after: datetime | None = None) -> datetime:
    """Next datetime with hour:minute >= now (or after), skipping weekends."""
    base = (after or _now_ist()).replace(
        hour=h, minute=m, second=0, microsecond=0
    )
    now = after or _now_ist()
    if base <= now:
        base += timedelta(days=1)
    while base.weekday() >= 5:
        base += timedelta(days=1)
    return base


def _sleep_until(target: datetime):
    """Sleep in chunks until target time, logging progress."""
    while True:
        remaining = (target - _now_ist()).total_seconds()
        if remaining <= 0:
            return
        chunk = min(remaining, 300)
        if remaining > 60:
            logger.debug(f"  Sleeping {remaining/60:.1f} min until {target.strftime('%H:%M:%S %Z')}")
        time.sleep(max(chunk, 1))


# ── Daily trading runner ──────────────────────────────────────────────────────

class DailyTradingRunner:
    """
    Orchestrates the daily paper trading loop.

    State machine per day:
      PHASE_WAIT_EOD   → sleep until 15:35
      PHASE_EOD_EVAL   → fetch bar, compute signals, queue orders
      PHASE_WAIT_OPEN  → sleep until next trading day 09:17
      PHASE_EXEC       → fetch opens, execute pending orders
      back to PHASE_WAIT_EOD
    """

    def __init__(self, conn, config: dict, instrument_map: dict,
                 ticker_buffers: dict, capital: float = POSITION_SIZE):
        self.conn           = conn
        self.config         = config
        self.instrument_map = instrument_map
        self.ticker_buffers = ticker_buffers

        basket_info = build_basket_info(config)
        self.portfolio  = PortfolioEngine(initial_capital=capital, n_slots=N_SLOTS)
        self.signal_eng = DailySignalEngine(
            basket_info, ticker_buffers, instrument_map,
            position_size=capital / N_SLOTS,
        )

    def _fetch_current_prices(self) -> dict:
        """Fetch latest LTP for every ticker held in open slots."""
        needed: set[str] = set()
        for slot in self.portfolio.slots:
            if slot is not None:
                needed.update(slot["tickers"])
        prices = {}
        for ticker in needed:
            info = self.instrument_map.get(ticker)
            if not info:
                continue
            p = fetch_live_ltp(self.conn, info["segment_id"], info["token"])
            if p is not None:
                prices[ticker] = p
        return prices

    def _has_pending(self) -> bool:
        return bool(self.portfolio._pending_entries or self.portfolio._pending_exits)

    def run_forever(self):
        """
        One loop iteration decides the next action for "today":

          1. Pending orders, trading day, before the close, not yet executed today
               → wait for 09:17 if early, then execute at TODAY'S OPEN price
                 (also covers a late start: fills still use the 09:15 open,
                 matching the backtest's next-open execution).
          2. EOD evaluation not yet done today (any calendar day — special
             weekend sessions exist) → wait for 15:35, append every new
             completed bar, evaluate and queue the next open's orders.
          3. Otherwise sleep until 09:17 the next calendar day and re-check.

        Both "done today" markers are persisted, so a restart never repeats or
        skips a step.  NSE holidays are detected from the data: no new daily
        bar → no new entry signals (see DailySignalEngine.on_day_close).
        """
        logger.info("Starting ADTS daily paper trading runner…")
        self._catch_up()

        while True:
            now = _now_ist()
            today = now.date().isoformat()
            trading_day = _is_market_day(now)

            exec_target  = now.replace(hour=MORNING_EXEC_H, minute=MORNING_EXEC_M, second=0, microsecond=0)
            market_close = now.replace(hour=MARKET_CLOSE_H, minute=MARKET_CLOSE_M, second=0, microsecond=0)
            eod_target   = now.replace(hour=EOD_EVAL_H, minute=EOD_EVAL_M, second=0, microsecond=0)

            if (trading_day and self._has_pending() and now < market_close
                    and self.portfolio.last_exec_date != today):
                if now < exec_target:
                    logger.info(f"Pending orders. Waiting for open execution at {exec_target.strftime('%H:%M %Z')}…")
                    _sleep_until(exec_target)
                self._morning_exec(_now_ist())
                self.portfolio.last_exec_date = today
                self._save()
                continue

            # Every calendar day (not only Mon–Fri): NSE sometimes trades on a
            # Saturday/Sunday (Budget day, Muhurat). No new bar → nothing happens.
            if self.portfolio.last_eod_date != today:
                if now < eod_target:
                    logger.info(f"Waiting for EOD evaluation at {eod_target.strftime('%H:%M %Z')}…")
                    _sleep_until(eod_target)
                self._eod_eval(_now_ist())
                continue

            nxt = (now + timedelta(days=1)).replace(
                hour=MORNING_EXEC_H, minute=MORNING_EXEC_M, second=0, microsecond=0)
            logger.info(f"Day done. Next check: {nxt.strftime('%A %Y-%m-%d %H:%M %Z')}")
            _sleep_until(nxt)

    def _catch_up(self):
        """
        At startup: if the warmup data holds a completed bar newer than the last
        one evaluated (engine was down at EOD), evaluate it now so its signals
        and any exits are not lost.  Orders still fill at the next open.
        """
        latest = self.signal_eng.latest_bar_date()
        done = self.portfolio.last_signal_bar_date
        if latest is None:
            return
        if done is None or latest.isoformat() > done:
            logger.info(f"Catch-up: evaluating bar {latest} (last evaluated: {done or 'never'})")
            self._eod_eval(_now_ist(), append=False)

    def _eod_eval(self, eval_time: datetime, append: bool = True):
        logger.info("=" * 60)
        logger.info(f"EOD EVALUATION: {eval_time.strftime('%Y-%m-%d %H:%M:%S %Z')}")
        logger.info("=" * 60)

        if append:
            n_appended = 0
            for ticker, info in self.instrument_map.items():
                buf = self.ticker_buffers.get(ticker)
                if buf is None:
                    continue
                if buf.append_today(self.conn, info["segment_id"], info["token"], ticker):
                    n_appended += 1
            logger.info(f"  Appended today's daily bar for {n_appended}/{len(self.instrument_map)} tickers")
            if n_appended == 0:
                logger.info("  No daily bar for today (exchange holiday?) — no new entry signals.")

        eval_ts = pd.Timestamp(eval_time)
        since = self.portfolio.last_signal_bar_date
        since_date = datetime.fromisoformat(since).date() if since else None
        res = self.signal_eng.on_day_close(eval_ts, since_date=since_date)
        entry_signals = res["entry_signals"]
        basket_closes = res["basket_closes"]

        # 1. Correlation history = each basket's real daily closes (as in backtest)
        self.portfolio.on_bar_close(eval_ts, basket_closes)
        self.portfolio.sync_close_series(
            {bid: o["Close"] for bid, o in self.signal_eng.basket_ohlc.items()})

        # 2. Exits: first OR-gate exit (regression_2sd / ema_death_cross) on or
        #    after each position's entry day.  Scanning since entry (not just
        #    today's bar) means an exit missed while the engine was down still fires.
        sell_signals = {}
        for slot in self.portfolio.slots:
            if slot is None:
                continue
            hit = self.signal_eng.exit_signal_since(slot["basket_id"], slot["entry_time"])
            if hit:
                bar_time, exit_type = hit
                sell_signals[slot["basket_id"]] = exit_type
                logger.info(f"  EXIT SIGNAL B{slot['basket_id']}: {exit_type} on bar {bar_time.date()}")
        self.portfolio.check_exit_signals(sell_signals, basket_closes)

        logger.info(f"  Signals: {len(entry_signals)} entries {entry_signals}, "
                    f"{len(sell_signals)} exits {sell_signals}")

        # 3. Queue new entries (slots with a queued exit count as free) — but only
        #    when a NEW trading day arrived.  On a weekend or holiday there is no
        #    new bar: re-planning would wipe the orders queued for the next open
        #    (e.g. a Friday signal that must fill on Monday).
        newest = res.get("latest_bar_date")
        if newest is not None and (not since or newest.isoformat() > since):
            self.portfolio.queue_entries(entry_signals, eval_ts)
        elif self.portfolio._pending_entries:
            logger.info(f"  No new trading day — keeping {len(self.portfolio._pending_entries)} "
                        f"queued entr{'y' if len(self.portfolio._pending_entries) == 1 else 'ies'} for the next open")

        # 4. Bookkeeping, snapshot and save
        if res.get("latest_bar_date") is not None:
            newest = res["latest_bar_date"].isoformat()
            if not since or newest > since:
                self.portfolio.last_signal_bar_date = newest
        if eval_time.hour * 60 + eval_time.minute >= EOD_EVAL_H * 60 + EOD_EVAL_M:
            self.portfolio.last_eod_date = eval_time.date().isoformat()

        snap = self.portfolio.mtm_snapshot(basket_closes, self.signal_eng.latest_component_closes())
        logger.info(
            f"  MTM  realized={snap['realized_pnl']:+,.0f}  "
            f"unrealized={snap['unrealized_pnl']:+,.0f}  "
            f"equity={snap['total_equity']:,.0f}"
        )

        self._save()
        logger.info("  EOD evaluation complete. State saved.")

    def _morning_exec(self, exec_time: datetime):
        logger.info("=" * 60)
        logger.info(f"MORNING EXECUTION: {exec_time.strftime('%Y-%m-%d %H:%M:%S %Z')}")
        logger.info("=" * 60)

        exec_ts = pd.Timestamp(exec_time)

        for attempt in range(5):
            missing_any = False
            open_prices: dict[str, float] = {}

            # Collect all tickers needed across pending exits, entries AND the
            # baskets an entry will evict (they are sold at the same open).
            needed_tickers: set[str] = set()
            for slot_idx, reason in self.portfolio._pending_exits:
                slot = self.portfolio.slots[slot_idx]
                if slot is not None:
                    needed_tickers.update(slot["tickers"])

            for item in self.portfolio._pending_entries:
                bid, _et, _cap, needs_eviction, slot_idx = item
                info = self.signal_eng.basket_info.get(bid, {})
                needed_tickers.update(info.get("tickers", []))
                if needs_eviction and 0 <= slot_idx < self.portfolio.n_slots:
                    victim = self.portfolio.slots[slot_idx]
                    if victim is not None:
                        needed_tickers.update(victim["tickers"])

            for t in needed_tickers:
                info = self.instrument_map.get(t)
                if not info:
                    continue
                op = fetch_todays_open(self.conn, info["segment_id"], info["token"])
                if op is not None:
                    open_prices[t] = op
                else:
                    missing_any = True

            exec_prices = self.signal_eng.get_exec_prices(open_prices)
            basket_info = self.signal_eng.basket_info

            new_trades = self.portfolio.execute_pending(exec_prices, exec_ts, basket_info)
            if new_trades:
                for t in new_trades:
                    reason = t.get("close_reason", "trade")
                    logger.info(
                        f"  EXEC {reason} B{t['basket_id']} "
                        f"pnl={t['pnl']:+.0f} ({t['pnl_pct']:+.2f}%)"
                    )

            self._save()

            pending_e_after = len(self.portfolio._pending_entries)
            pending_x_after = len(self.portfolio._pending_exits)

            if (pending_e_after > 0 or pending_x_after > 0) and missing_any:
                logger.warning(f"  {pending_e_after} entries and {pending_x_after} exits still pending. Retrying in 60s...")
                time.sleep(60)
                continue

            break


    def _save(self):
        save_state(
            self.portfolio,
            self.ticker_buffers,
            STATE_FILE,
            TRADE_LOG_FILE,
        )


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="ADTS Daily Paper Trader (Choice FinX / kkunal)",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument("--basket-csv",  default=BASKET_CSV_PATH)
    parser.add_argument("--basket-size", type=int,   default=TARGET_BASKET_SIZE)
    parser.add_argument("--capital",     type=float, default=POSITION_SIZE)
    parser.add_argument("--fresh",       action="store_true",
                        help="Ignore saved portfolio state; start clean")

    args = parser.parse_args()

    Path("state").mkdir(exist_ok=True)

    conn = _init_connection()

    logger.info(f"Loading basket config: {args.basket_csv} (size={args.basket_size})")
    config = _load_basket_config(args.basket_csv, args.basket_size)
    all_tickers = all_unique_tickers_from_configs([config])
    basket_info = build_basket_info(config)
    logger.info(f"  {len(basket_info)} baskets  {len(all_tickers)} unique tickers")

    logger.info("Building symbol map from symbol master…")
    load_symbol_master()
    instrument_map = build_basket_instrument_map(all_tickers)
    unmapped = set(all_tickers) - set(instrument_map)
    if unmapped:
        logger.warning(f"  {len(unmapped)} tickers unmapped: {sorted(unmapped)}")
    logger.info(f"  {len(instrument_map)} tickers mapped")

    logger.info(f"Fetching {WARMUP_CALENDAR_DAYS}-day daily history…")
    ticker_buffers = warmup_daily_buffers(conn, instrument_map, WARMUP_CALENDAR_DAYS)

    runner = DailyTradingRunner(
        conn=conn, config=config,
        instrument_map=instrument_map,
        ticker_buffers=ticker_buffers,
        capital=args.capital,
    )

    if not args.fresh:
        if load_state(runner.portfolio, ticker_buffers, STATE_FILE):
            logger.info("Daily portfolio state restored.")
    else:
        logger.info("Fresh start (--fresh).")

    runner.run_forever()


if __name__ == "__main__":
    main()
