#!/usr/bin/env python3
"""
backtest_size6_report.py
========================
OR Gate Dual-Basket  |  Basket-size-6  |  Full backtest report generator.

Imports core strategy logic from  strategy1_dualbasket.py  (must be in
the same directory or on PYTHONPATH).

Outputs  →  ./output_size6/
    metrics_size6.csv             Extended performance metrics
    trades_size6.csv              Full trade log with cumulative PnL
    basket_breakdown_size6.csv    Per-basket performance summary
    entry_type_breakdown.csv      Regression vs EMA-crossover stats
    exit_reason_breakdown.csv     or_gate_exit / evicted / mtm stats
    equity_size6.html             Equity curve + drawdown chart
    best_trade_1..5.html          Top-5 trade detail charts
    worst_trade_1..5.html         Bottom-5 trade detail charts

USAGE
-----
    python backtest_size6_report.py [path/to/baskets.csv]
    # csv_path defaults to  data/baskets_nifty200_all_sizes.csv
"""

import sys
import math
import warnings
from pathlib import Path
from typing import Optional

warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import plotly.io as pio

# ── import core from strategy1_dualbasket ─────────────────────────────────────
sys.path.insert(0, str(Path(__file__).resolve().parent))
from strategy1_dualbasket2 import (
    load_basket_configs,
    run_backtest_for_config,
    POSITION_SIZE,
    N_SLOTS,
    EMA_FAST,
    EMA_SLOW,
    BG,
    GRID,
    TEXT,
    GREEN,
    RED,
    ACCENT,
    ORANGE,
    YELLOW,
    PURPLE,
    TEAL,
    LAYOUT,
)

pio.templates.default = "plotly_dark"

# ── constants ─────────────────────────────────────────────────────────────────
TARGET_SIZE = 6
OUT_DIR = Path("output_size6")
RF_ANNUAL = 0.0448          # India risk-free proxy (long-run RBI repo rate)
RF_DAILY = (1 + RF_ANNUAL) ** (1 / 252) - 1
TRADE_BUFFER_DAYS = 91    # calendar days of context on each side of a trade
TOP_N = 5                 # best / worst N trades to chart


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _daily_equity(
    equity_series: pd.Series,
    initial_capital: float,
    start,
    end,
) -> pd.Series:
    """Forward-fill sparse realized-PnL equity to a business-day daily series."""
    # Multiple trades can close on the same date → keep last value per date
    s = equity_series.groupby(level=0).last()
    idx = pd.bdate_range(start=start, end=end)
    return s.reindex(idx).ffill().fillna(float(initial_capital))


def _nearest_open(
    ohlc_df: pd.DataFrame,
    dt: pd.Timestamp,
    direction: str = "forward",
) -> float:
    """Return Open price at dt, or nearest trading date in the given direction."""
    if dt in ohlc_df.index:
        return float(ohlc_df.at[dt, "Open"])
    avail = (
        ohlc_df.index[ohlc_df.index >= dt]
        if direction == "forward"
        else ohlc_df.index[ohlc_df.index <= dt]
    )
    return float(ohlc_df.at[avail[0], "Open"]) if len(avail) else np.nan


def _nearest_low(ohlc_df: pd.DataFrame, dt: pd.Timestamp) -> float:
    if dt in ohlc_df.index:
        return float(ohlc_df.at[dt, "Low"])
    avail = ohlc_df.index[ohlc_df.index >= dt]
    return float(ohlc_df.at[avail[0], "Low"]) if len(avail) else np.nan


def _nearest_high(ohlc_df: pd.DataFrame, dt: pd.Timestamp) -> float:
    if dt in ohlc_df.index:
        return float(ohlc_df.at[dt, "High"])
    avail = ohlc_df.index[ohlc_df.index >= dt]
    return float(ohlc_df.at[avail[0], "High"]) if len(avail) else np.nan


def _iso(dt) -> str:
    """Safely convert a Timestamp-like to ISO date string for Plotly shapes."""
    return pd.Timestamp(dt).strftime("%Y-%m-%d")


# ══════════════════════════════════════════════════════════════════════════════
# EXTENDED METRICS
# ══════════════════════════════════════════════════════════════════════════════

