import re

with open(r'c:\Choice_Daily\dashboard.py', 'r', encoding='utf-8') as f:
    content = f.read()

content = content.replace('use_container_width=True', 'width=\'stretch\'')
content = content.replace('use_container_width=False', 'width=\'content\'')
content = content.replace('components.html(', 'st.html(')

old_block_1 = """        total_invest   = 0.0
        total_live_val = 0.0
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

            for t in tickers:
                ep   = epx.get(t, 0.0)
                qty  = qtys.get(t, 0)
                ltp  = live_prices.get(t)
                src  = ltp_sources.get(t, "—")
                cost = ep * qty
                if ltp is None:
                    all_ltp_ok = False
                    ticker_rows.append(dict(ticker=t, ep=ep, qty=qty, ltp=None, cost=cost, mkt=None, pnl=None, src=src))
                else:
                    mkt = ltp * qty
                    slot_live += mkt
                    ticker_rows.append(dict(ticker=t, ep=ep, qty=qty, ltp=ltp, cost=cost, mkt=mkt, pnl=mkt - cost, src=src))"""

new_block_1 = """        total_invest   = 0.0
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
                    all_ltp_ok = False
                    ticker_rows.append(dict(ticker=t, ep=ep, qty=qty, ltp=None, cost=cost, mkt=None, pnl=None, src=src))
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
                    day_unrealized += (ltp - ref_price) * qty"""

old_block_2 = """        unrealized   = (total_live_val - total_invest) if total_live_val else 0.0
        day_pnl      = realized + unrealized
        total_equity = POSITION_SIZE + realized + unrealized"""

new_block_2 = """        unrealized   = (total_live_val - total_invest) if total_live_val else 0.0
        
        day_realized = 0.0
        for tr in trade_log_st:
            close_str = tr.get("close_time") or tr.get("exit_time")
            if close_str:
                if pd.to_datetime(close_str).date() == now_ist.date():
                    day_realized += float(tr.get("pnl", 0.0))
                    
        day_pnl      = day_realized + day_unrealized
        total_equity = POSITION_SIZE + realized + unrealized"""

if old_block_1 in content:
    content = content.replace(old_block_1, new_block_1)
    print('Replaced block 1 successfully')
else:
    print('Failed to find block 1')

if old_block_2 in content:
    content = content.replace(old_block_2, new_block_2)
    print('Replaced block 2 successfully')
else:
    print('Failed to find block 2')

with open(r'c:\Choice_Daily\dashboard.py', 'w', encoding='utf-8') as f:
    f.write(content)
