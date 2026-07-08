import os
import sys
import pytz
from datetime import datetime, timedelta
from unittest.mock import patch

def main():
    print("Fixing run_daily.py KeyError on cloud deployment...")
    
    # 1. Patch run_daily.py source code
    # Try to find run_daily.py in the same directory as this script, or current dir
    run_daily_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run_daily.py")
    if not os.path.exists(run_daily_path):
        run_daily_path = "run_daily.py"
        
    if os.path.exists(run_daily_path):
        with open(run_daily_path, "r", encoding="utf-8") as f:
            code = f.read()
        
        old_str = "Active: {snap['active_slots']}"
        new_str = "Active: {sum(1 for s in snap['slots'] if s is not None)}"
        
        if old_str in code:
            code = code.replace(old_str, new_str)
            with open(run_daily_path, "w", encoding="utf-8") as f:
                f.write(code)
            print("Successfully patched run_daily.py")
        else:
            print("run_daily.py already patched or string not found.")
    else:
        print(f"Could not find {run_daily_path}")
        return

    # 2. Re-run EOD for yesterday
    try:
        import run_daily
        import data_manager_daily
    except ImportError:
        print("Could not import run_daily or data_manager_daily. Make sure you run this script from the Choice_Daily directory.")
        return
    
    IST = pytz.timezone("Asia/Kolkata")
    
    # Use July 7th 2026 as the mock time (or yesterday if running normally later)
    # We will compute yesterday dynamically to make the script robust.
    now_ist = datetime.now(IST)
    yesterday_eod = (now_ist - timedelta(days=1)).replace(hour=15, minute=45, second=0, microsecond=0)
    
    print(f"Mocking current time to: {yesterday_eod}")
    
    def mock_now_ist():
        return yesterday_eod
        
    class MockDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return yesterday_eod

    print("Patching time functions and initializing LiveRunner...")
    with patch("run_daily._now_ist", side_effect=mock_now_ist), \
         patch("data_manager_daily.datetime", MockDatetime):
         
        runner = run_daily.LiveRunner()
        
        print("Running EOD evaluation for yesterday...")
        runner._eod_evaluation()
        
        print(f"EOD evaluation completed.")
        print(f"Pending Entries Queued: {len(runner.portfolio._pending_entries)}")
        print(f"Pending Exits Queued: {len(runner.portfolio._pending_exits)}")
        print("\nState saved successfully!")
        print("You can now start 'python run_daily.py' normally. The pending orders will be executed since the market is open.")

if __name__ == "__main__":
    main()
