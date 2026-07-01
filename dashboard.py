"""
dashboard.py — ADTS Daily Paper Trader • Live Dashboard

Fixes applied:
  • days_back = 1300 (matches WARMUP_CALENDAR_DAYS — enough for 756+ trading days)
  • LTP priority: 1-min close (during market) → daily close (after close / fallback)
  • After 15:30 IST: always shows daily close as LTP
  • Auto-refresh via JS window.location.reload() (avoids Python 3.14 event-loop bug)
  • Full engine state: open slots, pending orders, basket members, indicators
  • Trade log shows basket members + entry/exit prices per ticker
  • Basket chart uses exact build_equal_weight_basket_ohlc() formula
"""


from __future__ import annotations
from pandas._config import config
import json
import os
import sys
import subprocess
from datetime import datetime, timedelta
import time
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st
import streamlit.components.v1 as components
from datetime import time as dtime

# ── Engine Process Management ──────────────────────────────────────────────────

def _get_pid_file() -> Path:
    page = st.session_state.get("nav_page", "Daily Paper Trading")
    if page == "5-Min Paper Trading":
        return Path(__file__).resolve().parent / "state" / "engine_5min.pid"
    return Path(__file__).resolve().parent / "state" / "engine_daily.pid"

def is_engine_running() -> bool:
    pid_file = _get_pid_file()
    if not pid_file.exists():
        return False
    try:
        with open(pid_file) as f:
            pid = int(f.read().strip())
        if os.name == 'nt':
            output = subprocess.check_output(f'tasklist /FI "PID eq {pid}"', shell=True).decode()
            return str(pid) in output
        else:
            try:
                os.kill(pid, 0)
                return True
            except OSError:
                return False
    except Exception:
        return False

def start_engine():
    if not is_engine_running():
        page = st.session_state.get("nav_page", "Daily Paper Trading")
        script_name = "run_paper.py" if page == "5-Min Paper Trading" else "run_daily.py"
        cmd = [sys.executable, str(Path(__file__).resolve().parent / script_name)]
        
        pid_file = _get_pid_file()
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        log_file = pid_file.parent / f"{script_name.replace('.py', '')}.log"
        
        kwargs = {}
        if os.name == 'nt':
            kwargs['creationflags'] = 0x08000000
            
        with open(log_file, "a") as out:
            p = subprocess.Popen(cmd, cwd=str(Path(__file__).resolve().parent), stdout=out, stderr=subprocess.STDOUT, **kwargs)
            
        with open(pid_file, "w") as f:
            f.write(str(p.pid))

def stop_engine():
    pid_file = _get_pid_file()
    if pid_file.exists():
        try:
            with open(pid_file) as f:
                pid = int(f.read().strip())
            if os.name == 'nt':
                subprocess.run(f'taskkill /F /PID {pid}', shell=True)
            else:
                import signal
                os.kill(pid, signal.SIGTERM)
            pid_file.unlink()
        except Exception:
            pass

# ── Path setup ─────────────────────────────────────────────────────────────────
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE
_PROJ = _ROOT
for p in [str(_HERE), str(_ROOT)]:
    if p not in sys.path:
        sys.path.insert(0, p)

# ── Page config ────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="ADTS Daily Trader",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── CSS ────────────────────────────────────────────────────────────────────────
st.markdown("""
<style>
  .stApp{background:#0d1117;color:#e6edf3;}
  .block-container{padding-top:1rem;}
  header[data-testid="stHeader"]{background:transparent;}
  section[data-testid="stSidebar"]>div{background:#0d1117;border-right:1px solid #21262d;}
  button[kind="secondary"]{background:#161b22!important;border:1px solid #30363d!important;color:#e6edf3!important;}
  button[kind="secondary"]:hover{border-color:#58a6ff!important;}

  .mc{background:linear-gradient(135deg,#161b22,#1c2128);border:1px solid #30363d;
      border-radius:12px;padding:16px 18px;text-align:center;margin-bottom:4px;}
  .mc:hover{border-color:#58a6ff;}
  .ml{font-size:.72rem;color:#8b949e;text-transform:uppercase;letter-spacing:.08em;margin-bottom:5px;}
  .mv{font-size:1.45rem;font-weight:700;font-family:'JetBrains Mono',monospace;}
  .ms{font-size:.75rem;color:#8b949e;margin-top:3px;}
  .pos{color:#3fb950;} .neg{color:#f85149;} .neu{color:#58a6ff;}

  .sh{font-size:.88rem;font-weight:600;color:#8b949e;text-transform:uppercase;
      letter-spacing:.1em;border-bottom:1px solid #21262d;padding-bottom:5px;margin:18px 0 10px 0;}
  .tag{background:#21262d;border-radius:4px;padding:2px 7px;font-size:.75rem;color:#8b949e;margin-right:4px;}
</style>
""", unsafe_allow_html=True)


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _format_inr(value: float) -> str:
    is_neg = value < 0
    val_str = str(int(abs(value)))
    if len(val_str) <= 3:
        res = val_str
    else:
        res = val_str[-3:]
        val_str = val_str[:-3]
        while len(val_str) > 2:
            res = val_str[-2:] + "," + res
            val_str = val_str[:-2]
        res = val_str + "," + res
    return "-" + res if is_neg else res

def _fmt(v: float, prefix="₹", sign=True) -> str:
    s = "+" if (sign and v >= 0) else ""
    return f"{s}{prefix}{_format_inr(v)}" if prefix else f"{s}{_format_inr(v)}"

def _pct(v: float) -> str:
    return f"{'+' if v>=0 else ''}{v:.2f}%"

def _cls(v: float) -> str:
    return "pos" if v >= 0 else "neg"

def _metric(col, label: str, val_html: str, sub: str = ""):
    col.markdown(
        f'<div class="mc"><div class="ml">{label}</div>'
        f'<div class="mv">{val_html}</div>'
        + (f'<div class="ms">{sub}</div>' if sub else "")
        + "</div>",
        unsafe_allow_html=True,
    )


# ──────────────────────────────────────────────────────────────────────────────
# State / config loaders
# ──────────────────────────────────────────────────────────────────────────────

def _state_path() -> Path | None:
    page = st.session_state.get("nav_page", "Daily Paper Trading")
    if page == "5-Min Paper Trading":
        return _ROOT / "state" / "portfolio_state.json"
    for p in [
        _ROOT / "state" / "daily_portfolio_state.json",
        _PROJ / "state" / "daily_portfolio_state.json",
        Path("state/daily_portfolio_state.json"),
    ]:
        if p.exists():
            return p
    return None

def _load_state() -> dict | None:
    p = _state_path()
    if p is None:
        return None
    try:
        with open(p) as f:
            return json.load(f)
    except Exception:
        return None


def _load_trade_log() -> pd.DataFrame:
    page = st.session_state.get("nav_page", "Daily Paper Trading")
    if page == "5-Min Paper Trading":
        p = _ROOT / "state" / "trade_log.csv"
        if p.exists():
            return pd.read_csv(p)
        return pd.DataFrame()
    for p in [
        _ROOT / "state" / "daily_trade_log.csv",
        _PROJ / "state" / "daily_trade_log.csv",
    ]:
        if p.exists():
            try:
                df = pd.read_csv(p)
                return df
            except Exception:
                pass
    return pd.DataFrame()


@st.cache_data(ttl=3600, show_spinner=False)
def _basket_info(csv_path: str, size: int | None = None) -> dict:
    try:
        df = pd.read_csv(csv_path)
        if size is not None and "basket_size" in df.columns:
            df = df[df["basket_size"] == size]
        info: dict = {}
        for bid, grp in df.groupby("basket_id"):
            grp = grp.sort_values("stock_position")
            info[int(bid)] = {
                "tickers":   grp["ticker"].tolist(),
                "symbols":   grp["symbol"].tolist() if "symbol" in grp.columns else grp["ticker"].tolist(),
                "companies": grp["company_name"].tolist() if "company_name" in grp.columns else [],
                "sectors":   grp["sector"].tolist()       if "sector"       in grp.columns else [],
                "ticker_to_company": dict(zip(grp["ticker"], grp["company_name"])) if "company_name" in grp.columns else {},
            }
        return info
    except Exception as e:
        return {}


# ──────────────────────────────────────────────────────────────────────────────
# Broker connection
# ──────────────────────────────────────────────────────────────────────────────

def _session_is_same_day(saved_at_iso: str) -> bool:
    try:
        saved_date = datetime.fromisoformat(saved_at_iso).date()
        return saved_date == datetime.now().date()
    except Exception:
        return False

