import sys
import yfinance as yf
import pandas as pd
import math
from datetime import datetime, timedelta

# Fix encoding issues for windows console
sys.stdout.reconfigure(encoding='utf-8')

baskets = [
    {
        'id': 17,
        'entry_date': '2026-06-19',
        'exit_date': '2026-07-02',
        'stocks': ['UNIONBANK.NS', 'LAURUSLABS.NS', 'PIDILITIND.NS', 'BDL.NS', 'LODHA.NS', 'BAJAJ-AUTO.NS'],
        'entry_value': 4996433,
        'exit_value': 5176453.72
    },
    {
        'id': 6,
        'entry_date': '2026-07-02',
        'exit_date': '2026-07-08',
        'stocks': ['ABCAPITAL.NS', 'CIPLA.NS', 'NYKAA.NS', 'OIL.NS', 'MARICO.NS', 'BLUESTARCO.NS'],
        'entry_value': 5184180.65,
        'exit_value': 5242844.95
    },
    {
        'id': 19,
        'entry_date': '2026-07-08',
        'exit_date': '2026-07-10',
        'stocks': ['BANKINDIA.NS', 'INDHOTEL.NS', 'TVSMOTOR.NS', 'COALINDIA.NS', 'SHREECEM.NS', 'SRF.NS'],
        'entry_value': 5218325.7,
        'exit_value': 5193348.7
    },
    {
        'id': 7,
        'entry_date': '2026-04-23',
        'exit_date': '2026-07-16',
        'stocks': ['PFC.NS', 'BRITANNIA.NS', 'KPITTECH.NS', 'ADANIGREEN.NS', 'LUPIN.NS', 'SUPREMEIND.NS'],
        'entry_value': 4992524.3,
        'exit_value': 4906339.4,
        'known_quantities': {'PFC.NS': 1782, 'BRITANNIA.NS': 145, 'KPITTECH.NS': 1133, 'ADANIGREEN.NS': 698, 'LUPIN.NS': 361, 'SUPREMEIND.NS': 224}
    }
]

def get_open_price(ticker, date_str):
    try:
        end_date = (datetime.strptime(date_str, '%Y-%m-%d') + timedelta(days=3)).strftime('%Y-%m-%d')
        data = yf.download(ticker, start=date_str, end=end_date, progress=False)
        if not data.empty:
            for i, row in data.iterrows():
                if i.strftime('%Y-%m-%d') >= date_str:
                    return float(row['Open'].iloc[0]) if isinstance(row['Open'], pd.Series) else float(row['Open'])
    except Exception as e:
        print(e)
    return None

def get_current_price(ticker):
    try:
        data = yf.Ticker(ticker)
        info = data.history(period='1d')
        if not info.empty:
            return float(info['Close'].iloc[-1])
    except:
        pass
    return None

def calculate_basket(basket):
    print(f"=====================================")
    print(f"--- Basket {basket['id']} ---")
    allocation = basket['entry_value'] / len(basket['stocks'])
    
    total_calculated_entry = 0
    quantities = {}
    entry_prices = {}
    
    has_known = 'known_quantities' in basket
    
    for stock in basket['stocks']:
        price = get_open_price(stock, basket['entry_date'])
        if price is None or math.isnan(price):
            print(f"Could not get open price for {stock} on {basket['entry_date']}")
            continue
            
        entry_prices[stock] = price
        if has_known:
            qty = basket['known_quantities'][stock]
        else:
            qty = math.floor(allocation / price)
            
        quantities[stock] = qty
        total_calculated_entry += qty * price
        
    if not has_known:
        total_round_entry = sum([round(allocation / entry_prices[s]) * entry_prices[s] for s in basket['stocks'] if s in entry_prices])
        if abs(total_round_entry - basket['entry_value']) < abs(total_calculated_entry - basket['entry_value']):
            for s in basket['stocks']:
                if s in entry_prices:
                    quantities[s] = round(allocation / entry_prices[s])
    
    print(f"Original Entry Value: Rs. {basket['entry_value']:,.2f}")
    
    calculated_entry = sum(quantities[s]*entry_prices[s] for s in quantities)
    print(f"Calculated Entry Value (Open Prices): Rs. {calculated_entry:,.2f}")
    
    total_exit_value = 0
    current_value = 0
    
    for stock in basket['stocks']:
        if stock not in quantities: continue
        
        exit_price = get_open_price(stock, basket['exit_date'])
        cur_price = get_current_price(stock)
        
        qty = quantities[stock]
        total_exit_value += qty * exit_price if exit_price and not math.isnan(exit_price) else 0
        current_value += qty * cur_price if cur_price and not math.isnan(cur_price) else 0
        
    print(f"Original Exit Value: Rs. {basket['exit_value']:,.2f}")
    print(f"Calculated Exit Value (Open Prices): Rs. {total_exit_value:,.2f}")
    print(f"Current Value (as of now): Rs. {current_value:,.2f}")
    print(f"=====================================\n")
    return basket['id'], calculated_entry, total_exit_value, current_value

if __name__ == '__main__':
    results = []
    for b in baskets:
        results.append(calculate_basket(b))
        
    with open('exited_baskets_summary.csv', 'w', encoding='utf-8') as f:
        f.write("Basket ID,Calculated Entry Value (Open),Calculated Exit Value (Open),Current Value (If Held)\n")
        for r in results:
            f.write(f"{r[0]},{r[1]:.2f},{r[2]:.2f},{r[3]:.2f}\n")
    print("Saved to exited_baskets_summary.csv")
