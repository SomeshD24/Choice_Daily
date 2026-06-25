import sys
import os
from datetime import datetime, timedelta
import pytz
from dotenv import load_dotenv

sys.path.append(r"c:\Choice_Daily")
from choice_api import ChoiceClient
from symbol_mapper import load_symbol_master, ticker_to_token
from data_manager_daily import fetch_daily_historical

def main():
    load_dotenv(r"c:\Choice_Daily\.env")
    
    vendor_id = os.environ.get("CHOICE_VENDOR_ID", "")
    vendor_key = os.environ.get("CHOICE_VENDOR_KEY", "")
    api_key = os.environ.get("CHOICE_API_KEY", "")
    
    load_symbol_master()
    client = ChoiceClient(vendor_id, vendor_key, api_key)
    
    session_id = os.environ.get("CHOICE_SESSION_ID")
    if session_id:
        client.set_session_id(session_id)
        
    trading_symbol = "PFC.NS"
    token = ticker_to_token(trading_symbol)
    print(f"Token for {trading_symbol}: {token}")
    
    IST = pytz.timezone("Asia/Kolkata")
    now = datetime.now(IST)
    from_dt = now - timedelta(days=10)
    
    print("Testing Choice API direct (interval='D'):")
    try:
        df_choice = client.get_chart_data(1, token, from_dt.replace(tzinfo=None), now.replace(tzinfo=None), interval="D")
        print("Choice API returned:")
        print(df_choice)
    except Exception as e:
        print(f"Choice API error: {e}")
        
    print("\nTesting fetch_daily_historical:")
    df_hist = fetch_daily_historical(client, 1, token, trading_symbol, from_dt.replace(tzinfo=None), now.replace(tzinfo=None))
    print("fetch_daily_historical returned:")
    print(df_hist)

if __name__ == "__main__":
    main()
