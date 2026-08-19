"""
run_paper.py — ADTS 5-min paper trading runner (Choice FinX / kkunal).

Usage:
    python run_paper.py [--basket-csv path] [--basket-size 6]
                        [--capital 10000000] [--fresh]

Logic:
  Bar close (e.g. 09:20):
    1. Poll Choice FinX ChartData for new completed 5-min bars.
    2. Compute indicators for all baskets.
    3. Evaluate OR-gate entry signals + trailing SL / reg exits.
    4. Queue trades for execution at next bar open.
  Bar open (e.g. 09:20:02):
    Execute queued exits and entries.
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

from choice_api import ChoiceClient, BASE_URL_FINX
from data_manager import (
    warmup_ticker_buffers, TickerBuffer, LiveBarPoller,
    SessionExpiredError,
)
from signal_engine import BasketSignalEngine, build_basket_info
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
logger = logging.getLogger("run_paper")


# ── .env / credential helpers ─────────────────────────────────────────────────

def _load_env() -> dict:
    try:
        from dotenv import load_dotenv, find_dotenv
        load_dotenv(find_dotenv(filename=DOTENV_FILE, raise_error_if_not_found=False)
                    or DOTENV_FILE, override=True)
    except ImportError:
        logger.warning("python-dotenv not installed; reading environment only.")
    return {k: os.environ.get(k, "") for k in (
        "CHOICE_VENDOR_ID", "CHOICE_API_KEY", "CHOICE_MOBILE_NO",
        "CHOICE_SESSION_ID", "CHOICE_SESSION_SAVED_AT", "CHOICE_BASE_URL",
    )}


def _save_session_to_env(session_id: str, base_url: str):
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


def _clear_session_from_env():
    try:
        from dotenv import set_key, find_dotenv
        env_path = find_dotenv(filename=DOTENV_FILE, raise_error_if_not_found=False) or DOTENV_FILE
        set_key(env_path, "CHOICE_SESSION_ID", "")
        set_key(env_path, "CHOICE_SESSION_SAVED_AT", "")
        set_key(env_path, "CHOICE_BASE_URL", "")
        os.environ.pop("CHOICE_SESSION_ID", None)
        os.environ.pop("CHOICE_SESSION_SAVED_AT", None)
        os.environ.pop("CHOICE_BASE_URL", None)
        session_file = Path(SESSION_FILE)
        if session_file.exists():
            session_file.unlink()
        logger.info("Stale session cleared.")
    except Exception as e:
        logger.warning(f"Could not clear session: {e}")


def _session_is_same_day(saved_at_iso: str) -> bool:
    try:
        saved_date = datetime.fromisoformat(saved_at_iso).date()
        return saved_date == datetime.now().date()
    except Exception:
        return False


def _init_client(force_fresh: bool = False) -> ChoiceClient:
    env = _load_env()
    for required in ("CHOICE_VENDOR_ID", "CHOICE_API_KEY", "CHOICE_MOBILE_NO"):
        if not env.get(required):
            raise RuntimeError(f"{required} is not set in .env")

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


# ── Timing helpers ────────────────────────────────────────────────────────────

def _is_market_day(dt: datetime) -> bool:
    return dt.weekday() < 5


def _is_in_market_hours(dt: datetime) -> bool:
    t = dt.time()
    return dtime(MARKET_OPEN_H, MARKET_OPEN_M) <= t < dtime(MARKET_CLOSE_H, MARKET_CLOSE_M)


def _bar_open_time(dt: datetime) -> datetime:
    minutes_since_open = (
        dt.hour * 60 + dt.minute
    ) - (MARKET_OPEN_H * 60 + MARKET_OPEN_M)
    bar_idx = max(0, minutes_since_open // BAR_MINUTES)
    return dt.replace(
        hour=MARKET_OPEN_H, minute=MARKET_OPEN_M, second=0, microsecond=0
    ) + timedelta(minutes=bar_idx * BAR_MINUTES)


def _bar_close_time(dt: datetime) -> datetime:
    return _bar_open_time(dt) + timedelta(minutes=BAR_MINUTES)


# ── Paper trading runner ──────────────────────────────────────────────────────

class PaperTradingRunner:
    """
    Orchestrates the full 5-min paper trading loop.
    """

    def __init__(self, client: ChoiceClient, config: dict, instrument_map: dict,
                 ticker_buffers: dict, capital: float = POSITION_SIZE):
        self.client = client
        self.config = config
        self.instrument_map = instrument_map
        self.ticker_buffers = ticker_buffers

        basket_info = build_basket_info(config)
        slot_capital = capital / N_SLOTS

        self.portfolio = PortfolioEngine(initial_capital=capital, n_slots=N_SLOTS)
        self.signal_eng = BasketSignalEngine(
            basket_info, ticker_buffers, instrument_map,
            position_size=slot_capital,
        )
        self.poller = LiveBarPoller(client, instrument_map, ticker_buffers)
        self._last_eval_bar: pd.Timestamp | None = None

    def run_forever(self):
        logger.info("=" * 60)
        logger.info("ADTS 5-min Paper Trader — LIVE (Choice FinX / kkunal)")
        
        logger.info("Pre-computing indicators from warmup data...")
        self.signal_eng.warmup_indicators()
        
        logger.info(f"  {self.signal_eng.warmup_status()}")
        logger.info(f"  Capital: {self.portfolio.base_capital:,.0f}   Slots: {N_SLOTS}")
        logger.info("=" * 60)

        try:
            while True:
                now = datetime.now(IST)

                if not _is_market_day(now):
                    logger.info("Weekend / non-trading day. Sleeping 1 h.")
                    time.sleep(3600)
                    continue

                if not _is_in_market_hours(now):
                    self._end_of_day(now)
                    next_open = now.replace(
                        hour=MARKET_OPEN_H, minute=MARKET_OPEN_M,
                        second=0, microsecond=0,
                    )
                    if now >= next_open:
                        next_open += timedelta(days=1)
                    sleep_secs = (next_open - now).total_seconds()
                    logger.info(f"Market closed. Sleeping {sleep_secs / 3600:.1f} h.")
                    time.sleep(min(sleep_secs, 3600))
                    continue

                current_bar_open = _bar_open_time(now)
                curr_bar_ts = pd.Timestamp(current_bar_open).tz_convert(IST)

                exec_threshold = curr_bar_ts + timedelta(seconds=EXEC_DELAY_SECS)
                if (pd.Timestamp(now) >= exec_threshold and
                        (self.portfolio._pending_exits or
                         self.portfolio._pending_entries)):
                    self._execute_pending(curr_bar_ts)

                prev_bar_open = current_bar_open - timedelta(minutes=BAR_MINUTES)
                prev_bar_ts = pd.Timestamp(prev_bar_open).tz_convert(IST)
                prev_bar_close = current_bar_open
                
                prev_eval_time = prev_bar_close + timedelta(seconds=EVAL_DELAY_SECS)
                prev_eval_ts = pd.Timestamp(prev_eval_time).tz_convert(IST)
                
                if (pd.Timestamp(now) >= prev_eval_ts and
                        self._last_eval_bar != prev_bar_ts):
                    self._on_bar_close(prev_bar_ts, now)
                    self._last_eval_bar = prev_bar_ts

                if self._last_eval_bar == prev_bar_ts:
                    next_eval_time = current_bar_open + timedelta(minutes=BAR_MINUTES, seconds=EVAL_DELAY_SECS)
                    target_eval_ts = pd.Timestamp(next_eval_time).tz_convert(IST)
                else:
                    target_eval_ts = prev_eval_ts

                now_ts = pd.Timestamp(now)
                secs_to_eval = (target_eval_ts - now_ts).total_seconds()
                sleep_secs = max(1, min(secs_to_eval - 2, 30))
                time.sleep(sleep_secs)

        except KeyboardInterrupt:
            logger.info("KeyboardInterrupt — saving state and exiting.")
            save_state(self.portfolio, self.ticker_buffers)

    def _on_bar_close(self, bar_ts: pd.Timestamp, now: datetime):
        logger.info(f"── Bar {bar_ts.strftime('%H:%M')} close ──────────────────")

        self.poller.poll_and_update(now)

        result = self.signal_eng.on_bar_close(bar_ts)
        basket_closes = result["basket_closes"]

        self.portfolio.on_bar_close(bar_ts, basket_closes)
        self.portfolio.check_exit_signals(result["sell_signals"], basket_closes)
        self.portfolio.queue_entries(result["entry_signals"], bar_ts)

        snap = self.portfolio.mtm_snapshot(basket_closes)
        logger.info(
            f"  MTM  realized={snap['realized_pnl']:+,.0f}  "
            f"unrealized={snap['unrealized_pnl']:+,.0f}  "
            f"equity={snap['total_equity']:,.0f}"
        )
        logger.info(f"  {self.signal_eng.warmup_status()}")

        save_state(self.portfolio, self.ticker_buffers)

    def _execute_pending(self, bar_ts: pd.Timestamp):
        exec_prices = self.signal_eng.get_exec_prices(bar_ts)
        basket_info = self.signal_eng.basket_info
        new_trades = self.portfolio.execute_pending(exec_prices, bar_ts, basket_info)
        if new_trades:
            for t in new_trades:
                status = t.get("close_reason", "?")
                logger.info(
                    f"  EXEC {status} B{t['basket_id']} "
                    f"pnl={t['pnl']:+.0f} ({t['pnl_pct']:+.2f}%)"
                )
            save_state(self.portfolio, self.ticker_buffers)

    def _end_of_day(self, now: datetime):
        closes = self.signal_eng.get_basket_close_prices()
        snap = self.portfolio.mtm_snapshot(closes)
        logger.info("=" * 60)
        logger.info("END OF DAY")
        logger.info(f"  Realized PnL:   {snap['realized_pnl']:+,.2f}")
        logger.info(f"  Unrealized PnL: {snap['unrealized_pnl']:+,.2f}")
        logger.info(f"  Total Equity:   {snap['total_equity']:,.2f}")
        for s in [s for s in snap["slots"] if s is not None]:
            logger.info(
                f"  Open B{s['basket_id']}: invest={s['investment']:,.0f}  "
                f"mtm={s['mtm_value']:,.0f}  u_pnl={s['unrealized_pnl']:+,.0f}"
            )
        logger.info("=" * 60)
        save_state(self.portfolio, self.ticker_buffers)


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="ADTS 5-min Paper Trader (Choice FinX / kkunal)",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument("--basket-csv",  default=BASKET_CSV_PATH)
    parser.add_argument("--basket-size", type=int,   default=TARGET_BASKET_SIZE)
    parser.add_argument("--capital",     type=float, default=POSITION_SIZE)
    parser.add_argument("--fresh",       action="store_true",
                        help="Ignore saved portfolio state; start clean")
    args = parser.parse_args()

    Path("state").mkdir(exist_ok=True)

    client = _init_client()

    logger.info(f"Loading basket config from {args.basket_csv} (size={args.basket_size})")
    config = _load_basket_config(args.basket_csv, args.basket_size)
    all_tickers = all_unique_tickers_from_configs([config])
    logger.info(f"  {len(build_basket_info(config))} baskets, {len(all_tickers)} unique tickers")

    logger.info("Loading local ticker→token symbol master…")
    load_symbol_master()
    instrument_map = build_basket_instrument_map(all_tickers)
    unmapped = set(all_tickers) - set(instrument_map)
    if unmapped:
        logger.warning(f"  {len(unmapped)} tickers unmapped: {sorted(unmapped)}")
    logger.info(f"  {len(instrument_map)} tickers mapped")

    logger.info(f"Fetching {WARMUP_DAYS}-day warm-up history for {len(instrument_map)} instruments…")
    for _login_attempt in range(2):
        try:
            ticker_buffers = warmup_ticker_buffers(client, instrument_map, warmup_days=WARMUP_DAYS)
            break
        except SessionExpiredError as e:
            if _login_attempt == 0:
                logger.warning(
                    f"Session rejected during warmup ({e}). "
                    "Auto-clearing stale session and re-logging in..."
                )
                _clear_session_from_env()
                client = _init_client(force_fresh=True)
            else:
                logger.error(
                    f"Session still rejected after fresh login: {e}"
                )
                sys.exit(1)

    runner = PaperTradingRunner(
        client=client, config=config,
        instrument_map=instrument_map,
        ticker_buffers=ticker_buffers,
        capital=args.capital,
    )

    if not args.fresh:
        if load_state(runner.portfolio, ticker_buffers):
            logger.info("Portfolio state restored.")
    else:
        logger.info("Fresh start (--fresh).")

    runner.run_forever()


if __name__ == "__main__":
    main()
