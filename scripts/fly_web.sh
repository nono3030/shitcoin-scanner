#!/bin/sh
set -eu
mkdir -p "${DATA_ROOT:-/data}/live" "${DATA_ROOT:-/data}/cache" "${DATA_ROOT:-/data}/out" "${DATA_ROOT:-/data}/paper"
export BIND_HOST="${BIND_HOST:-0.0.0.0}"
export PORT="${PORT:-8080}"
exec python dashboard.py --serve --no-open --host "$BIND_HOST" --port "$PORT"
