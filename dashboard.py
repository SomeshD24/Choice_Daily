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
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap');
  :root{
    --bg:#0b0f14; --surface:#11161d; --surface-2:#161c24; --border:#222a35; --border-2:#2d3644;
    --text:#e6edf3; --muted:#8b96a5; --faint:#5d6875;
    --accent:#4c9aff; --pos:#2fbf71; --neg:#f2545b; --warn:#e3b341;
    --mono:'JetBrains Mono',ui-monospace,Consolas,monospace;
  }
  html,body,.stApp,[class*="css"]{font-family:'Inter',system-ui,-apple-system,'Segoe UI',sans-serif;}
  .stApp{background:var(--bg);color:var(--text);}
  .block-container{padding-top:1.4rem;padding-bottom:3rem;max-width:1560px;}
  header[data-testid="stHeader"]{background:transparent;}
  h1,h2,h3,h4{letter-spacing:-.01em;color:var(--text);}
  h4{font-size:.95rem!important;font-weight:600!important;margin:14px 0 4px!important;}
  hr{border-color:var(--border)!important;margin:1rem 0!important;}

  /* sidebar */
  section[data-testid="stSidebar"]>div{background:var(--surface);border-right:1px solid var(--border);}
  section[data-testid="stSidebar"] .block-container{padding-top:1rem;}
  .sb-brand{display:flex;align-items:center;gap:10px;padding:2px 0 14px;border-bottom:1px solid var(--border);margin-bottom:14px;}
  .sb-logo{width:34px;height:34px;border-radius:8px;background:linear-gradient(135deg,#2f6fd6,#4c9aff);
           display:flex;align-items:center;justify-content:center;font-weight:700;color:#fff;font-size:.85rem;}
  .sb-name{font-weight:700;font-size:1rem;line-height:1.1;}
  .sb-sub{font-size:.72rem;color:var(--muted);}
  .sb-h{font-size:.68rem;font-weight:600;color:var(--faint);text-transform:uppercase;letter-spacing:.1em;margin:18px 0 8px;}
  .sb-row{display:flex;justify-content:space-between;align-items:center;font-size:.82rem;padding:4px 0;color:var(--muted);}
  .sb-row b{color:var(--text);font-weight:500;}

  /* buttons, inputs, tabs, expanders */
  .stButton>button{border-radius:8px;font-weight:500;}
  button[kind="secondary"]{background:var(--surface-2)!important;border:1px solid var(--border-2)!important;color:var(--text)!important;}
  button[kind="secondary"]:hover{border-color:var(--accent)!important;}
  .stTabs [data-baseweb="tab-list"]{gap:4px;border-bottom:1px solid var(--border);}
  .stTabs [data-baseweb="tab"]{background:transparent;border-radius:8px 8px 0 0;padding:8px 16px;font-weight:500;color:var(--muted);}
  .stTabs [aria-selected="true"]{color:var(--text)!important;background:var(--surface)!important;}
  [data-testid="stExpander"]{border:1px solid var(--border)!important;border-radius:10px!important;background:var(--surface);}
  [data-testid="stDataFrame"]{border:1px solid var(--border);border-radius:10px;overflow:hidden;}
  [data-testid="stVerticalBlockBorderWrapper"]{border-color:var(--border)!important;border-radius:12px!important;background:var(--surface);}
  [data-testid="stMetric"]{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:12px 14px;}
  [data-testid="stMetricLabel"]{color:var(--muted);}

  /* page header */
  .ph{display:flex;justify-content:space-between;align-items:flex-end;gap:16px;flex-wrap:wrap;
      padding-bottom:14px;border-bottom:1px solid var(--border);margin-bottom:18px;}
  .ph-t{font-size:1.55rem;font-weight:700;line-height:1.2;}
  .ph-s{font-size:.85rem;color:var(--muted);margin-top:3px;}
  .ph-r{display:flex;gap:8px;flex-wrap:wrap;align-items:center;}

  /* status pills */
  .pill{display:inline-flex;align-items:center;gap:6px;padding:4px 10px;border-radius:999px;font-size:.75rem;font-weight:500;
        border:1px solid var(--border-2);background:var(--surface);color:var(--muted);white-space:nowrap;}
  .pill i{width:7px;height:7px;border-radius:50%;display:inline-block;background:var(--faint);}
  .pill.ok i{background:var(--pos);box-shadow:0 0 0 3px rgba(47,191,113,.15);} .pill.ok{color:var(--text);}
  .pill.bad i{background:var(--neg);} .pill.warn i{background:var(--warn);}

  /* metric cards */
  .mc{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:14px 16px;margin-bottom:6px;min-height:104px;}
  .ml{font-size:.7rem;color:var(--muted);text-transform:uppercase;letter-spacing:.08em;font-weight:600;margin-bottom:8px;}
  .mv{font-size:clamp(1.05rem,1.55vw,1.5rem);font-weight:600;font-family:var(--mono);white-space:nowrap;
      font-variant-numeric:tabular-nums;letter-spacing:-.02em;}
  .ms{font-size:.75rem;color:var(--muted);margin-top:6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
  .pos{color:var(--pos);} .neg{color:var(--neg);} .neu{color:var(--text);}

  /* section headers */
  .sh{display:flex;align-items:baseline;gap:10px;margin:28px 0 12px;padding-bottom:8px;border-bottom:1px solid var(--border);}
  .sh-t{font-size:1rem;font-weight:600;color:var(--text);}
  .sh-s{font-size:.78rem;color:var(--muted);}
  .tag{background:var(--surface-2);border:1px solid var(--border);border-radius:6px;padding:2px 8px;font-size:.72rem;color:var(--muted);margin-right:4px;}

  /* order cards */
  .oc{display:flex;gap:12px;align-items:flex-start;background:var(--surface);border:1px solid var(--border);
      border-left:3px solid var(--accent);border-radius:10px;padding:10px 14px;margin-bottom:8px;}
  .oc.sell{border-left-color:var(--neg);} .oc.buy{border-left-color:var(--pos);}
  .oc-k{font-size:.68rem;font-weight:700;letter-spacing:.08em;padding:2px 8px;border-radius:6px;margin-top:2px;}
  .oc.buy .oc-k{background:rgba(47,191,113,.12);color:var(--pos);} .oc.sell .oc-k{background:rgba(242,84,91,.12);color:var(--neg);}
  .oc-t{font-weight:600;font-size:.9rem;} .oc-s{font-size:.78rem;color:var(--muted);margin-top:2px;}
  .empty{border:1px dashed var(--border-2);border-radius:10px;padding:14px;color:var(--muted);font-size:.85rem;text-align:center;}

  /* holdings table */
  .hb-h{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:10px;}
  .hb-t{font-size:1.02rem;font-weight:600;} .hb-s{font-size:.78rem;color:var(--muted);margin-top:2px;}
  .hb-p{font-family:var(--mono);font-weight:600;font-size:1.05rem;text-align:right;}
  .tbl{width:100%;border-collapse:collapse;font-size:.84rem;}
  .tbl th{font-size:.68rem;text-transform:uppercase;letter-spacing:.07em;color:var(--muted);font-weight:600;
          text-align:right;padding:8px 10px;border-bottom:1px solid var(--border-2);white-space:nowrap;}
  .tbl td{padding:8px 10px;border-bottom:1px solid var(--border);text-align:right;white-space:nowrap;
          font-family:var(--mono);font-variant-numeric:tabular-nums;}
  .tbl th.l,.tbl td.l{text-align:left;font-family:inherit;}
  .tbl tr:hover td{background:rgba(76,154,255,.04);}
  .tbl tfoot td{border-bottom:none;border-top:1px solid var(--border-2);font-weight:600;}
  .tbl .src{color:var(--faint);font-size:.72rem;font-family:inherit;}
  .tbl-wrap{overflow-x:auto;}
  .foot{color:var(--faint);font-size:.75rem;margin-top:28px;padding-top:12px;border-top:1px solid var(--border);}
</style>
""", unsafe_allow_html=True)

# ── Chart theme: one look for every Plotly chart ───────────────────────────────
import plotly.io as pio
_C = dict(bg="#0b0f14", surface="#11161d", grid="#1c232d", axis="#2d3644", text="#e6edf3",
          muted="#8b96a5", accent="#4c9aff", pos="#2fbf71", neg="#f2545b", warn="#e3b341", violet="#a78bfa")
pio.templates["adts"] = go.layout.Template(layout=go.Layout(
    font=dict(family="Inter, system-ui, sans-serif", color=_C["muted"], size=11),
    paper_bgcolor=_C["surface"], plot_bgcolor=_C["surface"],
    colorway=[_C["accent"], _C["warn"], _C["violet"], _C["pos"], _C["neg"], "#22d3ee"],
    xaxis=dict(gridcolor=_C["grid"], linecolor=_C["axis"], zerolinecolor=_C["grid"], tickfont=dict(size=10)),
    yaxis=dict(gridcolor=_C["grid"], linecolor=_C["axis"], zerolinecolor=_C["grid"], tickfont=dict(size=10), side="right"),
    hoverlabel=dict(bgcolor=_C["bg"], bordercolor=_C["axis"], font=dict(family="JetBrains Mono, monospace", size=11)),
    legend=dict(bgcolor="rgba(0,0,0,0)", font=dict(size=10, color=_C["muted"])),
    title=dict(font=dict(size=13, color=_C["text"]), x=0.01, xanchor="left"),
    margin=dict(l=10, r=10, t=46, b=10),
))
pio.templates.default = "adts"


def _style_fig(fig: go.Figure) -> go.Figure:
    """Apply the dashboard look to a figure built elsewhere (e.g. backtest module)."""
    fig.update_layout(template="adts", paper_bgcolor=_C["surface"], plot_bgcolor=_C["surface"],
                      font=dict(family="Inter, system-ui, sans-serif", color=_C["muted"]))
    fig.update_xaxes(gridcolor=_C["grid"], linecolor=_C["axis"])
    fig.update_yaxes(gridcolor=_C["grid"], linecolor=_C["axis"])
    return fig


def _section(title: str, sub: str = ""):
    st.markdown(f'<div class="sh"><span class="sh-t">{title}</span>'
                + (f'<span class="sh-s">{sub}</span>' if sub else "") + "</div>", unsafe_allow_html=True)


def _pill(text: str, kind: str = "") -> str:
    return f'<span class="pill {kind}"><i></i>{text}</span>'


def _page_header(title: str, sub: str = "", pills: list[str] | None = None):
    st.markdown(f'<div class="ph"><div><div class="ph-t">{title}</div>'
                + (f'<div class="ph-s">{sub}</div>' if sub else "")
                + f'</div><div class="ph-r">{"".join(pills or [])}</div></div>', unsafe_allow_html=True)


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
    s = "+" if (sign and v >= 0) else ("-" if v < 0 else "")
    return f"{s}{prefix}{_format_inr(abs(v))}" if prefix else f"{s}{_format_inr(abs(v))}"

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
        return _ROOT / "state" / "portfolio_state.db"
    for p in [
        _ROOT / "state" / "daily_portfolio_state.db",
        _PROJ / "state" / "daily_portfolio_state.db",
        Path("state/daily_portfolio_state.db"),
    ]:
        if p.exists():
            return p
    return None

def _load_state() -> dict | None:
    p = _state_path()
    if p is None:
        return None
    try:
        from state_store import load_state_dict
        return load_state_dict(str(p))
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
        import sys, types
        try:
            import websocket
        except ImportError:
            sys.modules['websocket'] = types.ModuleType('websocket')

        from choice_api import ChoiceClient, BASE_URL_FINX
        from config_daily import CHOICE_BASE_URL, SESSION_FILE
        client = ChoiceClient(
            vendor_id=vendor_id,
            api_key=os.environ.get("CHOICE_API_KEY"),
            base_url=os.environ.get("CHOICE_BASE_URL") or CHOICE_BASE_URL or BASE_URL_FINX,
        )
        
        session_path = Path(SESSION_FILE)
        if session_path.exists() and client.load_session(str(session_path)):
            return client, client, None

        saved_at = os.environ.get("CHOICE_SESSION_SAVED_AT", "")
        cached   = os.environ.get("CHOICE_SESSION_ID", "").strip()
        if cached and saved_at and _session_is_same_day(saved_at):
            client.session_id = cached
            return client, client, None
            
        # Try automated OTP fetch before falling back to manual prompt
        mobile_no = os.environ.get("CHOICE_MOBILE_NO")
        if mobile_no:
            try:
                session_id = client.login(mobile_no)
                if session_id:
                    client.save_session(str(session_path))
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
def _fetch_daily(trading_symbol: str, days_back: int = 1300) -> tuple[pd.DataFrame, str | None]:
    try:
        from symbol_mapper import ticker_to_token
        from data_manager_daily import fetch_daily_historical
        import pytz
        from datetime import datetime, timedelta
        
        IST = pytz.timezone("Asia/Kolkata")
        now = datetime.now(IST)
        from_dt = now - timedelta(days=days_back)
        
        client, _, _ = _broker()
        token = ticker_to_token(trading_symbol) or 1
            
        df = fetch_daily_historical(client, 1, token, trading_symbol, from_dt.replace(tzinfo=None), now.replace(tzinfo=None))
        if df.empty:
            return pd.DataFrame(), f"{trading_symbol}: 0 daily rows returned"
        return df, None
    except Exception as e:
        return pd.DataFrame(), str(e)


@st.cache_data(ttl=60, show_spinner=False)
def _fetch_1min(trading_symbol: str, days_back: int = 5) -> tuple[pd.DataFrame, str | None]:
    client, _, err = _broker()
    try:
        from symbol_mapper import ticker_to_token
        import pytz
        from datetime import datetime, timedelta
        import yfinance as yf
        
        IST = pytz.timezone("Asia/Kolkata")
        now = datetime.now(IST)
        from_dt = now - timedelta(days=days_back)
        
        df = None
        # 1. Try Choice FinX API if client is authenticated
        if client is not None and client.session_id:
            token = ticker_to_token(trading_symbol)
            if token:
                from_str = from_dt.strftime("%Y-%m-%d %H:%M:%S")
                now_str = now.strftime("%Y-%m-%d %H:%M:%S")
                try:
                    df = client.historical.get_historical_data(
                        1, token, from_str, now_str, resolution="1"
                    )
                except Exception:
                    df = None

        # 2. If Choice API data is unavailable, fallback to yfinance 5m intraday
        if df is None or df.empty:
            try:
                yf_df = yf.download(trading_symbol, period=f"{days_back}d", interval="5m", progress=False)
                if not yf_df.empty:
                    if isinstance(yf_df.columns, pd.MultiIndex):
                        yf_df.columns = yf_df.columns.get_level_values(0)
                    if yf_df.index.tz is None:
                        yf_df.index = yf_df.index.tz_localize(IST)
                    else:
                        yf_df.index = yf_df.index.tz_convert(IST)
                    yf_df.index.name = None
                    cols = ["Open", "High", "Low", "Close", "Volume"]
                    for c in cols:
                        if c not in yf_df.columns:
                            yf_df[c] = 0.0
                    df_5min = yf_df[cols].apply(pd.to_numeric, errors="coerce").dropna(subset=["Close"]).sort_index()
                    if not df_5min.empty:
                        return df_5min, None
            except Exception:
                pass
            return pd.DataFrame(), f"{trading_symbol}: 0 intraday rows"
        
        df = df.copy()
        if "Time" in df.columns:
            df["Time"] = pd.to_datetime(df["Time"])
            if df["Time"].dt.tz is None:
                df["Time"] = df["Time"].dt.tz_localize(IST, nonexistent="shift_forward", ambiguous="NaT")
            else:
                df["Time"] = df["Time"].dt.tz_convert(IST)
            df.set_index("Time", inplace=True)
        elif df.index.tz is None:
            df.index = df.index.tz_localize(IST)
        else:
            df.index = df.index.tz_convert(IST)
        
        df.index.name = None
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
           exit_time=None, exit_price=None,
           bands=None, ema_f=None, ema_s=None,
           height: int = 450,
           intraday: bool = False) -> go.Figure:

    fig = make_subplots(rows=2, cols=1, shared_xaxes=True,
                        row_heights=[0.78, 0.22], vertical_spacing=0.02)

    fig.add_trace(go.Candlestick(
        x=df.index, open=df["Open"], high=df["High"],
        low=df["Low"], close=df["Close"], name="Price",
        increasing=dict(line=dict(color=_C["pos"], width=1), fillcolor="rgba(47,191,113,.55)"),
        decreasing=dict(line=dict(color=_C["neg"], width=1), fillcolor="rgba(242,84,91,.55)"),
    ), row=1, col=1)

    if bands is not None and not bands.empty:
        bi = bands.index.intersection(df.index)
        if not bi.empty:
            b = bands.loc[bi].dropna()
            fig.add_trace(go.Scatter(x=b.index, y=b["upper2"], name="+2σ",
                line=dict(color="rgba(242,84,91,.6)", width=1, dash="dot")), row=1, col=1)
            fig.add_trace(go.Scatter(x=b.index, y=b["trend_line"], name="Trend",
                line=dict(color="rgba(76,154,255,.75)", width=1)), row=1, col=1)
            fig.add_trace(go.Scatter(x=b.index, y=b["lower2"], name="-2σ",
                line=dict(color="rgba(47,191,113,.6)", width=1, dash="dot"),
                fill="tonexty", fillcolor="rgba(76,154,255,0.05)"), row=1, col=1)

    if ema_f is not None:
        fig.add_trace(go.Scatter(x=df.index, y=ema_f.reindex(df.index), name="EMA Fast",
            line=dict(color=_C["warn"], width=1.3)), row=1, col=1)
    if ema_s is not None:
        fig.add_trace(go.Scatter(x=df.index, y=ema_s.reindex(df.index), name="EMA Slow",
            line=dict(color=_C["violet"], width=1.5)), row=1, col=1)

    if entry_time is not None and entry_price is not None:
        fig.add_trace(go.Scatter(
            x=[entry_time], y=[entry_price], mode="markers+text",
            marker=dict(symbol="triangle-up", size=14, color=_C["pos"], line=dict(color=_C["bg"], width=1)),
            text=["BUY"], textposition="top center",
            textfont=dict(color=_C["pos"], size=10, family="Inter"), name="Entry",
        ), row=1, col=1)

    if exit_time is not None and exit_price is not None:
        fig.add_trace(go.Scatter(
            x=[exit_time], y=[exit_price], mode="markers+text",
            marker=dict(symbol="triangle-down", size=14, color=_C["neg"], line=dict(color=_C["bg"], width=1)),
            text=["SELL"], textposition="bottom center",
            textfont=dict(color=_C["neg"], size=10, family="Inter"), name="Exit",
        ), row=1, col=1)

    colors = [_C["pos"] if c >= o else _C["neg"]
              for o, c in zip(df["Open"], df["Close"])]
    fig.add_trace(go.Bar(x=df.index, y=df["Volume"], name="Volume",
        marker_color=colors, opacity=0.4, showlegend=False), row=2, col=1)

    fig.update_layout(
    template="adts",
    title=dict(text=title),
    height=height,
    xaxis_rangeslider_visible=False,
    hovermode="x unified",
    legend=dict(orientation="h", yanchor="bottom", y=1.01, xanchor="right", x=1),
    margin=dict(l=10, r=10, t=52, b=10),
    bargap=0.15,
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

    fig.update_xaxes(gridcolor=_C["grid"], linecolor=_C["axis"], showspikes=True, spikecolor=_C["axis"],
                     spikethickness=1, spikedash="dot", spikemode="across")
    fig.update_yaxes(gridcolor=_C["grid"], linecolor=_C["axis"], side="right")
    fig.update_yaxes(showgrid=False, showticklabels=False, row=2, col=1)
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
    _page_header("Backtest Engine", "OR-gate strategy · 6-stock baskets · 2 slots · run on full price history",
                 [_pill("Basket size 6"), _pill("2 slots")])

    st.sidebar.markdown('<div class="sb-h">Backtest settings</div>', unsafe_allow_html=True)
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
    if not results:
        st.markdown('<div class="empty">Set the parameters in the sidebar and press <b>Run Backtest</b>.</div>',
                    unsafe_allow_html=True)
    if results:
        m = results["metrics"]
        sv = lambda v, fmt: f"<span class='{_cls(v)}'>{fmt.format(v)}</span>"

        _section("Performance", "full history")
        c = st.columns(4)
        _metric(c[0], "CAGR", sv(m.get('cagr_pct', 0), "{:+.2f}%"), "compound growth per year")
        _metric(c[1], "Max Drawdown", f"<span class='neg'>{m.get('max_drawdown_pct', 0):.2f}%</span>", "worst fall from a peak")
        _metric(c[2], "Sharpe", f"<span class='neu'>{m.get('sharpe', 0):.3f}</span>", "risk-adjusted return")
        _metric(c[3], "Win Rate", f"<span class='neu'>{m.get('win_rate_pct', 0):.1f}%</span>", "closed trades in profit")
        c = st.columns(4)
        _metric(c[0], "Total Return", sv(m.get('total_return_pct', 0), "{:+.2f}%"))
        _metric(c[1], "Total P&L", f"<span class='{_cls(m.get('total_pnl', 0))}'>₹{_format_inr(m.get('total_pnl', 0))}</span>")
        _metric(c[2], "Trades", f"<span class='neu'>{m.get('closed_trades', 0)}</span>", "closed")
        _metric(c[3], "Profit Factor", f"<span class='neu'>{m.get('profit_factor', 0):.3f}</span>", "gross profit / gross loss")

        if results["eq_fig"]:
            _section("Equity curve")
            st.plotly_chart(_style_fig(results["eq_fig"]), width='stretch')

        _section("Breakdowns")
        col1, col2 = st.columns(2)
        with col1:
            st.markdown("#### By entry type")
            if not results.get("ebd", pd.DataFrame()).empty:
                st.dataframe(results["ebd"], width='stretch')
            st.markdown("#### By exit reason")
            if not results.get("xbd", pd.DataFrame()).empty:
                st.dataframe(results["xbd"], width='stretch')
        with col2:
            st.markdown("#### By basket (top 10)")
            if not results.get("bbd", pd.DataFrame()).empty:
                st.dataframe(results["bbd"].head(10), width='stretch')

        if results.get("best_figs"):
            _section("Top 5 best trades")
            for fig in results["best_figs"]:
                st.plotly_chart(_style_fig(fig), width='stretch')

        if results.get("worst_figs"):
            _section("Top 5 worst trades")
            for fig in results["worst_figs"]:
                st.plotly_chart(_style_fig(fig), width='stretch')

def save_engine_config(config_file: str, params: dict):
    import re
    with open(config_file, "r", encoding="utf-8") as f:
        content = f.read()
    
    for k, v in params.items():
        # Match 'KEY = VALUE' preserving comments if any
        content = re.sub(rf"^({k}\s*=\s*).*?(\s*(?:#.*)?)$", rf"\g<1>{v}\g<2>", content, flags=re.MULTILINE)
        
    with open(config_file, "w", encoding="utf-8") as f:
        f.write(content)

def render_exited_baskets_page():
    _page_header("Exited Baskets", "Every closed basket trade of the daily paper account, newest first",
                 [_pill("Daily engine")])

    db_path = _ROOT / "state" / "daily_portfolio_state.db"
    if not db_path.exists():
        st.warning("Daily portfolio state database not found.")
        return
        
    try:
        import sqlite3
        with sqlite3.connect(db_path, timeout=10) as conn:
            query = """
                SELECT 
                    basket_id AS 'Basket ID',
                    entry_time AS 'Entry Time',
                    exit_time AS 'Exit Time',
                    entry_type AS 'Entry Strategy',
                    exit_reason AS 'Exit Reason',
                    investment AS 'Investment (₹)',
                    exit_value AS 'Exit Value (₹)',
                    pnl AS 'PnL (₹)',
                    pnl_pct AS 'PnL (%)',
                    quantities,
                    entry_prices,
                    exit_prices
                FROM trade_log
                WHERE exit_time IS NOT NULL AND exit_time != ''
                ORDER BY exit_time DESC
            """
            df = pd.read_sql_query(query, conn)
            
        if df.empty:
            st.info("No exited baskets found in the daily trade log.")
        else:
            import json
            
            def format_stock_details(row):
                try:
                    qtys = json.loads(row['quantities']) if row['quantities'] else {}
                    ep = json.loads(row['entry_prices']) if row['entry_prices'] else {}
                    xp = json.loads(row['exit_prices']) if row['exit_prices'] else {}
                    
                    details = []
                    for tkr, q in qtys.items():
                        e_price = ep.get(tkr, 0)
                        x_price = xp.get(tkr, 0)
                        details.append(f"{tkr}: {q} qty (₹{e_price:,.2f} -> ₹{x_price:,.2f})")
                    return " | ".join(details)
                except:
                    return ""
                    
            df['Stock Details'] = df.apply(format_stock_details, axis=1)

            # Drop the raw JSON columns before display
            df = df.drop(columns=['quantities', 'entry_prices', 'exit_prices'])

            # Summary (display only)
            pnl_vals = pd.to_numeric(df['PnL (₹)'], errors='coerce').fillna(0.0)
            n, total = len(df), float(pnl_vals.sum())
            wins = int((pnl_vals > 0).sum())
            c = st.columns(4)
            _metric(c[0], "Closed trades", f"<span class='neu'>{n}</span>")
            _metric(c[1], "Total realized P&L", f"<span class='{_cls(total)}'>{_fmt(total)}</span>")
            _metric(c[2], "Winning trades", f"<span class='neu'>{wins} / {n}</span>", f"{wins / n * 100:.0f}% win rate" if n else "")
            _metric(c[3], "Best / worst", f"<span class='pos'>{_fmt(float(pnl_vals.max()))}</span>",
                    f"worst {_fmt(float(pnl_vals.min()))}")

            for col in ('Entry Time', 'Exit Time'):
                df[col] = pd.to_datetime(df[col], errors='coerce').dt.strftime('%d %b %Y  %H:%M')
            for col in ('Entry Strategy', 'Exit Reason'):
                df[col] = df[col].fillna('').astype(str).str.replace('_', ' ').str.title()

            # Format numbers
            df['Investment (₹)'] = df['Investment (₹)'].map('{:,.0f}'.format)
            df['Exit Value (₹)'] = df['Exit Value (₹)'].map('{:,.0f}'.format)
            df['PnL (₹)'] = df['PnL (₹)'].map('{:+,.0f}'.format)
            df['PnL (%)'] = df['PnL (%)'].map('{:+,.2f}%'.format)

            # Styling PnL
            def color_pnl(val):
                if isinstance(val, str):
                    if val.startswith('+'):
                        return f'color: {_C["pos"]}; font-weight: 600'
                    elif val.startswith('-'):
                        return f'color: {_C["neg"]}; font-weight: 600'
                return ''

            _section("Trade history")
            st.dataframe(df.style.map(color_pnl, subset=['PnL (₹)', 'PnL (%)']), width='stretch', hide_index=True,
                         column_config={"Basket ID": st.column_config.NumberColumn("Basket", format="B%d"),
                                        "Stock Details": st.column_config.TextColumn("Stocks (qty, entry → exit)", width="large")})
            
    except Exception as e:
        st.error(f"Error reading trade log: {e}")


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
    elif page == "Daily Paper Trading":
        import config_daily
        importlib.reload(config_daily)
        from config_daily import (
            BASKET_CSV_PATH, TARGET_BASKET_SIZE, POSITION_SIZE as CONFIG_POSITION_SIZE,
            EMA_FAST, EMA_SLOW, ROLLING_WINDOW, MIN_ROLLING_POINTS
        )
    else:
        # Dummy values for other pages so it doesn't crash before reaching the routing
        CONFIG_POSITION_SIZE = 100_000
        BASKET_CSV_PATH = "data/baskets.csv"

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
        st.markdown('<div class="sb-brand"><div class="sb-logo">AD</div><div>'
                    '<div class="sb-name">ADTS</div><div class="sb-sub">Basket paper trading</div></div></div>',
                    unsafe_allow_html=True)

        # Persist nav_page in URL query params so JS location.reload() doesn't
        # reset it to the default.  On a fresh load, seed session_state from URL.
        _pages = ["Daily Paper Trading", "5-Min Paper Trading", "Exited Baskets (Daily)", "Backtest Engine"]
        if "nav_page" not in st.session_state:
            _qp = st.query_params.get("page", "Daily Paper Trading")
            st.session_state["nav_page"] = _qp if _qp in _pages else "Daily Paper Trading"

        st.markdown('<div class="sb-h">Navigation</div>', unsafe_allow_html=True)
        page = st.radio("Navigation", _pages, key="nav_page", label_visibility="collapsed")

        # Write current page back to URL so reload picks it up
        st.query_params["page"] = page

        engine_running = False
        if page in ["Daily Paper Trading", "5-Min Paper Trading"]:
            # Engine Control
            st.markdown('<div class="sb-h">Engine</div>', unsafe_allow_html=True)
            engine_running = is_engine_running()
            st.markdown(_pill("Running", "ok") if engine_running else _pill("Stopped", "bad"),
                        unsafe_allow_html=True)
            if engine_running:
                if st.button("Stop engine", width='stretch'):
                    stop_engine()
                    st.rerun()
            else:
                if st.button("Start engine", type="primary", width='stretch'):
                    start_engine()
                    st.rerun()

            st.markdown('<div class="sb-h">Display</div>', unsafe_allow_html=True)
            refresh_sec = st.slider("Auto-refresh (seconds)", 30, 300, 60, 15)
            show_bands  = st.toggle("Regression bands (±2σ)", value=True)
            show_ema    = st.toggle("EMA lines",              value=True)
            chart_mode  = st.radio("Charts", ["Basket + Stocks", "Basket only", "Stocks only"], index=0)

            with st.expander("Engine settings"):
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

            st.markdown('<div class="sb-h">Status</div>', unsafe_allow_html=True)
            st.markdown(
                f'<div class="sb-row">Broker {_pill("Connected", "ok") if broker_ok else _pill("Offline", "bad")}</div>'
                f'<div class="sb-row">Market {_pill("Open", "ok") if is_market_open else _pill("Closed", "")}</div>'
                f'<div class="sb-row">Time <b>{now_ist.strftime("%H:%M:%S IST")}</b></div>',
                unsafe_allow_html=True,
            )
            if broker_err:
                st.caption(f"{broker_err[:80]}")
            if st.button("Clear cache & refresh", width='stretch'):
                st.cache_data.clear()
                st.rerun()
            st.caption(f"Auto-refresh every {refresh_sec}s while the market is open")

    if page in ["Daily Paper Trading", "5-Min Paper Trading"]:
        # ── Header ────────────────────────────────────────────────────────────────
        _page_header(
            page,
            f"Paper account · {now_ist.strftime('%A, %d %b %Y · %H:%M IST')}",
            [_pill("Engine running", "ok") if engine_running else _pill("Engine stopped", "bad"),
             _pill("Market open", "ok") if is_market_open else _pill("Market closed"),
             _pill("Broker connected", "ok") if broker_ok else _pill("Broker offline", "warn"),
             _pill("Paper mode")],
        )

        # ── Authentication ────────────────────────────────────────────────────────
        if broker_err == "AUTH_REQUIRED":
            st.error("🔒 **Broker Authentication Required**")
            st.markdown("A daily Choice OTP login is required to fetch live market data and execute trades.")
            with st.form("totp_form"):
                totp = st.text_input("Enter Choice OTP", max_chars=6)
                submit = st.form_submit_button("Login")
                
                if submit and totp:
                    try:
                        import sys, types
                        try:
                            import websocket
                        except ImportError:
                            sys.modules['websocket'] = types.ModuleType('websocket')

                        from choice_api import ChoiceClient, BASE_URL_FINX
                        from config_daily import CHOICE_BASE_URL, SESSION_FILE
                        from dotenv import set_key
                        import os
                        
                        client = ChoiceClient(
                            vendor_id=os.environ.get("CHOICE_VENDOR_ID"),
                            api_key=os.environ.get("CHOICE_API_KEY"),
                            base_url=os.environ.get("CHOICE_BASE_URL") or CHOICE_BASE_URL or BASE_URL_FINX,
                        )
                        
                        mobile_no = os.environ.get("CHOICE_MOBILE_NO")
                        session_id = client.login(mobile_no)
                        client.save_session(SESSION_FILE)
                        
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

        binfo = _basket_info(csv_path, TARGET_BASKET_SIZE)

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
                f"Capital ₹{_format_inr(POSITION_SIZE)} · {_pct((total_equity / POSITION_SIZE - 1) * 100) if POSITION_SIZE else '—'}")
        _metric(c[1], "Today's P&L",
                f"<span class='{_cls(day_pnl)}'>{_fmt(day_pnl)}</span>",
                f"{_pct(day_pnl / total_equity * 100) if total_equity else '—'} of equity")
        _metric(c[2], "Unrealized P&L",
                f"<span class='{_cls(unrealized)}'>{_fmt(unrealized)}</span>",
                f"{_pct(unrealized/total_invest*100) if total_invest else '—'} on cost")
        _metric(c[3], "Realized P&L",
                f"<span class='{_cls(realized)}'>{_fmt(realized)}</span>",
                f"{len(trade_log_st)} closed trades")
        _metric(c[4], "Open Slots",
                f"<span class='neu'>{len(active_slots)} / 2</span>",
                ", ".join(f"B{s['basket_id']}" for s in active_slots) or "all slots free")

        # ══════════════════════════════════════════════════════════════════════════
        # ② ENGINE STATE (pending orders, basket close series)
        # ══════════════════════════════════════════════════════════════════════════
        _section("Engine state", f"state saved {str(saved_at)[:19].replace('T', ' ')}")

        ec1, ec2 = st.columns([1, 1])
        with ec1:
            st.markdown("#### Orders for the next open (09:17)")
            if not pending_ent and not pending_ex:
                st.markdown('<div class="empty">No orders queued for the next open</div>', unsafe_allow_html=True)
            for pe in pending_ent:
                bid   = pe.get("basket_id", "?")
                etype = str(pe.get("entry_type", "?")).replace("_", " ").title()
                tkrs  = binfo.get(bid, {}).get("tickers", [])
                extra = ""
                evict_idx = pe.get("evict_idx", -1)
                if pe.get("needs_eviction") and evict_idx >= 0 and evict_idx < len(slots) and slots[evict_idx]:
                    extra = f" · replaces Basket {slots[evict_idx].get('basket_id', '?')}"
                st.markdown(f'<div class="oc buy"><span class="oc-k">BUY</span><div>'
                            f'<div class="oc-t">Basket {bid} · {etype}{extra}</div>'
                            f'<div class="oc-s">{", ".join(t.replace(".NS", "") for t in tkrs)}</div></div></div>',
                            unsafe_allow_html=True)
            for px in pending_ex:
                si_ = px.get("slot_idx")
                bid = px.get("basket_id") or (slots[si_].get("basket_id") if isinstance(si_, int)
                                              and 0 <= si_ < len(slots) and slots[si_] else "?")
                reason = str(px.get("reason", "?")).replace("_", " ")
                st.markdown(f'<div class="oc sell"><span class="oc-k">SELL</span><div>'
                            f'<div class="oc-t">Basket {bid}</div><div class="oc-s">{reason}</div></div></div>',
                            unsafe_allow_html=True)

        with ec2:
            st.markdown("#### Basket close series")
            bcs_rows = []
            for bid_str, series in (basket_cs or {}).items():
                vals = ([series[k] for k in sorted(series)] if isinstance(series, dict)
                        else list(series) if isinstance(series, list) else [])
                if len(vals) >= 2:
                    bcs_rows.append({
                        "Basket": int(bid_str),
                        "Last close": round(float(vals[-1]), 2),
                        "Prev close": round(float(vals[-2]), 2),
                        "Change %": round((float(vals[-1]) / float(vals[-2]) - 1) * 100, 2) if float(vals[-2]) else 0.0,
                        "Bars": len(vals),
                    })
            if bcs_rows:
                st.dataframe(pd.DataFrame(bcs_rows).sort_values("Basket").set_index("Basket"),
                             width='stretch', height=min(36 * len(bcs_rows) + 40, 260),
                             column_config={"Last close": st.column_config.NumberColumn(format="%.2f"),
                                            "Prev close": st.column_config.NumberColumn(format="%.2f"),
                                            "Change %": st.column_config.NumberColumn(format="%+.2f%%")})
            else:
                st.markdown('<div class="empty">No basket close series in state</div>', unsafe_allow_html=True)

        # ══════════════════════════════════════════════════════════════════════════
        # ③ HOLDINGS
        # ══════════════════════════════════════════════════════════════════════════
        _section("Holdings", f"{len(active_slots)} of 2 slots in use")

        if not active_slots:
            st.markdown('<div class="empty">No open positions — the engine is waiting for the next buy signal.</div>',
                        unsafe_allow_html=True)
        else:
            for si in slot_info_list:
                bid    = si["basket_id"]
                pnl    = si["pnl"]
                pct    = si["pnl_pct"]
                invest = si["invest"]
                live   = si["live_val"]
                etype  = si["entry_type"].replace("_", " ").title()
                pnl_ok = not (isinstance(pnl, float) and np.isnan(pnl))
                company_map = si.get("company_map", {})
                try:
                    _et = pd.Timestamp(si["entry_time"])
                    _et = _et.tz_localize(IST) if _et.tzinfo is None else _et
                    et_str = f"since {_et.strftime('%d %b %Y')} · {(now_ist - _et).days} days held"
                except Exception:
                    et_str = str(si["entry_time"] or "")[:16]

                rows_html = ""
                for tr in si["tickers"]:
                    t = tr['ticker']
                    comp = company_map.get(t, t)
                    ltp_s = f"{tr['ltp']:,.2f}" if tr["ltp"] else "—"
                    pnl_s = _fmt(tr["pnl"]) if tr["pnl"] is not None else "—"
                    pnl_cl = _cls(tr["pnl"]) if tr["pnl"] is not None else "neu"
                    pct_t = (f"{_pct(tr['pnl'] / tr['cost'] * 100)}" if tr["pnl"] is not None and tr["cost"] else "—")
                    rows_html += (
                        f"<tr><td class='l'><b>{t.replace('.NS', '')}</b></td><td class='l'>{str(comp)[:28]}</td>"
                        f"<td>{tr['qty']:,}</td><td>{tr['ep']:,.2f}</td><td>{ltp_s}</td>"
                        f"<td>₹{_format_inr(tr['cost'])}</td><td>₹{_format_inr(tr['mkt'])}</td>"
                        f"<td class='{pnl_cl}'>{pnl_s}</td><td class='{pnl_cl}'>{pct_t}</td>"
                        f"<td class='l src'>{tr['src']}</td></tr>")
                tot_cls = _cls(pnl) if pnl_ok else "neu"
                foot = (f"<tfoot><tr><td class='l' colspan='5'>Basket total</td>"
                        f"<td>₹{_format_inr(invest)}</td><td>{'₹' + _format_inr(live) if live else '—'}</td>"
                        f"<td class='{tot_cls}'>{_fmt(pnl) if pnl_ok else 'N/A'}</td>"
                        f"<td class='{tot_cls}'>{_pct(pct) if pnl_ok else ''}</td><td></td></tr></tfoot>")

                with st.container(border=True):
                    st.markdown(
                        f"<div class='hb-h'><div><div class='hb-t'>Basket {bid} "
                        f"<span class='tag'>{etype}</span></div><div class='hb-s'>{et_str}</div></div>"
                        f"<div class='hb-p {tot_cls}'>{_fmt(pnl) if pnl_ok else 'LTP unavailable'}"
                        f"<div class='hb-s' style='text-align:right'>{_pct(pct) if pnl_ok else ''} on ₹{_format_inr(invest)}</div></div></div>"
                        f"<div class='tbl-wrap'><table class='tbl'><thead><tr>"
                        f"<th class='l'>Stock</th><th class='l'>Company</th><th>Qty</th><th>Entry ₹</th><th>LTP ₹</th>"
                        f"<th>Cost</th><th>Value</th><th>P&amp;L</th><th>P&amp;L %</th><th class='l'>Price source</th>"
                        f"</tr></thead><tbody>{rows_html}</tbody>{foot}</table></div>",
                        unsafe_allow_html=True)

        # ══════════════════════════════════════════════════════════════════════════
        # ④ CHARTS
        # ══════════════════════════════════════════════════════════════════════════
        _section("Charts", "basket price with regression bands, EMAs and entry/exit marks")

        if not held_basket_ids:
            st.markdown('<div class="empty">Charts appear when a basket is held.</div>', unsafe_allow_html=True)
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

                            st.markdown(f"#### Basket {bid} — daily (history + today)")
                            st.caption(binfo_str)

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

                                # Check if a death cross or exit occurred after entry
                                exit_ts = None
                                exit_px = None
                                for px in pending_ex:
                                    if px.get("basket_id") == bid or (slot and px.get("slot_idx") == slots.index(slot)):
                                        exit_ts = basket_df.index[-1]
                                        exit_px = float(basket_df["Close"].iloc[-1])
                                        break
                                if exit_ts is None and ef is not None and es is not None and entry_ts is not None:
                                    post_entry = basket_df.index[basket_df.index >= entry_ts]
                                    if not post_entry.empty:
                                        ef_post = ef.loc[post_entry]
                                        es_post = es.loc[post_entry]
                                        cross_down = (ef_post.shift(1) > es_post.shift(1)) & (ef_post <= es_post)
                                        if cross_down.any():
                                            cross_dt = cross_down[cross_down].index[0]
                                            exit_ts = cross_dt
                                            exit_px = float(basket_df.loc[cross_dt, "Close"])

                                fig = _chart(
                                    basket_df,
                                    f"Basket {bid}  ·  {', '.join(tickers[:5])}{'…' if len(tickers) > 5 else ''}",
                                    entry_time=entry_ts if entry_px else None,
                                    entry_price=entry_px,
                                    exit_time=exit_ts if exit_px else None,
                                    exit_price=exit_px,
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
        _section("Trade log", "open positions first, then closed trades")

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

            view = tl[show_cols].copy()
            if "status" in view.columns:
                view["status"] = view["status"].fillna("closed").replace({"open (MTM)": "Open", "closed": "Closed"})
            for col in ("entry_time", "exit_time"):
                if col in view.columns:
                    _d = pd.to_datetime(view[col], errors="coerce", utc=True, format="ISO8601").dt.tz_convert(IST)
                    view[col] = _d.dt.strftime("%d %b %Y %H:%M").where(_d.notna(), view[col].astype(str))
            for col in ("entry_type", "close_reason"):
                if col in view.columns:
                    view[col] = view[col].fillna("—").astype(str).str.replace("_", " ").str.title()
            if "basket_tickers" in view.columns:
                view["basket_tickers"] = view["basket_tickers"].fillna("").astype(str).str.replace(".NS", "", regex=False)
            for col in ("investment", "exit_value", "pnl", "pnl_pct", "hold_days"):
                if col in view.columns:
                    view[col] = pd.to_numeric(view[col], errors="coerce")

            styled = view.style.format({"investment": "₹{:,.0f}", "exit_value": "₹{:,.0f}", "pnl": "{:+,.0f}",
                                        "pnl_pct": "{:+.2f}%", "hold_days": "{:.0f}"}, na_rep="—")
            for col in ["pnl", "pnl_pct"]:
                if col in show_cols:
                    styled = styled.map(_pnl_style, subset=[col])
            st.dataframe(styled, width='stretch', height=320, hide_index=True, column_config={
                "status": "Status", "entry_time": "Entry", "exit_time": "Exit",
                "basket_id": st.column_config.NumberColumn("Basket", format="B%d"),
                "basket_tickers": st.column_config.TextColumn("Stocks", width="large"),
                "entry_type": "Entry signal", "close_reason": "Exit reason", "investment": "Invested",
                "exit_value": "Value / exit", "pnl": "P&L ₹", "pnl_pct": "P&L %", "hold_days": "Days",
            })

            # Cumulative PnL chart
            if "pnl" in tl.columns and len(tl) > 1:
                tl_closed = tl[tl.get("status", "closed") != "open (MTM)"] if "status" in tl.columns else tl
                if len(tl_closed) > 1:
                    cum = tl_closed["pnl"].astype(float).cumsum()
                    colors_pnl = [_C["pos"] if v >= 0 else _C["neg"] for v in tl_closed["pnl"]]
                    fig_pnl = go.Figure(go.Scatter(
                        y=cum.values, mode="lines+markers",
                        line=dict(color=_C["accent"], width=2, shape="hv"),
                        marker=dict(size=8, color=colors_pnl, line=dict(color=_C["surface"], width=1.5)),
                        fill="tozeroy", fillcolor="rgba(76,154,255,0.08)",
                        hovertemplate="Trade %{x}<br>Cumulative ₹%{y:,.0f}<extra></extra>",
                    ))
                    fig_pnl.update_layout(
                        template="adts", title="Cumulative realized P&L", height=260,
                        yaxis=dict(tickprefix="₹", side="right"),
                        xaxis=dict(title=dict(text="closed trade #", font=dict(size=10))),
                    )
                    st.plotly_chart(fig_pnl, width='stretch')
        else:
            st.markdown('<div class="empty">No trades yet.</div>', unsafe_allow_html=True)

        # ══════════════════════════════════════════════════════════════════════════
        # ⑥ DEBUG PANEL (sidebar expandable)
        # ══════════════════════════════════════════════════════════════════════════
        with st.sidebar:
            with st.expander("Debug info"):
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
        st.markdown(f'<div class="foot">State saved {str(saved_at)[:19].replace("T", " ")} · '
                    f'Paper mode only — no real orders are placed · '
                    f'{"auto-refresh every " + str(refresh_sec) + "s" if is_market_open else "auto-refresh paused (market closed)"}</div>',
                    unsafe_allow_html=True)

        # ── JS auto-refresh (avoids Python 3.14 asyncio event-loop close bug) ────
        if is_market_open:
            _js_autorefresh(refresh_sec * 1000)

    elif page == "Exited Baskets (Daily)":
        render_exited_baskets_page()
    elif page == "Backtest Engine":
        render_backtest_page()

if __name__ == "__main__":
    main()