def compute_extended_metrics(
    port_trades,
    equity: pd.Series,
    initial_capital: float = POSITION_SIZE,
) -> dict:
    """
    Comprehensive performance metrics.

    Includes: trade counts, win/loss stats, profit factor, expectancy,
    max consecutive W/L, return, CAGR, drawdown, Sharpe, Sortino,
    Calmar, Omega, Recovery Factor, Ulcer Index, hold-day stats.
    """
    null_keys = [
        "total_trades", "closed_trades", "open_trades",
        "win_rate_pct", "loss_rate_pct",
        "avg_win_pct", "avg_loss_pct",
        "best_trade_pct", "worst_trade_pct",
        "profit_factor", "expectancy_pct",
        "max_consec_wins", "max_consec_losses",
        "total_return_pct", "cagr_pct",
        "max_drawdown_pct", "avg_drawdown_pct",
        "max_drawdown_dur_days",
        "daily_return_std_pct", "annual_return_std_pct",
        "sharpe", "sortino", "calmar", "omega",
        "recovery_factor", "ulcer_index",
        "avg_hold_days", "median_hold_days",
        "total_pnl", "avg_trade_pnl",
        "first_trade", "last_trade", "years",
    ]
    out = {k: np.nan for k in null_keys}

    if not port_trades:
        return out

    df = pd.DataFrame(port_trades)
    df["buy_date"] = pd.to_datetime(df["buy_date"])
    df["sell_date"] = pd.to_datetime(df["sell_date"])
    closed = (
        df[df["status"] == "closed"]
        .sort_values("sell_date")
        .reset_index(drop=True)
    )

    total = len(df)
    nclosed = len(closed)
    nopen = total - nclosed

    out["total_trades"] = int(total)
    out["closed_trades"] = int(nclosed)
    out["open_trades"] = int(nopen)

    if nclosed == 0:
        return out

    wins = closed[closed["pnl"] > 0]
    losses = closed[closed["pnl"] <= 0]
    wr = len(wins) / nclosed

    out["win_rate_pct"] = round(wr * 100, 2)
    out["loss_rate_pct"] = round((1 - wr) * 100, 2)
    out["avg_win_pct"] = round(float(wins["pnl_pct"].mean()), 2) if len(wins) else np.nan
    out["avg_loss_pct"] = round(float(losses["pnl_pct"].mean()), 2) if len(losses) else np.nan
    out["best_trade_pct"] = round(float(closed["pnl_pct"].max()), 2)
    out["worst_trade_pct"] = round(float(closed["pnl_pct"].min()), 2)
    out["avg_hold_days"] = round(float(df["hold_days"].mean()), 1)
    out["median_hold_days"] = round(float(closed["hold_days"].median()), 1)
    out["total_pnl"] = round(float(closed["pnl"].sum()), 2)
    out["avg_trade_pnl"] = round(float(closed["pnl"].mean()), 2)

    win_sum = float(wins["pnl"].sum())
    loss_sum = abs(float(losses["pnl"].sum()))
    out["profit_factor"] = round(win_sum / loss_sum, 3) if loss_sum > 0 else np.inf

    aw = out["avg_win_pct"] if not (isinstance(out["avg_win_pct"], float) and np.isnan(out["avg_win_pct"])) else 0.0
    al = out["avg_loss_pct"] if not (isinstance(out["avg_loss_pct"], float) and np.isnan(out["avg_loss_pct"])) else 0.0
    out["expectancy_pct"] = round(wr * aw - (1 - wr) * abs(al), 3)

    # Max consecutive wins / losses
    streaks = (closed["pnl"].values > 0).astype(int)
    mw = ml = cw = cl = 0
    for s in streaks:
        if s:
            cw += 1
            cl = 0
        else:
            cl += 1
            cw = 0
        mw = max(mw, cw)
        ml = max(ml, cl)
    out["max_consec_wins"] = int(mw)
    out["max_consec_losses"] = int(ml)

    first_date = df["buy_date"].min()
    last_date = df["sell_date"].max()
    years = max((last_date - first_date).days / 365.25, 1e-6)
    out["first_trade"] = str(first_date.date())
    out["last_trade"] = str(last_date.date())
    out["years"] = round(years, 2)

    final_cap = (
        float(equity.iloc[-1])
        if not equity.empty
        else float(initial_capital) + float(out["total_pnl"])
    )
    out["total_return_pct"] = round((final_cap / initial_capital - 1) * 100, 2)
    out["cagr_pct"] = round(((final_cap / initial_capital) ** (1 / years) - 1) * 100, 2)

    if not equity.empty:
        daily = _daily_equity(equity, initial_capital, first_date, last_date)
        run_max = daily.cummax()
        dd_ser = (daily - run_max) / run_max * 100

        out["max_drawdown_pct"] = round(float(dd_ser.min()), 2)
        out["avg_drawdown_pct"] = (
            round(float(dd_ser[dd_ser < 0].mean()), 2)
            if (dd_ser < 0).any()
            else 0.0
        )

        # Max drawdown duration (calendar days)
        in_dd = False
        dd_start = None
        max_dur = 0
        for dt_idx, val in dd_ser.items():
            if val < 0:
                if not in_dd:
                    dd_start = dt_idx
                    in_dd = True
                max_dur = max(max_dur, (dt_idx - dd_start).days)
            else:
                in_dd = False
        out["max_drawdown_dur_days"] = int(max_dur)

        daily_rets = daily.pct_change().dropna()
        excess = daily_rets - RF_DAILY
        ex_mean = float(excess.mean())
        ex_std = float(excess.std())

        # Std dev of raw daily returns (risk). Note: subtracting the
        # constant RF_DAILY shifts the mean but NOT the std dev
        # (std(R - c) == std(R) for constant c), so this is numerically
        # identical to ex_std — computed from raw returns here so the
        # number reads as pure return volatility, independent of the
        # risk-free assumption used in Sharpe's numerator.
        ret_std = float(daily_rets.std())
        out["daily_return_std_pct"] = round(ret_std * 100, 4)
        out["annual_return_std_pct"] = round(ret_std * math.sqrt(252) * 100, 2)

        if ex_std > 0:
            out["sharpe"] = round(ex_mean / ex_std * math.sqrt(252), 3)

        down = excess[excess < 0]
        if len(down) > 1:
            dn_std = float(down.std())
            if dn_std > 0:
                out["sortino"] = round(ex_mean / dn_std * math.sqrt(252), 3)

        mdd_abs = abs(out["max_drawdown_pct"])
        if mdd_abs > 0:
            out["calmar"] = round(out["cagr_pct"] / mdd_abs, 3)
            out["recovery_factor"] = round(out["total_return_pct"] / mdd_abs, 3)

        gains = float(excess[excess > 0].sum())
        losses_ = abs(float(excess[excess <= 0].sum()))
        out["omega"] = round(gains / losses_, 3) if losses_ > 0 else np.inf

        out["ulcer_index"] = round(float(np.sqrt((dd_ser ** 2).mean())), 3)

    return out


