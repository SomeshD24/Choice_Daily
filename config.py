"""
config.py — Central config for ADTS 5-min paper trading system.

All bar counts are in 5-min bars unless stated otherwise.
NSE: 9:15–15:30 IST → 75 bars/day
"""

import os
import pytz

# ── Timeframe ─────────────────────────────────────────────────────────────────
BAR_MINUTES          = 5
BARS_PER_DAY         = 75          # (375 min / 5)
IST                  = pytz.timezone("Asia/Kolkata")

MARKET_OPEN_H        = 9
MARKET_OPEN_M        = 15
MARKET_CLOSE_H       = 15
MARKET_CLOSE_M       = 30

# ── Indicator windows (in 5-min bars) ─────────────────────────────────────────
EMA_FAST             = 20          # ≈ 1h40m
EMA_SLOW             = 100         # ≈ 8h20m  (~1.3 days)
ROLLING_WINDOW       = 756         # ≈ 10.1 trading days
MIN_ROLLING_POINTS   = 500
MIN_SLOPE_PCT        = 0.01        # scale-invariant slope filter

# ── Portfolio ──────────────────────────────────────────────────────────────────
N_SLOTS              = 2
POSITION_SIZE        = 10_000_000  # total capital
TRAILING_SL_PCT      = 0.15
CORR_LOOKBACK        = 60          # 5-min bars (≈ 1 trading day) for basket-pair corr

# ── Data ──────────────────────────────────────────────────────────────────────
WARMUP_BARS          = ROLLING_WINDOW + 200   # bars needed before first signal
WARMUP_DAYS          = (WARMUP_BARS // BARS_PER_DAY) + 5   # calendar-day fetch window
MIN_ALIGNED_BARS     = 500         # min aligned bars across all stocks in a basket

# ── Choice FinX OpenAPI (kkunal) ──────────────────────────────────────────────
EXCHANGE             = "NSE"
SEGMENT_NSE_CASH     = 1          # Segment Master Id for NSE-CASH
CHOICE_CHART_INTERVAL = "5"        # matches BAR_MINUTES; '5' = 5-min intraday bars
CHOICE_BASE_URL      = os.environ.get("CHOICE_BASE_URL", "https://finx.choiceindia.com")
SYMBOL_MASTER_PATH   = "data/symbol_master.csv"
BASKET_CSV_PATH      = "data/baskets_nifty200_all_sizes.csv"
BASKET_QUANTITIES_PATH = "data/basket_quantities_6.json"
TARGET_BASKET_SIZE   = 6
SESSION_FILE         = "choice_session.json"
DOTENV_FILE          = ".env"

# ── State + logging ───────────────────────────────────────────────────────────
STATE_FILE           = "state/portfolio_state.db"
TRADE_LOG_FILE       = "state/trade_log.csv"
LOG_LEVEL            = "INFO"

# ── Scheduler ─────────────────────────────────────────────────────────────────
EVAL_DELAY_SECS      = 3
EXEC_DELAY_SECS      = 2