@st.cache_resource(ttl=600)
def _broker():
    """Return (client, ic, err_msg) - client/ic may be None if unavailable."""
    try:
        from dotenv import load_dotenv, find_dotenv
        env_file = find_dotenv(filename=".env", raise_error_if_not_found=False)
        if not env_file:
            # Search upwards from project root
            for candidate in [_PROJ / ".env", _ROOT / ".env", _HERE / ".env"]:
                if candidate.exists():
                    env_file = str(candidate)
                    break
        if env_file:
            load_dotenv(env_file, override=True)
    except ImportError:
        pass

    vendor_id = os.environ.get("CHOICE_VENDOR_ID")
    if not vendor_id:
        return None, None, "No API credentials in .env"

    try:
        from choice_api import ChoiceClient
        from config_daily import CHOICE_BASE_URL
        client = ChoiceClient(
            vendor_id=vendor_id,
            vendor_key=os.environ.get("CHOICE_VENDOR_KEY"),
            api_key=os.environ.get("CHOICE_API_KEY"),
            base_url=os.environ.get("CHOICE_BASE_URL") or CHOICE_BASE_URL,
        )
        
        saved_at = os.environ.get("CHOICE_SESSION_SAVED_AT", "")
        cached   = os.environ.get("CHOICE_SESSION_ID", "").strip()
        if cached and saved_at and _session_is_same_day(saved_at):
            client.set_session_id(cached)
            return client, client, None
            
        # Try automated OTP fetch before falling back to manual prompt
        mobile_no = os.environ.get("CHOICE_MOBILE_NO")
        if mobile_no:
            try:
                session_id = client.login(mobile_no, otp=None)
                if session_id:
                    from dotenv import set_key, find_dotenv
                    from datetime import datetime
                    env_path = find_dotenv(filename=".env", raise_error_if_not_found=False) or ".env"
                    set_key(env_path, "CHOICE_SESSION_ID", session_id)
                    set_key(env_path, "CHOICE_SESSION_SAVED_AT", datetime.now().isoformat())
                    set_key(env_path, "CHOICE_BASE_URL", client.base_url)
                    return client, client, None
            except Exception:
                pass
                
        return None, None, "AUTH_REQUIRED"
    except Exception as e:
        return None, None, str(e)


# ──────────────────────────────────────────────────────────────────────────────
# Symbol map
# ──────────────────────────────────────────────────────────────────────────────

@st.cache_data(ttl=3600, show_spinner=False)
def _instrument_map(tickers: tuple) -> tuple[dict, str]:
    conn, _, _ = _broker()
    if conn is None:
        return {t: t for t in tickers}, "Broker offline"
    try:
        from symbol_mapper import load_symbol_master, build_basket_instrument_map
        load_symbol_master()
        m = build_basket_instrument_map(list(tickers))
        for t in tickers:
            if t not in m:
                m[t] = {"trading_symbol": t}
        return m, None
    except Exception as e:
        return {t: {"trading_symbol": t} for t in tickers}, str(e)

def _sym(imap: dict, ticker: str) -> str:
    entry = imap.get(ticker, {})
    if isinstance(entry, dict):
        return entry.get("trading_symbol", ticker)
    return ticker


@st.cache_data(ttl=300, show_spinner=False)
def _fetch_daily(trading_symbol: str, days_back: int = 1300) -> tuple[pd.DataFrame, str | None]: # bust cache 3
    client, _, err = _broker()
    if client is None:
        return pd.DataFrame(), f"Broker: {err}"
    try:
        import pandas as pd
        from symbol_mapper import ticker_to_token
        from data_manager_daily import fetch_daily_historical
        import pytz
        from datetime import datetime, timedelta
        
        IST = pytz.timezone("Asia/Kolkata")
        now = datetime.now(IST)
        from_dt = now - timedelta(days=days_back)
        
        token = ticker_to_token(trading_symbol)
        if not token:
            return pd.DataFrame(), f"No Choice token for {trading_symbol}"
            
        df = fetch_daily_historical(client, 1, token, trading_symbol, from_dt.replace(tzinfo=None), now.replace(tzinfo=None))
        if df.empty:
            return pd.DataFrame(), f"{trading_symbol}: 0 daily rows returned"
        return df, None
    except Exception as e:
        import pandas as pd
        return pd.DataFrame(), str(e)


@st.cache_data(ttl=60, show_spinner=False)
def _fetch_1min(trading_symbol: str, days_back: int = 5) -> tuple[pd.DataFrame, str | None]:
    client, _, err = _broker()
    if client is None:
        return pd.DataFrame(), f"Broker: {err}"
    try:
        import pandas as pd
        from symbol_mapper import ticker_to_token
        import pytz
        from datetime import datetime, timedelta
        
        IST = pytz.timezone("Asia/Kolkata")
        now = datetime.now(IST)
        from_dt = now - timedelta(days=days_back)
        
        token = ticker_to_token(trading_symbol)
        if not token:
            return pd.DataFrame(), f"No Choice token for {trading_symbol}"
        
        df = client.get_chart_data(
            1, token,
            from_dt.replace(tzinfo=None),
            now.replace(tzinfo=None),
            interval="1"
        )
        
        if df.empty:
            return pd.DataFrame(), f"{trading_symbol}: 0 1-min rows"
        
        if df.index.tz is None:
            df.index = df.index.tz_localize(IST)
        else:
            df.index = df.index.tz_convert(IST)
        
        if df.index.has_duplicates:
            df = df[~df.index.duplicated(keep='last')]
        
        # ── MARKET HOURS FILTER (1-min data, BEFORE resample) ─────────────
        from datetime import time as dtime
        market_start = dtime(9, 15)
        market_end   = dtime(15, 30)
        df = df[(df.index.time >= market_start) & (df.index.time <= market_end)]
        # ──────────────────────────────────────────────────────────────────
        
        df_5min = df.resample("5min", label="right", closed="right").agg({
            "Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum",
        }).dropna(subset=["Close"])
        
        if df_5min.index.has_duplicates:
            df_5min = df_5min[~df_5min.index.duplicated(keep='last')]
        
        # ── MARKET HOURS FILTER (5-min data, AFTER resample) ──────────────
        df_5min = df_5min[(df_5min.index.time >= market_start) & 
                          (df_5min.index.time <= market_end)]
        # ──────────────────────────────────────────────────────────────────
        
        if df_5min.empty:
            return pd.DataFrame(), f"{trading_symbol}: 0 rows after filter"
        
        return df_5min, None
    except Exception as e:
        import pandas as pd
        return pd.DataFrame(), str(e)




# ──────────────────────────────────────────────────────────────────────────────
# LTP logic
# ──────────────────────────────────────────────────────────────────────────────

def _get_ltp(
    ticker: str,
    sym: str,
    is_market_open: bool,
    df_daily: pd.DataFrame,
    df_1min: pd.DataFrame,
) -> tuple[float | None, str]:
    """
    Return (ltp, source_label).
    Priority:
      Market open  → 1-min last close, then daily close
      Market closed → daily close directly (no 1-min needed)
    """
    if is_market_open and not df_1min.empty:
        v = float(df_1min["Close"].iloc[-1])
        return v, "1-min"

    # After market close or 1-min unavailable → daily close
    if not df_daily.empty:
        v = float(df_daily["Close"].iloc[-1])
        return v, "daily-close"

    return None, "—"


# ──────────────────────────────────────────────────────────────────────────────
# Basket price builder — EXACT port of build_equal_weight_basket_ohlc()
# ──────────────────────────────────────────────────────────────────────────────