# ══════════════════════════════════════════════════════════════════════════════
# PRETTY-PRINT METRICS
# ══════════════════════════════════════════════════════════════════════════════

def print_metrics(m: dict):
    sep = "═" * 72

    def row(label, key, fmt="{:.2f}"):
        val = m.get(key, np.nan)
        try:
            if isinstance(val, float) and np.isnan(val):
                s = "N/A"
            elif val == np.inf:
                s = "∞"
            else:
                s = fmt.format(val)
        except Exception:
            s = str(val)
        print(f"  {label:<42}  {s}")

    print(f"\n{sep}")
    print(
        f"  OR GATE DUAL-BASKET  │  BASKET-SIZE {TARGET_SIZE}  │  "
        f"EMA{EMA_FAST}/{EMA_SLOW}  │  {N_SLOTS} SLOTS"
    )
    print(sep)

    print("\n── TRADE STATISTICS ──────────────────────────────────")
    row("Total Trades",                 "total_trades",          "{:.0f}")
    row("Closed Trades",                "closed_trades",         "{:.0f}")
    row("Open / MTM Trades",            "open_trades",           "{:.0f}")
    row("Win Rate %",                   "win_rate_pct")
    row("Loss Rate %",                  "loss_rate_pct")
    row("Avg Win %",                    "avg_win_pct")
    row("Avg Loss %",                   "avg_loss_pct")
    row("Best Trade %",                 "best_trade_pct")
    row("Worst Trade %",                "worst_trade_pct")
    row("Profit Factor",                "profit_factor",         "{:.3f}")
    row("Expectancy %",                 "expectancy_pct",        "{:.3f}")
    row("Max Consecutive Wins",         "max_consec_wins",       "{:.0f}")
    row("Max Consecutive Losses",       "max_consec_losses",     "{:.0f}")
    row("Avg Hold Days",                "avg_hold_days",         "{:.1f}")
    row("Median Hold Days",             "median_hold_days",      "{:.1f}")
    row("Total PnL (₹)",                "total_pnl",             "{:,.0f}")
    row("Avg Trade PnL (₹)",            "avg_trade_pnl",         "{:,.0f}")

    print("\n── RETURN METRICS ────────────────────────────────────")
    row("First Trade",                  "first_trade",           "{}")
    row("Last Trade",                   "last_trade",            "{}")
    row("Years",                        "years",                 "{:.2f}")
    row("Total Return %",               "total_return_pct")
    row("CAGR %",                       "cagr_pct")

    print("\n── RISK METRICS ──────────────────────────────────────")
    row("Max Drawdown %",               "max_drawdown_pct")
    row("Avg Drawdown %",               "avg_drawdown_pct")
    row("Max Drawdown Duration (days)", "max_drawdown_dur_days", "{:.0f}")
    row("Ulcer Index",                  "ulcer_index",           "{:.3f}")
    row("Daily Return Std Dev %",       "daily_return_std_pct",  "{:.4f}")
    row("Annualized Std Dev % (Risk)",  "annual_return_std_pct", "{:.2f}")

    print("\n── RISK-ADJUSTED RATIOS ──────────────────────────────")
    row(f"Sharpe  (RF={RF_ANNUAL*100:.0f}%/yr, ann., uses Std Dev above)", "sharpe",   "{:.3f}")
    row(f"Sortino (RF={RF_ANNUAL*100:.0f}%/yr, ann.)", "sortino",  "{:.3f}")
    row("Calmar  (CAGR / |MaxDD|)",     "calmar",                 "{:.3f}")
    row("Omega Ratio",                  "omega",                  "{:.3f}")
    row("Recovery Factor",              "recovery_factor",        "{:.3f}")

    print(f"{sep}\n")


