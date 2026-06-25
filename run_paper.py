"""
run_paper.py — ADTS 5-min paper trading runner (Choice FinX OpenAPI).

Requires:
    pip install pandas numpy scipy pytz requests pycryptodome python-dotenv

Usage:
    python run_paper.py [options]

Options:
    --basket-csv   path to basket CSV  (default: data/baskets_nifty200_all_sizes.csv)
    --basket-size  int                 (default: 6)
    --capital      float               (default: 100000)
    --fresh        ignore saved state; start clean
    --otp          OTP to use instead of the automated GetClientLoginTOTP call

Credentials (.env):
    CHOICE_VENDOR_ID, CHOICE_VENDOR_KEY, CHOICE_API_KEY   — vendor/API creds
    CHOICE_MOBILE_NO                                       — client mobile no.
    CHOICE_AES_KEY, CHOICE_AES_IV                          — MobileNo encryption
                                                              material (issued
                                                              by Choice in a
                                                              separate doc)
    CHOICE_SESSION_ID, CHOICE_SESSION_SAVED_AT             — cached session
                                                              (written back
                                                              automatically)

Session caching: Choice doesn't document the SessionId TTL in the excerpt we
have, so we conservatively re-login after _SESSION_MAX_AGE_HOURS and cache
in between, mirroring the old 24h pyintegrate pattern. Adjust if Choice's
separate docs specify a different lifetime.

Bar cadence (NSE 5-min) and execution timing are unchanged from the
previous integration:
  ┌─ bar N open ─── bar N close ─ +3s eval ──── signals queued
  │                                              │
  └─ bar N+1 open ─ +2s execution ──────────────┘

All order execution in this file is PAPER ONLY (no real orders placed via
the Choice NewOrder/V2 endpoints — those are intentionally not called here).
"""

import argparse
import logging
import sys
import time
from datetime import datetime, timedelta, time as dtime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import (
    IST, BAR_MINUTES, EVAL_DELAY_SECS, EXEC_DELAY_SECS,
    MARKET_OPEN_H, MARKET_OPEN_M, MARKET_CLOSE_H, MARKET_CLOSE_M,
    POSITION_SIZE, N_SLOTS, TARGET_BASKET_SIZE,
    BASKET_CSV_PATH, STATE_FILE, TRADE_LOG_FILE, LOG_LEVEL,
    WARMUP_DAYS, CHOICE_BASE_URL,
)
from choice_api import ChoiceClient
from symbol_mapper import (
    load_symbol_master, build_basket_instrument_map,
    all_unique_tickers_from_configs,
)
from data_manager import warmup_ticker_buffers, TickerBuffer, LiveBarPoller, SessionExpiredError
from signal_engine import BasketSignalEngine, build_basket_info
from portfolio_engine import PortfolioEngine
from state_store import save_state, load_state

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("run_paper")

DOTENV_FILE  = ".env"
_SESSION_MAX_AGE_HOURS = 23   # conservative; adjust if Choice docs specify otherwise


# ── Basket config loader (unchanged) ──────────────────────────────────────────

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


# ── Choice FinX session management ───────────────────────────────────────────

def _load_env() -> dict:
    """
    Load credentials and cached session from .env file.
    Requires: pip install python-dotenv
    """
    try:
        from dotenv import load_dotenv, find_dotenv
        load_dotenv(find_dotenv(filename=DOTENV_FILE, raise_error_if_not_found=False)
                    or DOTENV_FILE, override=True)
    except ImportError:
        logger.warning("python-dotenv not installed; reading environment only. "
                       "Run: pip install python-dotenv")
    import os
    return {k: os.environ.get(k, "") for k in (
        "CHOICE_VENDOR_ID", "CHOICE_VENDOR_KEY", "CHOICE_API_KEY",
        "CHOICE_MOBILE_NO", "CHOICE_AES_KEY", "CHOICE_AES_IV",
        "CHOICE_SESSION_ID", "CHOICE_SESSION_SAVED_AT", "CHOICE_BASE_URL",
    )}


