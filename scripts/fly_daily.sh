#!/bin/sh
# Scheduled daily job (post UTC close). Prefer 00:15 UTC.
set -eu
mkdir -p "${DATA_ROOT:-/data}/live" "${DATA_ROOT:-/data}/cache" "${DATA_ROOT:-/data}/out" "${DATA_ROOT:-/data}/paper"
# Weekly full OHLC refresh if FLY_REFRESH=1
if [ "${FLY_REFRESH:-0}" = "1" ]; then
  exec python run_daily.py --refresh --skip-dashboard
fi
exec python run_daily.py --no-refresh --skip-dashboard
