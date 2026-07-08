import os
import sys
import pytz
from datetime import datetime, timedelta
from unittest.mock import patch

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import run_daily
import data_manager_daily

def trigger_yesterday_eod():
    print("Triggering EOD Evaluation for yesterday...")
    IST = pytz.timezone("Asia/Kolkata")
    now_ist = datetime.now(IST)
    
    # We want to mock time to be yesterday 15:45 IST
    yesterday_eod = (now_ist - timedelta(days=1)).replace(hour=15, minute=45, second=0, microsecond=0)
    
    print(f"Mocking current time to: {yesterday_eod}")
    
    def mock_now_ist():
        return yesterday_eod
        
    class MockDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return yesterday_eod

    # We will patch the DailyTradingRunner.run_forever to just call _eod_evaluation once
    def mock_run_forever(self):
        print("Running EOD evaluation for yesterday...")
        self._eod_evaluation()
        print(f"EOD evaluation completed.")
        print(f"Pending Entries Queued: {len(self.portfolio._pending_entries)}")
        print(f"Pending Exits Queued: {len(self.portfolio._pending_exits)}")
        print("State saved successfully!")

    print("Patching time functions and invoking run_daily.main()...")
    with patch("run_daily._now_ist", side_effect=mock_now_ist), \
         patch("data_manager_daily.datetime", MockDatetime), \
         patch("run_daily.DailyTradingRunner.run_forever", mock_run_forever):
         
        # Reset sys.argv to avoid argparse issues if any
        sys.argv = ["run_daily.py"]
        run_daily.main()
        
    print("\nSUCCESS! The signals from yesterday have now been evaluated.")
    print("You can now start 'python run_daily.py' normally. It will perform the _morning_execution and enter the qualifying baskets.")

if __name__ == "__main__":
    trigger_yesterday_eod()