def _save_session_to_env(session_id: str, base_url: str):
    """Persist the SessionId and dynamic BaseURL back into .env so the next run can reuse it."""
    try:
        from dotenv import set_key, find_dotenv
        env_path = find_dotenv(filename=DOTENV_FILE, raise_error_if_not_found=False) or DOTENV_FILE
        now_iso  = datetime.now().isoformat()
        set_key(env_path, "CHOICE_SESSION_ID", session_id)
        set_key(env_path, "CHOICE_SESSION_SAVED_AT", now_iso)
        set_key(env_path, "CHOICE_BASE_URL", base_url)
        logger.info(f"Session saved to {env_path} (valid for rest of today)")
    except Exception as e:
        logger.warning(f"Could not write session to .env: {e}")


def _clear_session_from_env():
    """Wipe the cached session from .env, forcing fresh OTP login next time."""
    try:
        from dotenv import set_key, find_dotenv
        import os
        env_path = find_dotenv(filename=DOTENV_FILE, raise_error_if_not_found=False) or DOTENV_FILE
        set_key(env_path, "CHOICE_SESSION_ID", "")
        set_key(env_path, "CHOICE_SESSION_SAVED_AT", "")
        set_key(env_path, "CHOICE_BASE_URL", "")
        # Also clear from current process environment so _load_env() gets fresh values.
        os.environ.pop("CHOICE_SESSION_ID", None)
        os.environ.pop("CHOICE_SESSION_SAVED_AT", None)
        os.environ.pop("CHOICE_BASE_URL", None)
        logger.info("Stale session cleared from .env — will require fresh OTP login.")
    except Exception as e:
        logger.warning(f"Could not clear session from .env: {e}")


def _session_is_same_day(saved_at_iso: str) -> bool:
    """Return True if the session was saved earlier today (same calendar date)."""
    try:
        saved_date = datetime.fromisoformat(saved_at_iso).date()
        return saved_date == datetime.now().date()
    except Exception:
        return False


def _init_client(otp: str | None, force_fresh: bool = False) -> ChoiceClient:
    """
    Create a ChoiceClient and either restore a cached SessionId (same calendar day)
    or run the full OTP login flow.

    Session caching policy:
      - If a session was saved TODAY  → restore it (no OTP needed).
      - If the session is from a prior day, or force_fresh=True → fresh OTP login.
      - On any 401 during data fetch, call this with force_fresh=True to re-login.
    """
    env = _load_env()
    for required in ("CHOICE_VENDOR_ID", "CHOICE_VENDOR_KEY", "CHOICE_API_KEY",
                     "CHOICE_MOBILE_NO"):
        if not env.get(required):
            raise RuntimeError(f"{required} is not set in .env")

    client = ChoiceClient(
        vendor_id=env["CHOICE_VENDOR_ID"],
        vendor_key=env["CHOICE_VENDOR_KEY"],
        api_key=env["CHOICE_API_KEY"],
        base_url=env.get("CHOICE_BASE_URL") or CHOICE_BASE_URL,
    )

    saved_at = env.get("CHOICE_SESSION_SAVED_AT", "")
    cached   = env.get("CHOICE_SESSION_ID", "").strip()

    if not force_fresh and cached and saved_at and _session_is_same_day(saved_at):
        # Same-day session — reuse without OTP.
        client.set_session_id(cached)
        logger.info(f"Session restored from .env (saved today at "
                    f"{datetime.fromisoformat(saved_at).strftime('%H:%M')}) "
                    "— no OTP needed.")
        return client

    if cached and saved_at and not force_fresh:
        logger.info("Cached session is from a previous day — fresh login required.")
    elif force_fresh:
        logger.info("Forced fresh login (previous session was rejected).")

    # Full OTP login.
    logger.info("Logging in to Choice FinX…")
    if otp is None:
        otp = _fetch_or_prompt_otp(client, env["CHOICE_MOBILE_NO"])
    session_id = client.login(env["CHOICE_MOBILE_NO"], otp=otp)
    _save_session_to_env(session_id, client.base_url)
    logger.info("Login successful. Session cached for today.")
    return client