# ══════════════════════════════════════════════════════════════════════════════
# EQUITY CURVE + DRAWDOWN CHART
# ══════════════════════════════════════════════════════════════════════════════

def plot_equity_chart(
    equity: pd.Series,
    metrics: dict,
    initial_capital: float,
    out_path: Optional[Path] = None,
):
    if equity.empty:
        return

    first = pd.Timestamp(metrics.get("first_trade", equity.index[0]))
    last = pd.Timestamp(metrics.get("last_trade", equity.index[-1]))
    daily = _daily_equity(equity, initial_capital, first, last)
    run_max = daily.cummax()
    dd = (daily - run_max) / run_max * 100

    fig = make_subplots(
        rows=2, cols=1,
        shared_xaxes=True,
        row_heights=[0.65, 0.35],
        vertical_spacing=0.05,
        subplot_titles=(
            "Portfolio Equity — Absolute Capital (₹)",
            "Drawdown %",
        ),
    )

    # Equity curve
    fig.add_trace(
        go.Scatter(
            x=daily.index,
            y=daily.values,
            mode="lines",
            line=dict(color=ACCENT, width=1.8),
            name="Equity",
            fill="tozeroy",
            fillcolor="rgba(0,212,255,0.06)",
        ),
        row=1, col=1,
    )
    fig.add_hline(
        y=initial_capital,
        line_dash="dash",
        line_color=TEXT,
        opacity=0.35,
        row=1, col=1,
    )

    # Drawdown
    fig.add_trace(
        go.Scatter(
            x=dd.index,
            y=dd.values,
            mode="lines",
            line=dict(color=RED, width=1.2),
            fill="tozeroy",
            fillcolor="rgba(255,68,102,0.12)",
            name="Drawdown %",
        ),
        row=2, col=1,
    )
    fig.add_hline(y=0, line_dash="dash", line_color=TEXT, opacity=0.25, row=2, col=1)

    def _fmt(key, fmt="{:.2f}"):
        v = metrics.get(key, np.nan)
        if isinstance(v, float) and np.isnan(v):
            return "N/A"
        return fmt.format(v)

    title = (
        f"OR Gate Dual-Basket  │  Basket-size {TARGET_SIZE}  │  "
        f"EMA{EMA_FAST}/{EMA_SLOW}  │  {N_SLOTS} slots<br>"
        f"CAGR={_fmt('cagr_pct')}%  │  MaxDD={_fmt('max_drawdown_pct')}%  │  "
        f"Sharpe={_fmt('sharpe','{:.3f}')}  │  Sortino={_fmt('sortino','{:.3f}')}  │  "
        f"Calmar={_fmt('calmar','{:.3f}')}  │  WR={_fmt('win_rate_pct')}%  │  "
        f"PF={_fmt('profit_factor','{:.3f}')}  │  Trades={_fmt('closed_trades','{:.0f}')}"
    )

    lyt = dict(**LAYOUT)
    lyt.update(
        dict(
            height=680,
            hovermode="x unified",
            title=dict(text=title, font=dict(size=11)),
        )
    )
    fig.update_layout(**lyt)
    fig.update_xaxes(rangeslider_visible=False, row=1, col=1)
    fig.update_yaxes(title_text="Capital (₹)", row=1, col=1)
    fig.update_yaxes(title_text="Drawdown %", row=2, col=1)
    if out_path:
        fig.write_html(str(out_path))
        print(f"  Equity chart      → {out_path}")
    return fig


# ══════════════════════════════════════════════════════════════════════════════
# SINGLE TRADE DETAIL CHART
# ══════════════════════════════════════════════════════════════════════════════

