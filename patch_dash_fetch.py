import re

with open(r"c:\Choice_Daily\dashboard.py", "r", encoding="utf-8") as f:
    content = f.read()

bad_pattern = re.compile(
    r"@st\.cache_data\(ttl=300, show_spinner=False\)\ndef _fetch_daily.*?return pd\.DataFrame\(\), str\(e\)",
    re.DOTALL
)

replacement = """@st.cache_data(ttl=300, show_spinner=False)
def _fetch_daily(trading_symbol: str, days_back: int = 1300) -> tuple[pd.DataFrame, str | None]:
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
def _fetch_1min(trading_symbol: str, days_back: int = 1) -> tuple[pd.DataFrame, str | None]:
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
            
        df = client.get_chart_data(1, token, from_dt.replace(tzinfo=None), now.replace(tzinfo=None), interval="1")
        if df.empty:
            return pd.DataFrame(), f"{trading_symbol}: 0 1-min rows"
            
        if df.index.tz is None:
            df.index = df.index.tz_localize(IST)
        else:
            df.index = df.index.tz_convert(IST)
        return df, None
    except Exception as e:
        import pandas as pd
        return pd.DataFrame(), str(e)"""

new_content, count = bad_pattern.subn(replacement, content)
print(f"Replaced {count} times.")

with open(r"c:\Choice_Daily\dashboard.py", "w", encoding="utf-8") as f:
    f.write(new_content)