def _fetch_or_prompt_otp(client: ChoiceClient, mobile_no: str) -> str:
    """
    Try to fetch the OTP automatically via GetClientLoginTOTP.
    If that fails or returns no OTP, prompt the user to enter it in the CLI.
    """
    from choice_api import encrypt_mobile_no

    enc_mobile = encrypt_mobile_no(mobile_no)
    headers = client._login_headers()

    # Step 1: Trigger OTP via LoginTOTP
    logger.info("Choice login: requesting OTP (LoginTOTP)…")
    try:
        client._request("POST", "/api/OpenAPIV1/LoginTOTP", headers,
                        {"MobileNo": enc_mobile})
    except Exception as e:
        logger.warning(f"LoginTOTP failed: {e} — will still prompt for OTP.")

    # Step 2: Try to fetch OTP automatically (vendor-only endpoint)
    logger.info("Choice login: attempting automated OTP fetch (GetClientLoginTOTP)…")
    otp = None
    try:
        otp_resp = client._request("POST", "/api/OpenAPIV1/GetClientLoginTOTP",
                                   headers, {"MobileNo": enc_mobile})
        logger.info(f"GetClientLoginTOTP response: {otp_resp!r}")
        payload = otp_resp.get("Response") or {}

        # Case 1: Response is a plain string — it IS the OTP
        # Observed: {'Status': 'Success', 'Response': '469361'}
        if isinstance(payload, str) and payload.strip():
            otp = payload.strip()
        # Case 2: Response is a dict with OTP/Otp key
        elif isinstance(payload, dict):
            otp = payload.get("OTP") or payload.get("Otp") or payload.get("otp")

        if otp:
            logger.info(f"OTP fetched automatically: {otp}")
            return str(otp)
        else:
            logger.warning(
                f"GetClientLoginTOTP returned no OTP in response: {otp_resp!r}"
            )
    except Exception as e:
        logger.warning(f"Automated OTP fetch failed: {e}")


    # Step 3: Fall back to CLI prompt
    print("\n" + "=" * 60)
    print("  Automated OTP fetch unavailable.")
    print(f"  An OTP has been sent to your registered mobile ({mobile_no[-4:].rjust(10, '*')}).")
    print("=" * 60)
    otp = input("  Enter OTP: ").strip()
    if not otp:
        raise RuntimeError("No OTP entered — cannot complete login.")
    return otp


# ── Timing helpers (unchanged) ────────────────────────────────────────────────

def _is_market_day(dt: datetime) -> bool:
    return dt.weekday() < 5      # Mon–Fri (no holiday calendar; add if needed)


def _is_in_market_hours(dt: datetime) -> bool:
    t = dt.time()
    return dtime(MARKET_OPEN_H, MARKET_OPEN_M) <= t < dtime(MARKET_CLOSE_H, MARKET_CLOSE_M)


