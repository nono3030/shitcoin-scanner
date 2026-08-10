#!/usr/bin/env python3
import os

os.environ.setdefault("DATA_ROOT", "/data")
os.environ["KRAKEN_ALLOW_FULL_DOWNLOAD"] = "1"

from kraken_data import load_or_refresh

ohlc, _ = load_or_refresh(refresh=True)
print(f"cached_series={len(ohlc)}")
