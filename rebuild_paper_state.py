"""
rebuild_paper_state.py — one-time rebuild of the DAILY PAPER account.

The paper account was traded by an engine with bugs (missed morning orders,
missed exits, look-ahead swap rule), so its saved state does not match what the
strategy would have done.  This tool replays the FIXED engine day by day from
the day the paper account started, using real daily prices (Yahoo, same source
as the warmup), and writes the result as the engine's own state file.  The
dashboard then shows the corrected account and the engine continues from it.

Paper only: no broker orders are involved.

Usage (stop run_daily.py first):
    python rebuild_paper_state.py --start 2026-04-23            # dry run: show result only
    python rebuild_paper_state.py --start 2026-04-23 --apply    # back up old state, write new
"""
import argparse, contextlib, io, logging, shutil, sys, time, types
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

import run_daily
import data_manager_daily as dmd
import strategy1_dualbasket2 as bt
from config_daily import IST, STATE_FILE, TRADE_LOG_FILE, POSITION_SIZE, BASKET_CSV_PATH, \
    TARGET_BASKET_SIZE, EOD_EVAL_H, EOD_EVAL_M
from data_manager_daily import DailyTickerBuffer
from state_store import save_state


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--start", required=True, help="first trading day of the paper account (YYYY-MM-DD)")
    ap.add_argument("--capital", type=float, default=POSITION_SIZE)
    ap.add_argument("--state-file", default=STATE_FILE)
    ap.add_argument("--apply", action="store_true", help="replace the state file (old one is backed up)")
    args = ap.parse_args()
    logging.disable(logging.WARNING)

    df = pd.read_csv(BASKET_CSV_PATH)
    members = df[df.basket_size == TARGET_BASKET_SIZE]
    config = {"label": f"{TARGET_BASKET_SIZE}-stock", "basket_size": TARGET_BASKET_SIZE, "members": members}
    tickers = sorted(members.ticker.unique())

    # ── prices (retry: Yahoo sometimes refuses a request) ────────────────────
    print(f"Downloading daily prices for {len(tickers)} stocks…", flush=True)
    data = {}
    for t in tickers:
        for attempt in range(6):
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    d = bt.load_ohlc(t)
                break
            except Exception:
                time.sleep(10 * (attempt + 1))
        else:
            sys.exit(f"ABORT: could not download {t} — nothing was changed")
        d = d.copy(); d.index = pd.DatetimeIndex(d.index).tz_localize(IST); data[t] = d

    now = datetime.now(IST)
    if now.hour * 60 + now.minute < EOD_EVAL_H * 60 + EOD_EVAL_M:   # today's bar not final yet
        data = {t: d[d.index.date < now.date()] for t, d in data.items()}
    days = sorted(set().union(*[set(d.index.date) for d in data.values()]))
    start = pd.Timestamp(args.start).date()
    before = [d for d in days if d < start]
    if len(before) < 2:
        sys.exit("ABORT: not enough history before --start")
    seed_day, end = before[-2], days[-1]   # signal bar = before[-1] → first fill on `start`
    print(f"Replaying fixed engine: buys from {start} → last completed day {end}", flush=True)

    # ── fixed engine with a simulated clock and Yahoo opens as fills ──────────
    imap = {t: {"segment_id": 1, "token": i + 1} for i, t in enumerate(tickers)}
    tok2t = {v["token"]: t for t, v in imap.items()}
    buffers = {}
    for t in tickers:
        b = DailyTickerBuffer(t); b.seed(data[t][data[t].index.date <= seed_day]); buffers[t] = b

    clock = {"now": IST.localize(datetime.combine(seed_day, datetime.min.time())) + timedelta(hours=20)}
    class Done(Exception): pass
    def now_ist():
        if clock["now"].date() > end:
            raise Done
        return clock["now"]
    def sleep_until(target):
        if target > clock["now"]:
            clock["now"] = target
    def fake_fetch(client, seg, tok, yf_ticker, from_dt, to_dt):
        d = data[yf_ticker]
        return d[(d.index >= pd.Timestamp(from_dt)) & (d.index.date <= to_dt.date())]
    def todays_open(client, seg, tok):
        d = data[tok2t[tok]]; row = d[d.index.date == clock["now"].date()]
        return None if row.empty else float(row.Open.iloc[0])

    tmp_dir = Path(args.state_file).parent / "_rebuild_tmp"
    shutil.rmtree(tmp_dir, ignore_errors=True); tmp_dir.mkdir(parents=True)
    tmp_state, tmp_log = tmp_dir / "state.db", tmp_dir / "trade_log.csv"

    run_daily._now_ist, run_daily._sleep_until = now_ist, sleep_until
    run_daily.time = types.SimpleNamespace(sleep=lambda s: sleep_until(clock["now"] + timedelta(seconds=s)))
    run_daily.fetch_todays_open = todays_open
    run_daily.STATE_FILE, run_daily.TRADE_LOG_FILE = str(tmp_state), str(tmp_log)
    dmd.fetch_daily_historical = fake_fetch
    dmd.datetime = types.SimpleNamespace(now=lambda tz=None: clock["now"])

    runner = run_daily.DailyTradingRunner(conn=None, config=config, instrument_map=imap,
                                          ticker_buffers=buffers, capital=args.capital)
    runner._save = lambda: None          # write once at the end, not after every simulated day
    runner.portfolio.last_signal_bar_date = seed_day.isoformat()
    runner.portfolio.last_eod_date = seed_day.isoformat()
    try:
        runner.run_forever()
    except Done:
        pass
    save_state(runner.portfolio, buffers, str(tmp_state), str(tmp_log))

    # ── summary (open positions at the last close) ───────────────────────────
    pe = runner.portfolio
    last_close = {t: float(d.Close.iloc[-1]) for t, d in data.items()}
    closed = [t for t in pe.trade_log if t.get("status", "closed") == "closed"]
    print(f"\nClosed trades: {len(closed)}")
    for t in closed:
        print(f"  B{t['basket_id']:<3} {pd.Timestamp(t['entry_time']).date()} -> {pd.Timestamp(t['exit_time']).date()}"
              f"  {t.get('close_reason', ''):<18} {t['pnl']:>+12,.0f}  ({t['pnl_pct']:+.2f}%)")
    unreal = 0.0
    print("Open slots:")
    for i, s in enumerate(pe.slots):
        if s is None:
            print(f"  slot {i}: empty"); continue
        val = sum(q * last_close[tk] for tk, q in s["quantities"].items())
        unreal += val - s["investment"]
        print(f"  slot {i}: B{s['basket_id']} since {pd.Timestamp(s['entry_time']).date()}  "
              f"invested {s['investment']:,.0f}  value {val:,.0f}  ({val - s['investment']:+,.0f})")
    print(f"Pending for next open: entries {pe._pending_entries}  exits {pe._pending_exits}")
    print(f"\nRealized P&L   {pe.realized_pnl:+,.0f}\nUnrealized P&L {unreal:+,.0f}\n"
          f"Total equity   {args.capital + pe.realized_pnl + unreal:,.0f}   (prices of {end})")

    if not args.apply:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        print("\nDry run — nothing changed. Add --apply to write this as the paper account.")
        return

    target = Path(args.state_file)
    stamp = datetime.now(IST).strftime("%Y%m%d-%H%M")
    if target.exists():
        backup = target.with_name(f"{target.stem}.backup-{stamp}{target.suffix}")
        shutil.copy2(target, backup); print(f"\nOld state backed up to {backup}")
    log_target = target.parent / Path(TRADE_LOG_FILE).name
    if log_target.exists():
        shutil.copy2(log_target, log_target.with_name(f"{log_target.stem}.backup-{stamp}{log_target.suffix}"))
    shutil.copy2(tmp_state, target)
    if tmp_log.exists():
        shutil.copy2(tmp_log, log_target)
    shutil.rmtree(tmp_dir, ignore_errors=True)
    print(f"New paper state written to {target}. Start run_daily.py again.")


if __name__ == "__main__":
    main()