def plot_trade_chart(
    trade: dict,
    ohlc_df: pd.DataFrame,
    bands: pd.DataFrame,
    signals: pd.DataFrame,
    rank: int,
    kind: str,          # 'best' | 'worst'
    out_path: Optional[Path] = None,
):
    """
    6+ months context chart for one portfolio trade.

    Layout
    ------
    Row 1 (80%) : Candlestick + EMA20/100 + regression bands
                  Entry/exit markers, dotted vlines, PnL shading
    Row 2 (20%) : Volume bars

    Visual elements
    ---------------
    • Green shading  (win) / red shading (loss) between entry↔exit
    • Green dotted vline at entry date
    • Red dotted vline at exit date
    • Triangle-up  ▲ at entry (below candle low)
    • Triangle-down▼ at exit  (above candle high)
    • PnL annotation in the middle of the shaded region
    """
    buy_dt = pd.Timestamp(trade["buy_date"])
    sell_dt = pd.Timestamp(trade["sell_date"])
    pnl = float(trade.get("pnl", 0.0))
    pnl_pct = float(trade.get("pnl_pct", 0.0))
    is_win = pnl > 0

    # ── Window  (entry + exit centred with >= 6 months total) ─────────────────
    buf = pd.Timedelta(days=TRADE_BUFFER_DAYS)
    w0 = buy_dt - buf
    w1 = sell_dt + buf
    # Guarantee at least 180 calendar days in the window
    if (w1 - w0).days < 180:
        mid = buy_dt + (sell_dt - buy_dt) / 2
        w0 = mid - pd.Timedelta(days=90)
        w1 = mid + pd.Timedelta(days=90)

    df = ohlc_df.loc[w0:w1].copy()
    if df.empty:
        print(f"  [SKIP] No OHLC data in window for {kind} #{rank}")
        return

    b = bands.reindex(df.index)
    s = signals.reindex(df.index)

    # ── Execution prices ──────────────────────────────────────────────────────
    entry_price = _nearest_open(ohlc_df, buy_dt,  "forward")
    exit_price  = _nearest_open(ohlc_df, sell_dt, "forward")
    entry_low   = _nearest_low( ohlc_df, buy_dt)
    exit_high   = _nearest_high(ohlc_df, sell_dt)

    # ── Labels ────────────────────────────────────────────────────────────────
    entry_type   = str(trade.get("entry_type",   "unknown"))
    close_reason = str(trade.get("close_reason", "unknown"))
    hold         = int(trade.get("hold_days", 0))
    symbols      = str(trade.get("basket_symbols", ""))
    basket_id    = trade.get("basket_id", "?")
    inv          = float(trade.get("investment", 0))
    exit_val     = float(trade.get("exit_value", 0))

    sign = "+" if is_win else ""
    pnl_str = f"{sign}₹{pnl:,.0f}  ({sign}{pnl_pct:.2f}%)"
    badge = "🏆 BEST" if kind == "best" else "💀 WORST"

    title_str = (
        f"{badge} #{rank}   ·   Basket {basket_id}: {symbols}   ·   "
        f"PnL: {pnl_str}   ·   Hold: {hold} days   ·   "
        f"Entry: {entry_type}   ·   Exit: {close_reason}"
    )

    # ── Figure skeleton ───────────────────────────────────────────────────────
    fig = make_subplots(
        rows=2, cols=1,
        shared_xaxes=True,
        row_heights=[0.80, 0.20],
        vertical_spacing=0.04,
        subplot_titles=(title_str, "Volume"),
    )

    # ── Row 1: Candlestick ────────────────────────────────────────────────────
    fig.add_trace(
        go.Candlestick(
            x=df.index,
            open=df["Open"], high=df["High"],
            low=df["Low"],   close=df["Close"],
            increasing_line_color=GREEN,
            decreasing_line_color=RED,
            name="OHLC",
        ),
        row=1, col=1,
    )

    # EMA lines
    fig.add_trace(
        go.Scatter(
            x=s.index, y=s["EMA_fast"],
            mode="lines",
            line=dict(color=ORANGE, width=1.2),
            name=f"EMA{EMA_FAST}",
        ),
        row=1, col=1,
    )
    fig.add_trace(
        go.Scatter(
            x=s.index, y=s["EMA_slow"],
            mode="lines",
            line=dict(color=YELLOW, width=1.6),
            name=f"EMA{EMA_SLOW}",
        ),
        row=1, col=1,
    )

    # Regression bands
    band_spec = [
        ("Reg Trend", "trend_line", PURPLE, "dash"),
        ("−2σ Band",  "lower2",     RED,    "dot"),
        ("+2σ Band",  "upper2",     GREEN,  "dot"),
    ]
    for bname, bkey, bcol, bdash in band_spec:
        if bkey in b.columns and b[bkey].notna().any():
            fig.add_trace(
                go.Scatter(
                    x=b.index, y=b[bkey],
                    mode="lines",
                    line=dict(color=bcol, width=0.9, dash=bdash),
                    name=bname,
                    opacity=0.80,
                ),
                row=1, col=1,
            )

    # ── Row 2: Volume ─────────────────────────────────────────────────────────
    vcols = [
        GREEN if c >= o else RED
        for o, c in zip(df["Open"], df["Close"])
    ]
    fig.add_trace(
        go.Bar(
            x=df.index, y=df["Volume"],
            marker_color=vcols, marker_opacity=0.55,
            name="Volume", showlegend=False,
        ),
        row=2, col=1,
    )

    # ── PnL shading between entry and exit (below candles) ────────────────────
    shade_fill = "rgba(0,255,136,0.12)" if is_win else "rgba(255,68,102,0.12)"
    fig.add_vrect(
        x0=_iso(buy_dt),
        x1=_iso(sell_dt),
        fillcolor=shade_fill,
        opacity=1.0,
        layer="below",
        line_width=0,
    )

    # ── Dotted vertical lines at entry (green) and exit (red) ─────────────────
    for dt_mark, col_mark in [(buy_dt, GREEN), (sell_dt, RED)]:
        fig.add_shape(
            type="line",
            x0=_iso(dt_mark), x1=_iso(dt_mark),
            y0=0.0, y1=1.0,
            xref="x", yref="paper",
            line=dict(color=col_mark, width=1.8, dash="dot"),
        )

    # ── Entry marker: triangle-up below candle low ────────────────────────────
    entry_marker_y = entry_low * 0.988 if not np.isnan(entry_low) else (
        entry_price * 0.988 if not np.isnan(entry_price) else None
    )
    if entry_marker_y is not None:
        ecolor = ACCENT if entry_type == "regression" else PURPLE
        entry_label = "REG entry" if entry_type == "regression" else "EMA cross entry"
        fig.add_trace(
            go.Scatter(
                x=[buy_dt],
                y=[entry_marker_y],
                mode="markers+text",
                marker=dict(
                    symbol="triangle-up",
                    color=ecolor,
                    size=14,
                    line=dict(color="white", width=1),
                ),
                text=[f"<b>▲ BUY</b><br>{entry_label}"],
                textposition="bottom center",
                textfont=dict(size=9, color=ecolor),
                name=f"Entry ({entry_type})",
            ),
            row=1, col=1,
        )

    # ── Exit marker: triangle-down above candle high ──────────────────────────
    exit_marker_y = exit_high * 1.012 if not np.isnan(exit_high) else (
        exit_price * 1.012 if not np.isnan(exit_price) else None
    )
    if exit_marker_y is not None:
        if "ema" in close_reason.lower():
            xcolor = RED
            xlabel = "EMA death cross"
        elif "evict" in close_reason.lower():
            xcolor = ORANGE
            xlabel = "Evicted"
        elif "mtm" in close_reason.lower():
            xcolor = YELLOW
            xlabel = "MTM (open)"
        else:
            xcolor = TEAL
            xlabel = "Reg +2σ exit"

        fig.add_trace(
            go.Scatter(
                x=[sell_dt],
                y=[exit_marker_y],
                mode="markers+text",
                marker=dict(
                    symbol="triangle-down",
                    color=xcolor,
                    size=14,
                    line=dict(color="white", width=1),
                ),
                text=[f"<b>▼ SELL</b><br>{xlabel}"],
                textposition="top center",
                textfont=dict(size=9, color=xcolor),
                name=f"Exit ({close_reason})",
            ),
            row=1, col=1,
        )

    # ── PnL annotation in centre of shaded region ─────────────────────────────
    mid_dt = buy_dt + (sell_dt - buy_dt) / 2
    inv_str = f"Inv: ₹{inv:,.0f} → ₹{exit_val:,.0f}" if inv > 0 else ""
    fig.add_annotation(
        x=_iso(mid_dt),
        y=0.93,
        xref="x",
        yref="paper",
        text=f"<b>{pnl_str}</b><br><span style='font-size:10px'>{inv_str}</span>",
        showarrow=False,
        font=dict(color=GREEN if is_win else RED, size=13, family="monospace"),
        bgcolor="rgba(13,17,23,0.82)",
        bordercolor=GREEN if is_win else RED,
        borderwidth=1.5,
        borderpad=5,
        align="center",
    )

    # ── Entry / exit date labels at bottom ────────────────────────────────────
    for dt_lbl, col_lbl, pos in [
        (buy_dt,  GREEN, 0.02),
        (sell_dt, RED,   0.02),
    ]:
        fig.add_annotation(
            x=_iso(dt_lbl),
            y=pos,
            xref="x",
            yref="paper",
            text=f"<b>{dt_lbl.strftime('%d %b %Y')}</b>",
            showarrow=False,
            font=dict(color=col_lbl, size=9, family="monospace"),
            bgcolor="rgba(13,17,23,0.75)",
            bordercolor=col_lbl,
            borderwidth=1,
            borderpad=3,
        )

    # ── Layout ────────────────────────────────────────────────────────────────
    lyt = dict(**LAYOUT)
    lyt.update(
        dict(
            height=740,
            hovermode="x unified",
            title=dict(
                text=title_str,
                font=dict(size=11, family="monospace"),
            ),
        )
    )
    fig.update_layout(**lyt)
    fig.update_xaxes(rangeslider_visible=False, row=1, col=1)
    fig.update_yaxes(title_text="Basket Price (₹)", row=1, col=1)
    fig.update_yaxes(title_text="Volume", row=2, col=1)
    if out_path:
        fig.write_html(str(out_path))
        print(f"  {kind.upper()} #{rank:02d} → {out_path.name}  "
              f"(B{basket_id}: {symbols}, {pnl_str})")
    return fig