def _build_basket_df(
    ticker_daily: dict[str, pd.DataFrame],
    ticker_1min:  dict[str, pd.DataFrame],
    tickers: list[str],
    position_size: float,
    is_market_open: bool,
    basket_id: int | None = None,
) -> tuple[pd.DataFrame | None, str]:
    """
    Exact port of build_equal_weight_basket_ohlc() from strategy1_dualbasket2.py.

    Steps:
      1. Build OHLCV MultiIndex panel, inner-join → dropna() (common start date).
      2. quantities = (1/N * position_size) / first_close[t]
      3. basket[field] = sum(panel[(t,field)] * quantities[t])
      4. If market is open: stitch today's 1-min bars as synthetic daily bar.

    Returns (basket_df | None, debug_str).
    """
    fields = ["Open", "High", "Low", "Close", "Volume"]

    quantities = _basket_quantities(ticker_daily, tickers, position_size, basket_id=basket_id)
    if not quantities:
        return None, "Missing fixed quantities in JSON"

    # Use quantities keys as authoritative ticker list — avoids multi-size CSV pollution
    tickers = [t for t in quantities.keys() if t in ticker_daily and not ticker_daily[t].empty]
    missing = [t for t in quantities.keys() if t not in tickers]
    if missing:
        return None, f"Missing daily data for: {missing}"

    loaded = [
        ticker_daily[t][fields].rename(columns={c: (t, c) for c in fields})
        for t in tickers
    ]
    panel = pd.concat(loaded, axis=1, sort=False).dropna()
    if panel.empty:
        return None, "Panel empty after inner join"

    basket = pd.DataFrame(index=panel.index)
    for field in ["Open", "High", "Low", "Close"]:
        basket[field] = sum(panel[(t, field)] * quantities[t] for t in tickers)
    basket["Volume"] = sum(panel[(t, "Volume")] for t in tickers)
    basket = basket.dropna().sort_index()
    if basket.empty:
        return None, "Basket empty after dropna"

    # Stitch today's 1-min bars if market is open
    if is_market_open:
        import pytz
        IST = pytz.timezone("Asia/Kolkata")
        today = datetime.now(IST).date()
        if basket.index[-1].date() < today:
            today_rows: dict = {}
            all_ok = True
            for t in tickers:
                df1 = ticker_1min.get(t, pd.DataFrame())
                if df1.empty:
                    all_ok = False; break
                td = df1[df1.index.date == today]
                if td.empty:
                    all_ok = False; break
                today_rows[t] = {
                    "Open":   float(td["Open"].iloc[0]),
                    "High":   float(td["High"].max()),
                    "Low":    float(td["Low"].min()),
                    "Close":  float(td["Close"].iloc[-1]),
                    "Volume": float(td["Volume"].sum()),
                }
            if all_ok:
                today_ts = pd.Timestamp(today).tz_localize(IST)
                nr: dict = {}
                for field in ["Open", "High", "Low", "Close"]:
                    nr[field] = sum(today_rows[t][field] * quantities[t] for t in tickers)
                nr["Volume"] = sum(today_rows[t]["Volume"] for t in tickers)
                basket = pd.concat([basket, pd.DataFrame([nr], index=[today_ts])])

    return basket, f"{len(basket)} bars, from {basket.index[0].date()} to {basket.index[-1].date()}"


def _basket_quantities(ticker_daily: dict, tickers: list, position_size: float,
                       basket_id: int | None = None) -> dict:
    """Load exact backtest quantities from JSON.

    If basket_id is provided, looks up directly by ID (preferred — avoids
    ticker-set matching which breaks when binfo is loaded without size filter).
    """
    import json
    from pathlib import Path

    candidates = [
        Path(__file__).resolve().parent / "state" / "basket_quantities_6.json",
        _ROOT / "state" / "basket_quantities_6.json",
        _HERE / "state" / "basket_quantities_6.json",
        Path("state/basket_quantities_6.json"),
        Path(__file__).resolve().parent / "basket_quantities_6.json",
        _ROOT / "basket_quantities_6.json",
    ]
    q_file = next((p for p in candidates if p.exists()), None)
    if q_file is None:
        st.warning(
            "⚠️ basket_quantities_6.json not found. Looked in:\n"
            + "\n".join(f"  • {p}" for p in candidates)
        )
        return {}

    try:
        with open(q_file) as f:
            all_q = json.load(f)

        # Direct lookup by basket_id — fast, immune to multi-size CSV pollution
        if basket_id is not None:
            q_map = all_q.get(str(basket_id))
            if q_map:
                return q_map
            st.warning(f"⚠️ Basket {basket_id} not found in `{q_file.name}`.")
            return {}

        # Fallback: match by ticker set (only reliable when size is fixed)
        for bid, q_map in all_q.items():
            if set(q_map.keys()) == set(tickers):
                return q_map
        st.warning(
            f"⚠️ No basket in `{q_file.name}` matches tickers: `{sorted(set(tickers))}`\n\n"
            f"Loaded {len(all_q)} baskets from `{q_file}`. Pass basket_id for direct lookup."
        )
    except Exception as e:
        st.error(f"❌ Failed to load basket_quantities_6.json from `{q_file}`: {e}")

    return {}


def _build_intraday_basket(ticker_1min: dict, tickers: list, quantities: dict, resample_rule: str | None = None) -> pd.DataFrame | None:
    """
    Build equal-weight basket intraday OHLC from per-ticker 1-min/5-min DataFrames.
    
    Uses the SAME logic as backtest's build_equal_weight_basket_ohlc():
      1. Deduplicate each ticker's index
      2. Build MultiIndex panel via concat (axis=1) — each column = (ticker, field)
      3. Inner-join via dropna() — all tickers aligned at common timestamps
      4. Apply quantities: basket[field] = sum(panel[(t, field)] * quantities[t])
    
    This avoids reindex-based duplication bugs entirely.
    """
    import pytz
    IST = pytz.timezone("Asia/Kolkata")
    
    fields = ["Open", "High", "Low", "Close", "Volume"]
    
    # 1. Validate tickers and deduplicate each one's index BEFORE building the panel
    loaded = []
    valid_tickers = []
    
    for t in tickers:
        df = ticker_1min.get(t, pd.DataFrame())
        if df.empty:
            continue
        if t not in quantities:
            continue
        
        # CRITICAL FIX: Deduplicate index — keep last occurrence
        if df.index.has_duplicates:
            df = df[~df.index.duplicated(keep='last')]
        
        # Ensure all required fields exist
        missing_fields = [f for f in fields if f not in df.columns]
        if missing_fields:
            continue
        
        loaded.append(df[fields].rename(columns={c: (t, c) for c in fields}))
        valid_tickers.append(t)
    
    if not loaded or len(valid_tickers) < len(tickers):
        # Some tickers missing — can't build complete basket
        return None
    
    # 2. Build the MultiIndex panel — inner-join via dropna
    #    Identical to strategy1_dualbasket2.py:build_equal_weight_basket_ohlc()
    panel = pd.concat(loaded, axis=1).dropna()
    if panel.empty:
        return None
    
    # 3. Build the basket using pre-computed quantities
    basket = pd.DataFrame(index=panel.index)
    for field in ["Open", "High", "Low", "Close"]:
        basket[field] = sum(panel[(t, field)] * quantities[t] for t in valid_tickers)
    basket["Volume"] = sum(panel[(t, "Volume")] for t in valid_tickers)
    
    basket = basket.dropna().sort_index()
    if basket.empty:
        return None
    
    # 4. Optional resampling (e.g., "5min" — though inputs may already be 5-min)
    if resample_rule and not basket.empty:
        basket = basket.resample(resample_rule).agg({
            "Open": "first",
            "High": "max",
            "Low": "min",
            "Close": "last",
            "Volume": "sum",
        }).dropna()
        
        # Deduplicate again after resample (rare edge case)
        if basket.index.has_duplicates:
            basket = basket[~basket.index.duplicated(keep='last')]
    
    if not basket.empty:
        basket = basket[(basket.index.time >= dtime(9, 15)) & 
                        (basket.index.time <= dtime(15, 30))]
    
    return basket if not basket.empty else None


# ──────────────────────────────────────────────────────────────────────────────
# Indicator overlays
# ──────────────────────────────────────────────────────────────────────────────

def _compute_overlays(close: pd.Series, ema_fast: int, ema_slow: int,
                      show_bands: bool, show_ema: bool, window: int, min_pts: int):
    ef = close.ewm(span=ema_fast, adjust=False).mean() if show_ema else None
    es = close.ewm(span=ema_slow, adjust=False).mean() if show_ema else None
    bands = None
    if show_bands and len(close) >= min_pts:
        try:
            from indicators import rolling_regression_bands
            bands = rolling_regression_bands(close, window, min_pts)
        except Exception:
            pass
    return ef, es, bands


# ──────────────────────────────────────────────────────────────────────────────
# Chart builder
# ──────────────────────────────────────────────────────────────────────────────

