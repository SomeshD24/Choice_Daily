"""
symbol_mapper.py — Maps basket yfinance tickers (.NS) to Choice FinX
SegmentId + Token (with kkunal ScripMaster support).
"""

import logging
from pathlib import Path
import pandas as pd

from config_daily import SEGMENT_NSE_CASH, SYMBOL_MASTER_PATH

logger = logging.getLogger(__name__)

_COLUMN_ALIASES = {
    "ticker": ["ticker", "yf_ticker", "Ticker", "symbol_yf"],
    "token":  ["token", "Token", "nse_token", "exchange_token"],
}

# Module-level cache: populated by load_symbol_master() on first use.
_SYMBOL_CACHE: dict[str, int] = {}   # {yf_ticker: token}


def _resolve_column(df: pd.DataFrame, names: list[str]) -> str:
    for n in names:
        if n in df.columns:
            return n
    raise ValueError(f"None of {names} found in CSV columns {list(df.columns)}")


def load_symbol_master(csv_path: str = SYMBOL_MASTER_PATH) -> dict[str, int]:
    """
    Load the local ticker→NSE-token CSV master into memory.
    Call once at startup. Idempotent (cached after first successful load).
    """
    global _SYMBOL_CACHE
    if _SYMBOL_CACHE:
        return _SYMBOL_CACHE

    path = Path(csv_path)
    if not path.exists():
        raise FileNotFoundError(
            f"Symbol master CSV not found at {csv_path}. This file must map "
            f"yfinance tickers to NSE exchange tokens."
        )

    df = pd.read_csv(path)
    ticker_col = _resolve_column(df, _COLUMN_ALIASES["ticker"])
    token_col  = _resolve_column(df, _COLUMN_ALIASES["token"])

    cache = {}
    for _, row in df.iterrows():
        t = str(row[ticker_col]).strip().upper()
        try:
            tok = int(row[token_col])
        except (TypeError, ValueError):
            continue
        if t and tok:
            cache[t] = tok

    _SYMBOL_CACHE = cache
    logger.info(f"Symbol master loaded: {len(cache)} tickers from {csv_path}")
    return cache


def _normalise_ticker(yf_ticker: str) -> str:
    return yf_ticker.strip().upper()


def ticker_to_token(yf_ticker: str) -> int | None:
    """Return the NSE exchange token for a yfinance ticker, or None."""
    if not _SYMBOL_CACHE:
        load_symbol_master()
    tok = _SYMBOL_CACHE.get(_normalise_ticker(yf_ticker))
    if tok is not None:
        return tok

    # Fallback to kkunal ScripMaster if available
    try:
        from choice_api import ScripMaster
        sm = ScripMaster()
        bare_symbol = yf_ticker.replace(".NS", "").replace(".BO", "").strip().upper()
        res = sm.get_token(bare_symbol, segment=str(SEGMENT_NSE_CASH))
        if res:
            return int(res)
    except Exception:
        pass

    return None


def build_basket_instrument_map(basket_tickers: list[str]) -> dict:
    """
    Returns {yf_ticker: {"segment_id": int, "token": int}} for each ticker.
    Logs a warning for unmappable tickers.
    """
    result = {}
    for t in basket_tickers:
        token = ticker_to_token(t)
        if token is None:
            logger.warning(f"  No NSE token found for ticker '{t}'")
            continue
        result[t] = {"segment_id": SEGMENT_NSE_CASH, "token": token}
        logger.debug(f"  {t} → segment={SEGMENT_NSE_CASH} token={token}")
    return result


def all_unique_tickers_from_configs(configs: list[dict]) -> list[str]:
    """Flatten all tickers from all basket configs (deduped)."""
    tickers = set()
    for cfg in configs:
        tickers.update(cfg["members"]["ticker"].tolist())
    return sorted(tickers)
