import re

with open(r"c:\Choice_Daily\dashboard.py", "r", encoding="utf-8") as f:
    content = f.read()

bad_pattern = re.compile(
    r"if cached and saved_at and _session_is_same_day\(saved_@st\.cache_data.*?ly import _records_to_df\n    return _records_to_df\(rows\)",
    re.DOTALL
)

replacement = """if cached and saved_at and _session_is_same_day(saved_at):
            client.set_session_id(cached)
            return client, client, None
            
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
        load_symbol_master(conn)
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
    return ticker"""

new_content, count = bad_pattern.subn(replacement, content)
print(f"Replaced {count} times.")

with open(r"c:\Choice_Daily\dashboard.py", "w", encoding="utf-8") as f:
    f.write(new_content)