# ══════════════════════════════════════════════════════════════════════════════
# BREAKDOWN TABLES
# ══════════════════════════════════════════════════════════════════════════════

def basket_breakdown(port_trades) -> pd.DataFrame:
    if not port_trades:
        return pd.DataFrame()
    df = pd.DataFrame(port_trades)
    closed = df[df["status"] == "closed"]
    if closed.empty:
        return pd.DataFrame()
    rows = []
    for bid, grp in closed.groupby("basket_id"):
        wins = grp[grp["pnl"] > 0]
        rows.append({
            "basket_id":     bid,
            "symbols":       grp["basket_symbols"].iloc[0],
            "trades":        len(grp),
            "wins":          len(wins),
            "win_rate_pct":  round(len(wins) / len(grp) * 100, 1),
            "total_pnl":     round(float(grp["pnl"].sum()), 2),
            "avg_pnl_pct":   round(float(grp["pnl_pct"].mean()), 2),
            "best_pnl_pct":  round(float(grp["pnl_pct"].max()), 2),
            "worst_pnl_pct": round(float(grp["pnl_pct"].min()), 2),
            "avg_hold_days": round(float(grp["hold_days"].mean()), 1),
        })
    return (
        pd.DataFrame(rows)
        .sort_values("total_pnl", ascending=False)
        .reset_index(drop=True)
    )


