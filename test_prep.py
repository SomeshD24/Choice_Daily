import sys
from pathlib import Path

from strategy_variant_sector_regime import load_basket_configs, prepare_global_data, SECTOR_MAP, PEER_INDICATORS

print("Loading configs...")
configs = load_basket_configs(csv_paths=[r"data\baskets_nifty200_all_sizes.csv"])
size_configs = [c for c in configs if c["basket_size"] == 6]
if not size_configs:
    print("No config found")
    sys.exit(1)
    
print("Calling prepare_global_data...")
prepare_global_data(size_configs)

print("Sectors loaded:", len(SECTOR_MAP))
print("Indicators loaded:", len(PEER_INDICATORS))