def _bar_open_time(dt: datetime) -> datetime:
    """Return the open timestamp of the 5-min bar that contains dt."""
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
        self.client         = client
        self.config         = config
        self.instrument_map = instrument_map
        self.ticker_buffers = ticker_buffers

        basket_info       = build_basket_info(config)
        slot_capital      = capital / N_SLOTS

        self.portfolio    = PortfolioEngine(initial_capital=capital, n_slots=N_SLOTS)
        self.signal_eng   = BasketSignalEngine(
            basket_info, ticker_buffers, instrument_map,
            position_size=slot_capital,
        )
        self.poller       = LiveBarPoller(client, instrument_map, ticker_buffers)
        self._last_eval_bar: pd.Timestamp | None = None

    # ── Main loop ─────────────────────────────────────────────────────────────

    def run_forever(self):
        logger.info("=" * 60)
        logger.info("ADTS 5-min Paper Trader — LIVE (Choice FinX)")
        
        # Pre-compute indicators from seeded warmup data so the engine is
        # immediately ready for signals on the very first bar close.
        logger.info("Pre-computing indicators from warmup data...")
        n_ready = self.signal_eng.warmup_indicators()
        
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
                    # Sleep until next market open
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

                current_bar_open  = _bar_open_time(now)
                curr_bar_ts = pd.Timestamp(current_bar_open).tz_convert('Asia/Kolkata')
                
                # ── Execute any pending orders at current bar open + EXEC_DELAY ────────
                exec_threshold = curr_bar_ts + timedelta(seconds=EXEC_DELAY_SECS)
                if (pd.Timestamp(now) >= exec_threshold and
                        (self.portfolio._pending_exits or
                         self.portfolio._pending_entries)):
                    self._execute_pending(curr_bar_ts)

                # ── Evaluate signals for the PREVIOUS bar at its close + EVAL_DELAY ────
                prev_bar_open = current_bar_open - timedelta(minutes=BAR_MINUTES)
                prev_bar_ts = pd.Timestamp(prev_bar_open).tz_convert('Asia/Kolkata')
                prev_bar_close = current_bar_open
                
                prev_eval_time = prev_bar_close + timedelta(seconds=EVAL_DELAY_SECS)
                prev_eval_ts = pd.Timestamp(prev_eval_time).tz_convert('Asia/Kolkata')
                
                if (pd.Timestamp(now) >= prev_eval_ts and
                        self._last_eval_bar != prev_bar_ts):
                    self._on_bar_close(prev_bar_ts, now)
                    self._last_eval_bar = prev_bar_ts

                # Sleep toward the next pending evaluation
                if self._last_eval_bar == prev_bar_ts:
                    # Already evaluated prev bar; wait for the current bar to close
                    next_eval_time = current_bar_open + timedelta(minutes=BAR_MINUTES, seconds=EVAL_DELAY_SECS)
                    target_eval_ts = pd.Timestamp(next_eval_time).tz_convert('Asia/Kolkata')
                else:
                    # Still waiting to evaluate the previous bar
                    target_eval_ts = prev_eval_ts

                now_ts = pd.Timestamp(now)
                secs_to_eval = (target_eval_ts - now_ts).total_seconds()
                sleep_secs = max(1, min(secs_to_eval - 2, 30))
                time.sleep(sleep_secs)

        except KeyboardInterrupt:
            logger.info("KeyboardInterrupt — saving state and exiting.")
            save_state(self.portfolio, self.ticker_buffers)

    # ── Bar-close handler ─────────────────────────────────────────────────────

    def _on_bar_close(self, bar_ts: pd.Timestamp, now: datetime):
        logger.info(f"── Bar {bar_ts.strftime('%H:%M')} close ──────────────────")

        # 1. Poll broker for new 5-min bars (Choice ChartData fetches them
        #    natively — no 1-min aggregation step needed)
        self.poller.poll_and_update(now)

        # 2. Signal evaluation for all baskets
        result        = self.signal_eng.on_bar_close(bar_ts)
        basket_closes = result["basket_closes"]

        # 3. Update trailing SL peaks + extend basket close series
        self.portfolio.on_bar_close(bar_ts, basket_closes)

        # 4. Exit signal check (reg +2σ or trailing SL)
        self.portfolio.check_exit_signals(result["sell_signals"], basket_closes)

        # 5. Queue new entries
        self.portfolio.queue_entries(result["entry_signals"], bar_ts)

        # 6. MTM log
        snap = self.portfolio.mtm_snapshot(basket_closes)
        logger.info(
            f"  MTM  realized={snap['realized_pnl']:+,.0f}  "
            f"unrealized={snap['unrealized_pnl']:+,.0f}  "
            f"equity={snap['total_equity']:,.0f}"
        )
        logger.info(f"  {self.signal_eng.warmup_status()}")

        # 7. Persist state every bar
        save_state(self.portfolio, self.ticker_buffers)

    # ── Order execution (next bar open) ───────────────────────────────────────

    def _execute_pending(self, bar_ts: pd.Timestamp):
        exec_prices = self.signal_eng.get_exec_prices(bar_ts)
        basket_info = self.signal_eng.basket_info
        new_trades  = self.portfolio.execute_pending(exec_prices, bar_ts, basket_info)
        if new_trades:
            for t in new_trades:
                status = t.get("close_reason", "?")
                logger.info(
                    f"  EXEC {status} B{t['basket_id']} "
                    f"pnl={t['pnl']:+.0f} ({t['pnl_pct']:+.2f}%)"
                )
            save_state(self.portfolio, self.ticker_buffers)

    # ── End-of-day ────────────────────────────────────────────────────────────

    def _end_of_day(self, now: datetime):
        closes = self.signal_eng.get_basket_close_prices()
        snap   = self.portfolio.mtm_snapshot(closes)
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
        description="ADTS 5-min Paper Trader (Choice FinX)",
        formatter_class=argparse.RawTextHelpFormatter,
        epilog=(
            "Credentials are read from .env (copy .env.example → .env and fill in:\n"
            "  CHOICE_VENDOR_ID, CHOICE_VENDOR_KEY, CHOICE_API_KEY,\n"
            "  CHOICE_MOBILE_NO, CHOICE_AES_KEY, CHOICE_AES_IV).\n"
            "Session id is cached back into .env automatically after login."
        ),
    )
    parser.add_argument("--basket-csv",  default=BASKET_CSV_PATH)
    parser.add_argument("--basket-size", type=int,   default=TARGET_BASKET_SIZE)
    parser.add_argument("--capital",     type=float, default=POSITION_SIZE)
    parser.add_argument("--otp",         default=None,
                        help="OTP to use instead of the automated "
                             "GetClientLoginTOTP call")
    parser.add_argument("--fresh",       action="store_true",
                        help="Ignore saved portfolio state; start clean")
    args = parser.parse_args()

    Path("state").mkdir(exist_ok=True)

    # ── 1. Connect + login ────────────────────────────────────────────────────
    client = _init_client(args.otp)

    # ── 2. Load basket config ─────────────────────────────────────────────────
    logger.info(f"Loading basket config from {args.basket_csv} (size={args.basket_size})")
    config = _load_basket_config(args.basket_csv, args.basket_size)
    all_tickers = all_unique_tickers_from_configs([config])
    logger.info(f"  {len(build_basket_info(config))} baskets, {len(all_tickers)} unique tickers")

    # ── 3. Map tickers → SegmentId+Token via local NSE token CSV ──────────────
    logger.info("Loading local ticker→token symbol master…")
    load_symbol_master()
    instrument_map = build_basket_instrument_map(all_tickers)
    unmapped = set(all_tickers) - set(instrument_map)
    if unmapped:
        logger.warning(f"  {len(unmapped)} tickers unmapped: {sorted(unmapped)}")
    logger.info(f"  {len(instrument_map)} tickers mapped")

    # ── 4. Warm up rolling buffers with historical 5-min data ────────────────
    logger.info(f"Fetching {WARMUP_DAYS}-day warm-up history for {len(instrument_map)} instruments…")
    for _login_attempt in range(2):   # at most 2 attempts: cached session, then fresh login
        try:
            ticker_buffers = warmup_ticker_buffers(client, instrument_map, warmup_days=WARMUP_DAYS)
            break   # success
        except SessionExpiredError as e:
            if _login_attempt == 0:
                logger.warning(
                    f"Session rejected during warmup ({e}). "
                    "Auto-clearing stale session and re-logging in with fresh OTP…"
                )
                _clear_session_from_env()
                client = _init_client(otp=None, force_fresh=True)
                # continue loop → retry warmup with new client
            else:
                logger.error(
                    f"Session still rejected after fresh login: {e}\n"
                    "This may be a broker-side issue. Please try again later."
                )
                sys.exit(1)

    # ── 5. Build runner ───────────────────────────────────────────────────────
    runner = PaperTradingRunner(
        client=client, config=config,
        instrument_map=instrument_map,
        ticker_buffers=ticker_buffers,
        capital=args.capital,
    )

    # ── 6. Restore or fresh state ─────────────────────────────────────────────
    if not args.fresh:
        if load_state(runner.portfolio, ticker_buffers):
            logger.info("Portfolio state restored.")
    else:
        logger.info("Fresh start (--fresh).")

    # ── 7. Run ────────────────────────────────────────────────────────────────
    runner.run_forever()


if __name__ == "__main__":
    main()