def _chart(df: pd.DataFrame, title: str,
           entry_time=None, entry_price=None,
           bands=None, ema_f=None, ema_s=None,
           height: int = 450,
           intraday: bool = False) -> go.Figure:

    fig = make_subplots(rows=2, cols=1, shared_xaxes=True,
                        row_heights=[0.78, 0.22], vertical_spacing=0.02)

    fig.add_trace(go.Candlestick(
        x=df.index, open=df["Open"], high=df["High"],
        low=df["Low"], close=df["Close"], name="Price",
        increasing=dict(line=dict(color="#3fb950"), fillcolor="#152a15"),
        decreasing=dict(line=dict(color="#f85149"), fillcolor="#2a1515"),
    ), row=1, col=1)

    if bands is not None and not bands.empty:
        bi = bands.index.intersection(df.index)
        if not bi.empty:
            b = bands.loc[bi].dropna()
            fig.add_trace(go.Scatter(x=b.index, y=b["upper2"], name="+2σ",
                line=dict(color="rgba(248,81,73,.55)", width=1, dash="dot")), row=1, col=1)
            fig.add_trace(go.Scatter(x=b.index, y=b["trend_line"], name="Trend",
                line=dict(color="rgba(88,166,255,.65)", width=1)), row=1, col=1)
            fig.add_trace(go.Scatter(x=b.index, y=b["lower2"], name="-2σ",
                line=dict(color="rgba(63,185,80,.55)", width=1, dash="dot"),
                fill="tonexty", fillcolor="rgba(88,166,255,0.04)"), row=1, col=1)

    if ema_f is not None:
        fig.add_trace(go.Scatter(x=df.index, y=ema_f.reindex(df.index), name="EMA Fast",
            line=dict(color="#e3b341", width=1.2)), row=1, col=1)
    if ema_s is not None:
        fig.add_trace(go.Scatter(x=df.index, y=ema_s.reindex(df.index), name="EMA Slow",
            line=dict(color="#bc8cff", width=1.4)), row=1, col=1)

    if entry_time is not None and entry_price is not None:
        fig.add_trace(go.Scatter(
            x=[entry_time], y=[entry_price], mode="markers+text",
            marker=dict(symbol="triangle-up", size=14, color="#3fb950"),
            text=["BUY"], textposition="top center",
            textfont=dict(color="#3fb950", size=10), name="Entry",
        ), row=1, col=1)

    colors = ["#3fb950" if c >= o else "#f85149"
              for o, c in zip(df["Open"], df["Close"])]
    fig.add_trace(go.Bar(x=df.index, y=df["Volume"], name="Volume",
        marker_color=colors, opacity=0.45, showlegend=False), row=2, col=1)

    fig.update_layout(
    title=dict(text=title, font=dict(size=13, color="#e6edf3")),
    height=height, paper_bgcolor="#0d1117", plot_bgcolor="#0d1117",
    xaxis_rangeslider_visible=False,
    legend=dict(orientation="h", yanchor="bottom", y=1.01,
                font=dict(size=9, color="#8b949e"), bgcolor="rgba(0,0,0,0)"),
    margin=dict(l=8, r=8, t=48, b=8),
)

# ── MARKET HOURS: hide non-trading gaps for intraday charts ───────────
    if intraday and isinstance(df.index, pd.DatetimeIndex) and len(df) > 0:
        _rb = [
            dict(bounds=[15.5, 9.25], pattern="hour"),   # 15:30→09:15 daily
            dict(bounds=["sat", "mon"]),                  # weekends
        ]
        fig.update_xaxes(rangebreaks=_rb, row=1, col=1)
        fig.update_xaxes(rangebreaks=_rb, row=2, col=1)
# ──────────────────────────────────────────────────────────────────────────

    for ax in ["xaxis", "xaxis2", "yaxis", "yaxis2"]:
        fig.update_layout(**{ax: dict(
            gridcolor="#21262d", tickfont=dict(color="#8b949e", size=9))})
    return fig


# ──────────────────────────────────────────────────────────────────────────────
# Auto-refresh via JS (avoids Python 3.14 event-loop close bug)
# ──────────────────────────────────────────────────────────────────────────────

def _js_autorefresh(ms: int):
    # Auto-refresh via JS (avoids Python 3.14 event-loop close bug)
    st.html(
        f'<script>setTimeout(()=>window.top.location.reload(),{ms});</script>'
    )


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────


# ──────────────────────────────────────────────────────────────────────────────
# Backtest Engine Page
# ──────────────────────────────────────────────────────────────────────────────

def render_backtest_page():
    import pandas as pd
    st.markdown("# 🧪 Backtest Engine (Basket Size 6)")
    st.markdown("---")
    
    st.sidebar.markdown("---")
    st.sidebar.markdown("### Backtest Settings")
    window_val = st.sidebar.number_input("Rolling Window (days)", min_value=100, max_value=2000, value=756, step=1)
    ema_fast_val = st.sidebar.number_input("EMA Fast", min_value=5, max_value=200, value=20, step=1)
    ema_slow_val = st.sidebar.number_input("EMA Slow", min_value=10, max_value=500, value=100, step=1)
    
    run_btn = st.sidebar.button("Run Backtest", type="primary", width='stretch')
    
    if "backtest_results" not in st.session_state:
        st.session_state.backtest_results = None
        
    if run_btn:
        with st.spinner("Running Backtest..."):
            try:
                import sys
                from pathlib import Path
                # Add project root to path if needed to import strategy
                sys.path.insert(0, str(_PROJ))
                import strategy1_dualbasket2
                # Patch rolling window & EMAs
                strategy1_dualbasket2.ROLLING_WINDOW = window_val
                strategy1_dualbasket2.MIN_ROLLING_POINTS = min(500, int(window_val * 0.8))
                strategy1_dualbasket2.EMA_FAST = ema_fast_val
                strategy1_dualbasket2.EMA_SLOW = ema_slow_val
                
                from backtest_size6_report import (
                    load_basket_configs, run_backtest_for_config,
                    compute_extended_metrics, plot_equity_chart,
                    basket_breakdown, entry_type_breakdown, exit_reason_breakdown,
                    plot_trade_chart
                )
                from config_daily import BASKET_CSV_PATH
                
                csv_path = str(_PROJ / BASKET_CSV_PATH)
                configs = load_basket_configs(csv_paths=[csv_path])
                size_configs = [c for c in configs if c["basket_size"] == 6]
                if not size_configs:
                    st.error("No basket_size=6 config found in the CSV.")
                    return
                config = size_configs[0]
                
                initial_capital = strategy1_dualbasket2.POSITION_SIZE
                n_slots = strategy1_dualbasket2.N_SLOTS
                
                results_cfg, bdata, port_trades, equity_abs, _ = run_backtest_for_config(
                    config, initial_capital, n_slots
                )
                
                metrics = compute_extended_metrics(port_trades, equity_abs, initial_capital)
                metrics.update({
                    "basket_size": 6,
                    "label": config["label"],
                    "n_baskets": config["members"]["basket_id"].nunique(),
                    "n_slots": n_slots,
                    "ema_fast": strategy1_dualbasket2.EMA_FAST,
                    "ema_slow": strategy1_dualbasket2.EMA_SLOW,
                    "initial_capital": initial_capital,
                    "rf_annual_pct": strategy1_dualbasket2.RF_ANNUAL * 100,
                })
                
                eq_fig = plot_equity_chart(equity_abs, metrics, initial_capital)
                
                # Breakdowns
                bbd = basket_breakdown(port_trades)
                ebd = entry_type_breakdown(port_trades)
                xbd = exit_reason_breakdown(port_trades)
                
                # Trade detail charts
                best_figs = []
                worst_figs = []
                if port_trades:
                    tdf = pd.DataFrame(port_trades)
                    if "status" in tdf.columns:
                        closed_df = tdf[tdf["status"] == "closed"].copy()
                        if not closed_df.empty:
                            selections = [
                                ("best",  closed_df.nlargest(5,  "pnl_pct")),
                                ("worst", closed_df.nsmallest(5, "pnl_pct")),
                            ]
                            for kind, sel in selections:
                                for rank, (_, row) in enumerate(sel.iterrows(), 1):
                                    bid = row["basket_id"]
                                    if bid in bdata and bid in results_cfg:
                                        t_fig = plot_trade_chart(
                                            trade=row.to_dict(),
                                            ohlc_df=bdata[bid],
                                            bands=results_cfg[bid]["bands"],
                                            signals=results_cfg[bid]["signals"],
                                            rank=rank,
                                            kind=kind
                                        )
                                        if kind == "best":
                                            best_figs.append(t_fig)
                                        else:
                                            worst_figs.append(t_fig)
                
                st.session_state.backtest_results = {
                    "metrics": metrics,
                    "eq_fig": eq_fig,
                    "trades": port_trades,
                    "bbd": bbd,
                    "ebd": ebd,
                    "xbd": xbd,
                    "best_figs": best_figs,
                    "worst_figs": worst_figs
                }
            except Exception as e:
                st.error(f"Error during backtest: {e}")
                
    results = st.session_state.backtest_results
    if results:
        m = results["metrics"]
        
        # Display key metrics
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("CAGR", f"{m.get('cagr_pct', 0):.2f}%")
        c2.metric("Max Drawdown", f"{m.get('max_drawdown_pct', 0):.2f}%")
        c3.metric("Sharpe", f"{m.get('sharpe', 0):.3f}")
        c4.metric("Win Rate", f"{m.get('win_rate_pct', 0):.1f}%")
        
        c5, c6, c7, c8 = st.columns(4)
        c5.metric("Total Return", f"{m.get('total_return_pct', 0):.2f}%")
        c6.metric("Total PnL", f"₹{m.get('total_pnl', 0):,.0f}")
        c7.metric("Trades", f"{m.get('closed_trades', 0)}")
        c8.metric("Profit Factor", f"{m.get('profit_factor', 0):.3f}")
        
        st.markdown("---")
        if results["eq_fig"]:
            st.plotly_chart(results["eq_fig"], width='stretch')
            
        st.markdown("### Breakdowns")
        col1, col2 = st.columns(2)
        with col1:
            st.markdown("**Entry Type Breakdown**")
            if not results.get("ebd", pd.DataFrame()).empty:
                st.dataframe(results["ebd"], width='stretch')
            st.markdown("**Exit Reason Breakdown**")
            if not results.get("xbd", pd.DataFrame()).empty:
                st.dataframe(results["xbd"], width='stretch')
        with col2:
            st.markdown("**Basket Breakdown (Top 10)**")
            if not results.get("bbd", pd.DataFrame()).empty:
                st.dataframe(results["bbd"].head(10), width='stretch')
                
        if results.get("best_figs"):
            st.markdown("---")
            st.markdown("### Top 5 Best Trades")
            for fig in results["best_figs"]:
                st.plotly_chart(fig, width='stretch')
                
        if results.get("worst_figs"):
            st.markdown("---")
            st.markdown("### Top 5 Worst Trades")
            for fig in results["worst_figs"]:
                st.plotly_chart(fig, width='stretch')