def entry_type_breakdown(port_trades) -> pd.DataFrame:
    if not port_trades:
        return pd.DataFrame()
    df = pd.DataFrame(port_trades)
    closed = df[df["status"] == "closed"]
    if closed.empty:
        return pd.DataFrame()
    rows = []
    for etype, grp in closed.groupby("entry_type"):
        wins = grp[grp["pnl"] > 0]
        rows.append({
            "entry_type":    etype,
            "trades":        len(grp),
            "wins":          len(wins),
            "win_rate_pct":  round(len(wins) / len(grp) * 100, 1),
            "total_pnl":     round(float(grp["pnl"].sum()), 2),
            "avg_pnl_pct":   round(float(grp["pnl_pct"].mean()), 2),
            "avg_hold_days": round(float(grp["hold_days"].mean()), 1),
        })
    return pd.DataFrame(rows)


def exit_reason_breakdown(port_trades) -> pd.DataFrame:
    if not port_trades:
        return pd.DataFrame()
    df = pd.DataFrame(port_trades)
    closed = df[df["status"] == "closed"]
    if closed.empty:
        return pd.DataFrame()
    rows = []
    for reason, grp in closed.groupby("close_reason"):
        wins = grp[grp["pnl"] > 0]
        rows.append({
            "close_reason":  reason,
            "trades":        len(grp),
            "wins":          len(wins),
            "win_rate_pct":  round(len(wins) / len(grp) * 100, 1),
            "total_pnl":     round(float(grp["pnl"].sum()), 2),
            "avg_pnl_pct":   round(float(grp["pnl_pct"].mean()), 2),
            "avg_hold_days": round(float(grp["hold_days"].mean()), 1),
        })
    return pd.DataFrame(rows)


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main(
    csv_path: Optional[str] = None,
    initial_capital: float = POSITION_SIZE,
    n_slots: int = N_SLOTS,
):
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print(f"\n{'═'*72}")
    print(f"  OR Gate Dual-Basket  │  Basket-size {TARGET_SIZE} Full Report")
    print(f"  Output directory : {OUT_DIR.resolve()}")
    print(f"{'═'*72}")

    # ── Load configs, filter to TARGET_SIZE ───────────────────────────────────
    configs = load_basket_configs(
        csv_paths=[csv_path] if csv_path else None
    )
    size_configs = [c for c in configs if c["basket_size"] == TARGET_SIZE]
    if not size_configs:
        available = sorted({c["basket_size"] for c in configs})
        raise ValueError(
            f"No basket_size={TARGET_SIZE} config found in the CSV.\n"
            f"Available basket sizes: {available}"
        )
    config = size_configs[0]
    n_baskets = config["members"]["basket_id"].nunique()
    print(f"\nConfig   : {config['label']}  ({n_baskets} baskets)")
    print(f"Capital  : ₹{initial_capital:,.0f}   Slots: {n_slots}")

    # ── Run backtest ──────────────────────────────────────────────────────────
    results, bdata, port_trades, equity_abs, _ = run_backtest_for_config(
        config, initial_capital, n_slots
    )

    # ── Extended metrics ──────────────────────────────────────────────────────
    metrics = compute_extended_metrics(port_trades, equity_abs, initial_capital)
    metrics.update({
        "basket_size":     TARGET_SIZE,
        "label":           config["label"],
        "n_baskets":       n_baskets,
        "n_slots":         n_slots,
        "ema_fast":        EMA_FAST,
        "ema_slow":        EMA_SLOW,
        "initial_capital": initial_capital,
        "rf_annual_pct":   RF_ANNUAL * 100,
    })
    print_metrics(metrics)

    # ── Save metrics CSV ──────────────────────────────────────────────────────
    mpath = OUT_DIR / "metrics_size6.csv"
    (
        pd.DataFrame.from_dict(metrics, orient="index", columns=["value"])
        .reset_index()
        .rename(columns={"index": "metric"})
        .to_csv(mpath, index=False)
    )
    print(f"  Metrics           → {mpath}")

    # ── Build & enrich trade log ──────────────────────────────────────────────
    tdf = pd.DataFrame()
    if port_trades:
        tdf = pd.DataFrame(port_trades)
        tdf = tdf.drop(
            columns=["quantities", "component_open", "component_close"],
            errors="ignore",
        )
        tdf["buy_date"]  = pd.to_datetime(tdf["buy_date"])
        tdf["sell_date"] = pd.to_datetime(tdf["sell_date"])
        tdf = tdf.sort_values(["sell_date", "basket_id"]).reset_index(drop=True)
        tdf.insert(0, "trade_no", range(1, len(tdf) + 1))

        # Cumulative PnL (closed trades only, chronological)
        closed_mask = tdf["status"] == "closed"
        tdf.loc[closed_mask, "cum_pnl"] = tdf.loc[closed_mask, "pnl"].cumsum()
        tdf["cum_pnl"] = tdf["cum_pnl"].ffill()

        # Running win-rate after each closed trade
        closed_idx = tdf.index[closed_mask]
        run_wr = []
        count = wins = 0
        for i in tdf.index:
            if tdf.at[i, "status"] == "closed":
                count += 1
                if tdf.at[i, "pnl"] > 0:
                    wins += 1
                run_wr.append(round(wins / count * 100, 1) if count else np.nan)
            else:
                run_wr.append(np.nan)
        tdf["running_win_rate_pct"] = run_wr

        trade_path = OUT_DIR / "trades_size6.csv"
        tdf.to_csv(trade_path, index=False)
        print(f"  Trade log         → {trade_path}")

        # Per-basket breakdown
        bbd = basket_breakdown(port_trades)
        if not bbd.empty:
            bp = OUT_DIR / "basket_breakdown_size6.csv"
            bbd.to_csv(bp, index=False)
            print(f"  Basket breakdown  → {bp}")
            print(f"\nBasket Breakdown (top 10 by PnL):\n{bbd.head(10).to_string(index=False)}")

        # Entry-type breakdown
        ebd = entry_type_breakdown(port_trades)
        if not ebd.empty:
            ep = OUT_DIR / "entry_type_breakdown.csv"
            ebd.to_csv(ep, index=False)
            print(f"\nEntry-type Breakdown:\n{ebd.to_string(index=False)}")
            ebd.to_csv(ep, index=False)

        # Exit-reason breakdown
        xbd = exit_reason_breakdown(port_trades)
        if not xbd.empty:
            xp = OUT_DIR / "exit_reason_breakdown.csv"
            xbd.to_csv(xp, index=False)
            print(f"\nExit-reason Breakdown:\n{xbd.to_string(index=False)}")
    else:
        print("  No trades generated — check basket CSV and date range.")

    # ── Equity chart ──────────────────────────────────────────────────────────
    if not equity_abs.empty:
        plot_equity_chart(
            equity_abs, metrics, initial_capital,
            OUT_DIR / "equity_size6.html",
        )

    # ── Best / worst 5 trade charts ────────────────────────────────────────────
    if not tdf.empty:
        closed_df = tdf[tdf["status"] == "closed"].copy()
        if not closed_df.empty:
            print(f"\n── Generating trade detail charts ({TOP_N} best + {TOP_N} worst) ──")
            selections = [
                ("best",  closed_df.nlargest(TOP_N,  "pnl_pct")),
                ("worst", closed_df.nsmallest(TOP_N, "pnl_pct")),
            ]
            for kind, sel in selections:
                print(f"\n{kind.upper()} {TOP_N}:")
                for rank, (_, row) in enumerate(sel.iterrows(), 1):
                    bid = row["basket_id"]
                    if bid not in bdata or bid not in results:
                        print(f"  [SKIP] basket_id={bid} not in bdata/results")
                        continue
                    plot_trade_chart(
                        trade    = row.to_dict(),
                        ohlc_df  = bdata[bid],
                        bands    = results[bid]["bands"],
                        signals  = results[bid]["signals"],
                        rank     = rank,
                        kind     = kind,
                        out_path = OUT_DIR / f"{kind}_trade_{rank:02d}.html",
                    )

    print(f"\n{'═'*72}")
    print(f"  Done.  All outputs → {OUT_DIR.resolve()}")
    print(f"{'═'*72}\n")


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    csv_arg = sys.argv[1] if len(sys.argv) > 1 else None
    main(csv_path=csv_arg)