def save_engine_config(config_file: str, params: dict):
    import re
    with open(config_file, "r", encoding="utf-8") as f:
        content = f.read()
    
    for k, v in params.items():
        # Match 'KEY = VALUE' preserving comments if any
        content = re.sub(rf"^({k}\s*=\s*).*?(\s*(?:#.*)?)$", rf"\g<1>{v}\g<2>", content, flags=re.MULTILINE)
        
    with open(config_file, "w", encoding="utf-8") as f:
        f.write(content)

def main():
    import pytz
    import importlib
    IST = pytz.timezone("Asia/Kolkata")
    now_ist = datetime.now(IST)

    page = st.session_state.get("nav_page", "Daily Paper Trading")

    if page == "5-Min Paper Trading":
        import config
        importlib.reload(config)
        from config import (
            BASKET_CSV_PATH, TARGET_BASKET_SIZE, POSITION_SIZE as CONFIG_POSITION_SIZE,
            EMA_FAST, EMA_SLOW, ROLLING_WINDOW, MIN_ROLLING_POINTS
        )
    else:
        import config_daily
        importlib.reload(config_daily)
        from config_daily import (
            BASKET_CSV_PATH, TARGET_BASKET_SIZE, POSITION_SIZE as CONFIG_POSITION_SIZE,
            EMA_FAST, EMA_SLOW, ROLLING_WINDOW, MIN_ROLLING_POINTS
        )

    POSITION_SIZE = 100_000 if page == "5-Min Paper Trading" else CONFIG_POSITION_SIZE
    csv_path = str(_PROJ / BASKET_CSV_PATH)

    # ── Market status ─────────────────────────────────────────────────────────
    mkt_open  = now_ist.replace(hour=9,  minute=15, second=0, microsecond=0)
    mkt_close = now_ist.replace(hour=15, minute=30, second=0, microsecond=0)
    is_market_open = (now_ist.weekday() < 5 and mkt_open <= now_ist <= mkt_close)

    # ── Broker ────────────────────────────────────────────────────────────────
    conn, ic, broker_err = _broker()
    broker_ok = conn is not None

    # ── Sidebar ───────────────────────────────────────────────────────────────
    with st.sidebar:
        st.markdown("## 📈 ADTS Paper Trading")

        # Persist nav_page in URL query params so JS location.reload() doesn't
        # reset it to the default.  On a fresh load, seed session_state from URL.
        _pages = ["Daily Paper Trading", "5-Min Paper Trading", "Backtest Engine"]
        if "nav_page" not in st.session_state:
            _qp = st.query_params.get("page", "Daily Paper Trading")
            st.session_state["nav_page"] = _qp if _qp in _pages else "Daily Paper Trading"

        page = st.radio("Navigation", _pages, key="nav_page")

        # Write current page back to URL so reload picks it up
        st.query_params["page"] = page
        
        if page in ["Daily Paper Trading", "5-Min Paper Trading"]:
            # Engine Control
            st.markdown("---")
            engine_running = is_engine_running()
            if engine_running:
                st.success("🟢 Engine is RUNNING")
                if st.button("⏹ Stop Engine", width='stretch'):
                    stop_engine()
                    st.rerun()
            else:
                st.error("🔴 Engine is STOPPED")
                if st.button("▶️ Start Engine", width='stretch'):
                    start_engine()
                    st.rerun()

            st.markdown("---")
            refresh_sec = st.slider("Auto-refresh (s)", 30, 300, 60, 15)
            show_bands  = st.toggle("OLS bands",   value=True)
            show_ema    = st.toggle("EMAs",         value=True)
            chart_mode  = st.radio("Charts", ["Basket + Stocks", "Basket only", "Stocks only"], index=0)

            with st.expander("⚙️ Engine Settings"):
                if page == "5-Min Paper Trading":
                    from config import EMA_FAST as CFG_EMA_FAST, EMA_SLOW as CFG_EMA_SLOW, ROLLING_WINDOW as CFG_ROLLING_WINDOW, MIN_ROLLING_POINTS as CFG_MIN_ROLLING_POINTS, POSITION_SIZE as CFG_POSITION_SIZE
                    cfg_file = "config.py"
                else:
                    from config_daily import EMA_FAST as CFG_EMA_FAST, EMA_SLOW as CFG_EMA_SLOW, ROLLING_WINDOW as CFG_ROLLING_WINDOW, MIN_ROLLING_POINTS as CFG_MIN_ROLLING_POINTS, POSITION_SIZE as CFG_POSITION_SIZE
                    cfg_file = "config_daily.py"

                new_fast = st.number_input("EMA Fast", value=int(CFG_EMA_FAST))
                new_slow = st.number_input("EMA Slow", value=int(CFG_EMA_SLOW))
                new_rw = st.number_input("Rolling Window", value=int(CFG_ROLLING_WINDOW))
                new_min_pts = st.number_input("Min Rolling Points", value=int(CFG_MIN_ROLLING_POINTS))
                new_pos_size = st.number_input("Position Size", value=int(CFG_POSITION_SIZE))

                if st.button("Save Settings", width='stretch'):
                    params = {
                        "EMA_FAST": new_fast,
                        "EMA_SLOW": new_slow,
                        "ROLLING_WINDOW": new_rw,
                        "MIN_ROLLING_POINTS": new_min_pts,
                        "POSITION_SIZE": new_pos_size,
                    }
                    save_engine_config(cfg_file, params)
                    st.success("Saved! Restart engine for changes to take effect.")
                    time.sleep(2)
                    st.rerun()

            st.markdown("---")
            st.markdown(
                f"**Broker:** {'🟢 connected' if broker_ok else f'🔴 offline'}",
                unsafe_allow_html=True,
            )
            if broker_err:
                st.caption(f"⚠️ {broker_err[:80]}")
            st.markdown(f"**Market:** {'🟢 Open' if is_market_open else '🔴 Closed'}")
            st.markdown(f"`{now_ist.strftime('%H:%M:%S IST')}`")
            st.markdown("---")
            if st.button("🔄 Clear cache & refresh"):
                st.cache_data.clear()
                st.rerun()
            st.caption(f"Refresh in {refresh_sec}s")

    if page in ["Daily Paper Trading", "5-Min Paper Trading"]:
        # ── Header ────────────────────────────────────────────────────────────────
        h1, h2 = st.columns([4, 1])
        h1.markdown(f"# 📈 ADTS {page} Paper Trader")
        h2.markdown(
            f"<p style='color:#8b949e;font-size:.82rem;text-align:right;padding-top:14px'>"
            f"{now_ist.strftime('%d %b %Y  %H:%M:%S IST')}</p>",
            unsafe_allow_html=True,
        )
        st.markdown("---")

        # ── Authentication ────────────────────────────────────────────────────────
        if broker_err == "AUTH_REQUIRED":
            st.error("🔒 **Broker Authentication Required**")
            st.markdown("A daily Choice OTP login is required to fetch live market data and execute trades.")
            with st.form("totp_form"):
                totp = st.text_input("Enter Choice OTP", max_chars=6)
                submit = st.form_submit_button("Login")
                
                if submit and totp:
                    try:
                        from choice_api import ChoiceClient
                        from config_daily import CHOICE_BASE_URL
                        from dotenv import set_key
                        import os
                        
                        client = ChoiceClient(
                            vendor_id=os.environ.get("CHOICE_VENDOR_ID"),
                            vendor_key=os.environ.get("CHOICE_VENDOR_KEY"),
                            api_key=os.environ.get("CHOICE_API_KEY"),
                            base_url=os.environ.get("CHOICE_BASE_URL") or CHOICE_BASE_URL,
                        )
                        
                        mobile_no = os.environ.get("CHOICE_MOBILE_NO")
                        session_id = client.login(mobile_no, otp=totp)
                        
                        from dotenv import find_dotenv
                        env_file = find_dotenv(filename=".env", raise_error_if_not_found=False)
                        if not env_file:
                            for candidate in [_PROJ / ".env", _ROOT / ".env", _HERE / ".env"]:
                                if candidate.exists():
                                    env_file = str(candidate)
                                    break
                        if not env_file:
                            env_file = str(_PROJ / ".env")

                        set_key(env_file, "CHOICE_SESSION_ID", session_id)
                        set_key(env_file, "CHOICE_SESSION_SAVED_AT", datetime.now().isoformat())
                        set_key(env_file, "CHOICE_BASE_URL", client.base_url)
                        
                        st.cache_resource.clear()
                        st.rerun()
                    except Exception as e:
                        st.error(f"Login failed: {e}")
            return  # Stop rendering the rest of the dashboard

        # ── Load state ────────────────────────────────────────────────────────────
        state = _load_state()
        if state is None:
            st.error(f"No state file found for {page}. Run the respective engine first.")
            if is_market_open:
                _js_autorefresh(refresh_sec * 1000)
            return

        saved_at     = state.get("saved_at", "—")
        slots        = state.get("slots", [None, None])
        trade_log_st = state.get("trade_log", [])
        realized     = float(state.get("realized_pnl", 0.0))
        pending_ent  = state.get("pending_entries", [])
        pending_ex   = state.get("pending_exits",   [])
        basket_cs    = state.get("basket_close_series", {})

        binfo = _basket_info(csv_path, None)  # None allows loading all sizes (e.g. size 7 for 5-min)

        active_slots    = [s for s in slots if s is not None]
        held_basket_ids = {s["basket_id"] for s in active_slots}

        # All tickers across held baskets
        all_tickers: set[str] = set()
        for slot in active_slots:
            all_tickers.update(slot.get("tickers", []))

        imap, imap_err = _instrument_map(tuple(sorted(all_tickers))) if all_tickers else ({}, None)

        # ── Fetch data ────────────────────────────────────────────────────────────
        ticker_daily: dict[str, pd.DataFrame] = {}
        ticker_1min:  dict[str, pd.DataFrame] = {}
        fetch_errors: dict[str, str]          = {}

        if all_tickers:
            ph = st.empty()
            ph.info(f"⏳ Fetching data for {len(all_tickers)} tickers (1300 days daily)…")
            for ticker in sorted(all_tickers):
                sym = _sym(imap, ticker)
                df_d, err_d = _fetch_daily(sym, days_back=1300)
                ticker_daily[ticker] = df_d
                if err_d:
                    fetch_errors[ticker] = err_d
                fetch_1min = is_market_open or page == "5-Min Paper Trading"
                if fetch_1min:
                    days_m = 1
                    if page == "5-Min Paper Trading":
                        try:
                            from config import ROLLING_WINDOW as _RW, MIN_ROLLING_POINTS as _MRP, BARS_PER_DAY
                            _trading_days = (_RW + _MRP) // BARS_PER_DAY + 5
                            days_m = int(_trading_days * 10 / 7) + 3           # trading→calendar ≈ 33
                        except Exception:
                            days_m = 35
                    df_m, err_m = _fetch_1min(sym, days_back=days_m)
                    ticker_1min[ticker] = df_m
                    if err_m and ticker not in fetch_errors:
                        fetch_errors[ticker] = err_m
            ph.empty()

        # ── LTP computation ───────────────────────────────────────────────────────
        live_prices: dict[str, float] = {}
        ltp_sources: dict[str, str]   = {}
        for ticker in all_tickers:
            sym = _sym(imap, ticker)
            ltp, src = _get_ltp(
                ticker, sym,
                is_market_open,
                ticker_daily.get(ticker, pd.DataFrame()),
                ticker_1min.get(ticker,  pd.DataFrame()),
            )
            if ltp is not None:
                live_prices[ticker] = ltp
                ltp_sources[ticker] = src

        # ── PnL per slot ──────────────────────────────────────────────────────────
        total_invest   = 0.0
        total_live_val = 0.0
        day_unrealized = 0.0
        slot_info_list = []

        for slot in active_slots:
            bid     = slot["basket_id"]
            tickers = slot.get("tickers", [])
            qtys    = {k: int(v)   for k, v in slot.get("quantities",   {}).items()}
            epx     = {k: float(v) for k, v in slot.get("entry_prices", {}).items()}
            invest  = float(slot.get("investment", 0))
            total_invest += invest

            slot_live   = 0.0
            all_ltp_ok  = True
            ticker_rows = []
            
            slot_entry_date = pd.to_datetime(slot.get("entry_time")).date() if slot.get("entry_time") else None

            for t in tickers:
                ep   = epx.get(t, 0.0)
                qty  = qtys.get(t, 0)
                ltp  = live_prices.get(t)
                src  = ltp_sources.get(t, "—")
                cost = ep * qty
                if ltp is None:
                    # Fallback to entry price so we don't completely zero out the basket
                    ltp = ep
                    mkt = ltp * qty
                    slot_live += mkt
                    ticker_rows.append(dict(ticker=t, ep=ep, qty=qty, ltp=None, cost=cost, mkt=mkt, pnl=0.0, src="entry_price(fallback)"))
                    # day_unrealized doesn't change since ltp == ref_price (if we assume no previous close)
                else:
                    mkt = ltp * qty
                    slot_live += mkt
                    ticker_rows.append(dict(ticker=t, ep=ep, qty=qty, ltp=ltp, cost=cost, mkt=mkt, pnl=mkt - cost, src=src))
                    
                    ref_price = ep
                    if slot_entry_date and slot_entry_date < now_ist.date():
                        df_d = ticker_daily.get(t, pd.DataFrame())
                        if not df_d.empty:
                            past_df = df_d[df_d.index.date < now_ist.date()]
                            if not past_df.empty:
                                ref_price = float(past_df["Close"].iloc[-1])
                    day_unrealized += (ltp - ref_price) * qty

            s_pnl = (slot_live - invest)
            s_pct = (s_pnl / invest * 100) if invest else 0.0
            total_live_val += slot_live

            bi      = binfo.get(bid, {})
            company_map = bi.get("ticker_to_company", {})
            tck_list  = bi.get("tickers", tickers)

            slot_info_list.append(dict(
                basket_id  = bid,
                entry_time = slot.get("entry_time"),
                entry_type = slot.get("entry_type", ""),
                invest     = invest,
                live_val   = slot_live if all_ltp_ok else None,
                pnl        = s_pnl,
                pnl_pct    = s_pct,
                tickers    = ticker_rows,
                company_map= company_map,
                tck_list   = tck_list,
            ))

        unrealized   = (total_live_val - total_invest) if total_live_val else 0.0
        
        day_realized = 0.0
        for tr in trade_log_st:
            close_str = tr.get("close_time") or tr.get("exit_time")
            if close_str:
                if pd.to_datetime(close_str).date() == now_ist.date():
                    day_realized += float(tr.get("pnl", 0.0))
                    
        day_pnl      = day_realized + day_unrealized
        total_equity = POSITION_SIZE + realized + unrealized

        # ══════════════════════════════════════════════════════════════════════════
        # ① METRIC CARDS
        # ══════════════════════════════════════════════════════════════════════════
        c = st.columns(5)
        _metric(c[0], "Total Equity",
                f"<span class='neu'>₹{_format_inr(total_equity)}</span>",
                f"Capital ₹{_format_inr(POSITION_SIZE)}")
        _metric(c[1], "Unrealized P&L",
                f"<span class='{_cls(unrealized)}'>{_fmt(unrealized)}</span>",
                f"{_pct(unrealized/total_invest*100) if total_invest else '—'} on cost")
        _metric(c[2], "Realized P&L",
                f"<span class='{_cls(realized)}'>{_fmt(realized)}</span>",
                f"{len(trade_log_st)} closed trades")
        _metric(c[3], "Day P&L",
                f"<span class='{_cls(day_pnl)}'>{_fmt(day_pnl)}</span>",
                _pct(day_pnl / POSITION_SIZE * 100))
        _metric(c[4], "Open Slots",
                f"<span class='neu'>{len(active_slots)}/2</span>",
                "🟢 Market open" if is_market_open else "🔴 Market closed")

        st.markdown("<br>", unsafe_allow_html=True)

        # ══════════════════════════════════════════════════════════════════════════
        # ② ENGINE STATE (pending orders, basket close series)
        # ══════════════════════════════════════════════════════════════════════════
        st.markdown('<div class="sh">⚙️ Engine State</div>', unsafe_allow_html=True)

        ec1, ec2 = st.columns([1, 1])
        with ec1:
            st.markdown(f"**State saved:** `{saved_at}`")
            if pending_ent:
                st.markdown("**📥 Pending ENTRIES (execute at 09:17):**")
                for pe in pending_ent:
                    bid   = pe.get("basket_id", "?")
                    etype = pe.get("entry_type", "?")
                    tkrs  = binfo.get(bid, {}).get("tickers", [])
                    st.success(f"🧺 Basket {bid} · {etype} · {', '.join(tkrs)}")
            else:
                st.info("No pending entry orders")

            if pending_ex:
                st.markdown("**📤 Pending EXITS (execute at 09:17):**")
                for px in pending_ex:
                    bid    = px.get("basket_id", "?")
                    reason = px.get("reason", "?")
                    st.warning(f"🧺 Basket {bid} · {reason}")
            else:
                st.info("No pending exit orders")

        with ec2:
            if basket_cs:
                st.markdown("**📊 Basket Close Series (last 3 values):**")
                bcs_rows = []
                for bid_str, series in basket_cs.items():
                    if isinstance(series, list) and len(series) >= 2:
                        last3 = series[-3:]
                        bcs_rows.append({
                            "Basket": int(bid_str),
                            "Last Close": round(float(last3[-1]), 2) if last3 else "—",
                            "Prev Close": round(float(last3[-2]), 2) if len(last3) >= 2 else "—",
                            "Bars": len(series),
                        })
                if bcs_rows:
                    st.dataframe(pd.DataFrame(bcs_rows).set_index("Basket"),
                                 width='stretch', height=120)
            else:
                st.info("No basket close series in state")

        # ══════════════════════════════════════════════════════════════════════════
        # ③ HOLDINGS
        # ══════════════════════════════════════════════════════════════════════════
        st.markdown('<div class="sh">🗂 Holdings</div>', unsafe_allow_html=True)

        if not active_slots:
            st.info("No open positions.")
        else:
            for si in slot_info_list:
                bid    = si["basket_id"]
                pnl    = si["pnl"]
                pct    = si["pnl_pct"]
                invest = si["invest"]
                live   = si["live_val"]
                etype  = si["entry_type"].replace("_", " ").title()
                et_str = str(si["entry_time"] or "")[:16]
                pnl_ok = not (isinstance(pnl, float) and np.isnan(pnl))
                company_map = si.get("company_map", {})

                exp_label = (
                    f"🧺 Basket {bid}  ·  {etype}  ·  Entered {et_str}  ·  "
                    + (_fmt(pnl) + " (" + _pct(pct) + ")" if pnl_ok else "⚠️ LTP unavailable")
                )
                with st.expander(exp_label, expanded=True):
                    hdr = st.columns([2.2, 2, 1, 1.3, 1.3, 1.3, 1.8, 1])
                    for col, h in zip(hdr, ["Ticker", "Company", "Qty", "Entry ₹", "LTP ₹", "Cost", "P&L", "Src"]):
                        col.markdown(f"**{h}**")

                    for i, tr in enumerate(si["tickers"]):
                        row = st.columns([2.2, 2, 1, 1.3, 1.3, 1.3, 1.8, 1])
                        t = tr['ticker']
                        comp = company_map.get(t, t)
                        ltp_s = f"₹{tr['ltp']:,.2f}" if tr["ltp"] else "—"
                        pnl_s  = _fmt(tr["pnl"]) if tr["pnl"] is not None else "—"
                        pnl_cl = _cls(tr["pnl"]) if tr["pnl"] is not None else "neu"
                        row[0].markdown(f"**{t}**")
                        row[1].markdown(comp[:22])
                        row[2].markdown(f"{tr['qty']:,}")
                        row[3].markdown(f"₹{tr['ep']:,.2f}")
                        row[4].markdown(ltp_s)
                        row[5].markdown(f"₹{tr['cost']:,.0f}")
                        row[6].markdown(f"<span class='{pnl_cl}'>{pnl_s}</span>", unsafe_allow_html=True)
                        row[7].markdown(f"<span style='color:#8b949e;font-size:.75rem'>{tr['src']}</span>",
                                        unsafe_allow_html=True)

                    st.markdown("---")
                    m1, m2, m3 = st.columns(3)
                    m1.metric("Invested",   f"₹{invest:,.0f}")
                    m2.metric("Live Value", f"₹{live:,.0f}" if live else "—")
                    if pnl_ok:
                        m3.metric("Unrealized", _fmt(pnl), delta=_pct(pct))
                    else:
                        m3.metric("Unrealized", "N/A")

        # ══════════════════════════════════════════════════════════════════════════
        # ④ CHARTS
        # ══════════════════════════════════════════════════════════════════════════
        st.markdown('<div class="sh">📊 Charts</div>', unsafe_allow_html=True)

        if not held_basket_ids:
            st.info("Charts appear when a position is held.")
        else:
            tabs = st.tabs([f"Basket {bid}" for bid in sorted(held_basket_ids)])
            for tab, bid in zip(tabs, sorted(held_basket_ids)):
                with tab:
                    bi      = binfo.get(bid, {})
                    tickers = bi.get("tickers", [])
                    companies = bi.get("companies", [])
                    if not tickers:
                        st.warning(f"Basket {bid} not in CSV")
                        continue

                    slot = next((s for s in slots if s and s["basket_id"] == bid), None)
                    entry_ts = None
                    if slot and slot.get("entry_time"):
                        try:
                            entry_ts = pd.Timestamp(slot["entry_time"])
                            if entry_ts.tzinfo is None:
                                entry_ts = entry_ts.tz_localize(IST)
                        except Exception:
                            entry_ts = None

                    qtys_basket = _basket_quantities(ticker_daily, tickers, POSITION_SIZE, basket_id=bid)
                    if qtys_basket:
                        # Override tickers to avoid multi-size CSV pollution (restricts to the actual held items)
                        tickers = list(qtys_basket.keys())

                    show_basket = chart_mode in ("Basket + Stocks", "Basket only")
                    show_stocks = chart_mode in ("Basket + Stocks", "Stocks only")

                    # ── Daily basket chart ────────────────────────────────────────
                    if show_basket:
                        if page == "Daily Paper Trading":
                            basket_df, binfo_str = _build_basket_df(
                                ticker_daily, ticker_1min, tickers, POSITION_SIZE, is_market_open,
                                basket_id=bid)

                            st.markdown(f"#### Basket {bid} — Daily (history + today)")
                            st.caption(f"📐 {binfo_str}")

                            if basket_df is not None and not basket_df.empty:
                                close = basket_df["Close"]
                                ef, es, bands = _compute_overlays(
                                    close, EMA_FAST, EMA_SLOW,
                                    show_bands, show_ema, ROLLING_WINDOW, MIN_ROLLING_POINTS)

                                entry_px = None
                                if entry_ts is not None:
                                    avail = basket_df.index[basket_df.index <= entry_ts]
                                    if not avail.empty:
                                        entry_px = float(basket_df.loc[avail[-1], "Close"])

                                fig = _chart(
                                    basket_df,
                                    f"Basket {bid}  ·  {', '.join(tickers[:5])}{'…' if len(tickers) > 5 else ''}",
                                    entry_time=entry_ts if entry_px else None,
                                    entry_price=entry_px,
                                    bands=bands, ema_f=ef, ema_s=es, height=500,
                                )
                                st.plotly_chart(fig, width='stretch')
                            else:
                                st.warning(f"Insufficient daily data — {binfo_str}")

                        # Intraday basket
                        if qtys_basket and (is_market_open or page == "5-Min Paper Trading"):
                            rule = "5min" if page == "5-Min Paper Trading" else None
                            label = "5-min" if rule else "1-min"
                            st.markdown(f"#### Basket {bid} — Intraday {label}")
                            ib = _build_intraday_basket(ticker_1min, tickers, qtys_basket, resample_rule=rule)
                            if ib is not None:
                                ib_close = ib["Close"]
                                ib_ef, ib_es, ib_bands = _compute_overlays(
                                    ib_close, CFG_EMA_FAST, CFG_EMA_SLOW,
                                    show_bands, show_ema, CFG_ROLLING_WINDOW, CFG_MIN_ROLLING_POINTS)
                                st.plotly_chart(_chart(ib, f"Basket {bid} — Intraday", height=320, ema_f=ib_ef, ema_s=ib_es, bands=ib_bands, intraday=True),
                                                width='stretch', key=f"intra_basket_{bid}")
                            else:
                                st.info("Intraday basket data not available yet.")

                    # ── Individual stock charts ───────────────────────────────────
                    if show_stocks and tickers:
                        st.markdown(f"#### Individual Stocks — Basket {bid}")
                        n_cols = max(1, min(len(tickers), 3))
                        cols   = st.columns(n_cols)
                        for i, ticker in enumerate(tickers):
                            with cols[i % n_cols]:
                                company = companies[i] if i < len(companies) else ticker
                                ltp     = live_prices.get(ticker)
                                src     = ltp_sources.get(ticker, "")
                                pnl_t   = next((tr["pnl"] for si in slot_info_list
                                               if si["basket_id"] == bid
                                               for tr in si["tickers"]
                                               if tr["ticker"] == ticker), None)

                                # Choose best df based on engine
                                df_s = pd.DataFrame()
                                if page == "5-Min Paper Trading":
                                    df_1m = ticker_1min.get(ticker, pd.DataFrame())
                                    if not df_1m.empty:
                                        from datetime import time as dtime
                                        df_s = df_1m.resample("5min", label="right", closed="right").agg({
                                            "Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"
                                        }).dropna(subset=["Close"])
                                        
                                        # ── MARKET HOURS FILTER (individual stocks) ──────────────────────
                                        df_s = df_s[(df_s.index.time >= dtime(9, 15)) & 
                                                    (df_s.index.time <= dtime(15, 30))]
                                        # ──────────────────────────────────────────────────────────────────

                                else:
                                    # Daily page — always show daily bars regardless of market hours
                                    df_s = ticker_daily.get(ticker, pd.DataFrame())

                                title_parts = [ticker]
                                if company:
                                    title_parts.append(company[:18])
                                if ltp:
                                    title_parts.append(f"₹{ltp:,.2f} [{src}]")
                                if pnl_t is not None:
                                    title_parts.append(f"{'▲' if pnl_t>=0 else '▼'}{_fmt(pnl_t)}")

                                if df_s.empty:
                                    err = fetch_errors.get(ticker, "no data")
                                    st.warning(f"{ticker}: {err}")
                                else:
                                    st.plotly_chart(
                                        _chart(df_s, "  ·  ".join(title_parts), height=300,
                                               intraday=(page == "5-Min Paper Trading")),
                                        width='stretch', key=f"ind_chart_{bid}_{i}_{ticker}")

        # ══════════════════════════════════════════════════════════════════════════
        # ⑤ TRADE LOG
        # ══════════════════════════════════════════════════════════════════════════
        st.markdown('<div class="sh">📋 Trade Log</div>', unsafe_allow_html=True)

        tl = _load_trade_log()
        if tl.empty and trade_log_st:
            tl = pd.DataFrame(trade_log_st)

        # Convert active slots to open trade log entries
        open_rows = []
        for si in slot_info_list:
            et = si.get("entry_time")
            try:
                et_dt = pd.Timestamp(et).tz_localize(IST) if et and pd.Timestamp(et).tzinfo is None else pd.Timestamp(et)
                hold_days = (now_ist - et_dt).days if pd.notna(et_dt) else 0
            except Exception:
                hold_days = 0

            open_rows.append({
                "entry_time": et,
                "exit_time": "—",
                "basket_id": si["basket_id"],
                "basket_tickers": ", ".join(si["tck_list"]),
                "entry_type": si["entry_type"],
                "close_reason": "—",
                "investment": si["invest"],
                "exit_value": si.get("live_val"),
                "pnl": si.get("pnl"),
                "pnl_pct": si.get("pnl_pct"),
                "hold_days": hold_days,
                "status": "open (MTM)"
            })

        if open_rows:
            open_df = pd.DataFrame(open_rows)
            tl = pd.concat([open_df, tl], ignore_index=True) if not tl.empty else open_df

        if not tl.empty:
            # Enrich with basket member tickers from binfo
            if "basket_id" in tl.columns:
                if "basket_tickers" not in tl.columns:
                    tl["basket_tickers"] = ""
                
                def _get_tickers(row):
                    bid = row.get("basket_id")
                    if pd.notna(bid):
                        tcks = binfo.get(int(bid), {}).get("tickers", [])
                        if tcks:
                            return ", ".join(tcks)
                    # fallback to entry_prices
                    ep = row.get("entry_prices")
                    if isinstance(ep, dict):
                        return ", ".join(ep.keys())
                    elif isinstance(ep, str) and ep.startswith("{"):
                        try:
                            import ast
                            return ", ".join(ast.literal_eval(ep).keys())
                        except:
                            pass
                    return ""

                # Only apply to rows where basket_tickers is empty or NaN
                mask = tl["basket_tickers"].isna() | (tl["basket_tickers"] == "")
                tl.loc[mask, "basket_tickers"] = tl.loc[mask].apply(_get_tickers, axis=1)

            if "hold_days" not in tl.columns:
                tl["hold_days"] = np.nan
            
            mask_hd = tl["hold_days"].isna()
            if mask_hd.any() and "hold_minutes" in tl.columns:
                tl.loc[mask_hd, "hold_days"] = (tl.loc[mask_hd, "hold_minutes"].astype(float) / 1440).round(2)

            show_cols = [c for c in [
                "status", "entry_time", "exit_time", "basket_id", "basket_tickers",
                "entry_type", "close_reason",
                "investment", "exit_value", "pnl", "pnl_pct", "hold_days",
            ] if c in tl.columns]

            def _pnl_style(v):
                try:
                    return f"color: {'#3fb950' if float(v) >= 0 else '#f85149'}"
                except Exception:
                    return ""

            styled = tl[show_cols].style
            for col in ["pnl", "pnl_pct"]:
                if col in show_cols:
                    styled = styled.map(_pnl_style, subset=[col])
            st.dataframe(styled, width='stretch', height=280)

            # Cumulative PnL chart
            if "pnl" in tl.columns and len(tl) > 1:
                tl_closed = tl[tl.get("status", "closed") != "open (MTM)"] if "status" in tl.columns else tl
                if len(tl_closed) > 1:
                    cum = tl_closed["pnl"].astype(float).cumsum()
                    colors_pnl = ["#3fb950" if v >= 0 else "#f85149" for v in tl_closed["pnl"]]
                    fig_pnl = go.Figure(go.Scatter(
                        y=cum.values, mode="lines+markers",
                        line=dict(color="#58a6ff", width=2),
                        marker=dict(size=7, color=colors_pnl),
                        fill="tozeroy", fillcolor="rgba(88,166,255,0.07)",
                    ))
                    fig_pnl.update_layout(
                        title="Cumulative Realized PnL", height=240,
                        paper_bgcolor="#0d1117", plot_bgcolor="#0d1117",
                        margin=dict(l=8, r=8, t=38, b=8),
                        yaxis=dict(gridcolor="#21262d", tickprefix="₹",
                                   tickfont=dict(color="#8b949e")),
                        xaxis=dict(gridcolor="#21262d", tickfont=dict(color="#8b949e")),
                    )
                    st.plotly_chart(fig_pnl, width='stretch')
        else:
            st.info("No closed trades yet.")

        # ══════════════════════════════════════════════════════════════════════════
        # ⑥ DEBUG PANEL (sidebar expandable)
        # ══════════════════════════════════════════════════════════════════════════
        with st.sidebar:
            with st.expander("🔍 Debug info"):
                st.json({
                    "broker_ok":     broker_ok,
                    "broker_err":    broker_err,
                    "imap_err":      imap_err,
                    "market_open":   is_market_open,
                    "tickers":       sorted(all_tickers),
                    "fetch_errors":  fetch_errors,
                    "ltp_sources":   ltp_sources,
                    "daily_bars":    {t: len(df) for t, df in ticker_daily.items()},
                    "1min_bars":     {t: len(df) for t, df in ticker_1min.items()},
                    "state_saved_at": saved_at,
                })

        # ── Footer ────────────────────────────────────────────────────────────────
        st.markdown("---")
        st.caption(f"State: {saved_at}  ·  Paper mode only — no real orders  ·  Next refresh in {refresh_sec}s")

        # ── JS auto-refresh (avoids Python 3.14 asyncio event-loop close bug) ────
        if is_market_open:
            _js_autorefresh(refresh_sec * 1000)
    elif page == 'Backtest Engine':
        render_backtest_page()


if __name__ == "__main__":
    main()