#!/usr/bin/env python3
"""
Run run_daily.py once per UTC day shortly after the daily candle close.

Default: 00:15 UTC (close is 00:00 UTC). Override with:
  DAILY_RUN_UTC_HOUR=0
  DAILY_RUN_UTC_MINUTE=15

Weekly full OHLC refresh: Sundays (UTC) with --refresh, other days --no-refresh.
Set RUN_ON_START=1 to fire once immediately after boot (ops/debug).
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent
DATA_ROOT = Path(os.environ.get("DATA_ROOT", str(APP_ROOT)))
STAMP_FILE = DATA_ROOT / "out" / "last_daily_run.utc"
LOG_FILE = DATA_ROOT / "out" / "scheduler.log"


def _log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _run_hour_minute() -> tuple[int, int]:
    h = int(os.environ.get("DAILY_RUN_UTC_HOUR", "0"))
    m = int(os.environ.get("DAILY_RUN_UTC_MINUTE", "15"))
    return max(0, min(23, h)), max(0, min(59, m))


def _next_run_after(now: datetime) -> datetime:
    h, m = _run_hour_minute()
    candidate = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if now >= candidate:
        candidate = candidate + timedelta(days=1)
    return candidate


def _already_ran_today(now: datetime) -> bool:
    if not STAMP_FILE.exists():
        return False
    try:
        day = STAMP_FILE.read_text(encoding="utf-8").strip()[:10]
        return day == now.strftime("%Y-%m-%d")
    except OSError:
        return False


def _mark_ran(now: datetime) -> None:
    STAMP_FILE.parent.mkdir(parents=True, exist_ok=True)
    STAMP_FILE.write_text(now.strftime("%Y-%m-%dT%H:%M:%SZ"), encoding="utf-8")


def run_daily_job(force_refresh: bool | None = None) -> int:
    """Invoke run_daily.py. Returns process exit code."""
    now = datetime.now(timezone.utc)
    if force_refresh is None:
        # Sunday UTC = full refresh
        force_refresh = now.weekday() == 6

    cmd = [sys.executable, str(APP_ROOT / "run_daily.py")]
    if force_refresh:
        cmd.append("--refresh")
        os.environ["KRAKEN_ALLOW_FULL_DOWNLOAD"] = "1"
    else:
        cmd.append("--no-refresh")
    # Dashboard is already served by entrypoint; skip regen to save time
    cmd.append("--skip-dashboard")

    env = os.environ.copy()
    env["DATA_ROOT"] = str(DATA_ROOT)
    env["PYTHONPATH"] = str(APP_ROOT) + os.pathsep + env.get("PYTHONPATH", "")

    _log(f"START {' '.join(cmd)}")
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(APP_ROOT),
            env=env,
            check=False,
        )
        code = int(proc.returncode)
    except Exception as e:
        _log(f"FAILED spawn: {e}")
        return 1
    _log(f"DONE exit={code}")
    return code


def main() -> int:
    h, m = _run_hour_minute()
    _log(f"scheduler up | daily at {h:02d}:{m:02d} UTC | DATA_ROOT={DATA_ROOT}")

    if os.environ.get("RUN_ON_START", "").strip() in ("1", "true", "yes"):
        now = datetime.now(timezone.utc)
        if not _already_ran_today(now):
            _log("RUN_ON_START=1 → firing job now")
            code = run_daily_job()
            if code == 0:
                _mark_ran(now)
        else:
            _log("RUN_ON_START skipped (already ran today)")

    while True:
        now = datetime.now(timezone.utc)
        h, m = _run_hour_minute()
        window_start = now.replace(hour=h, minute=m, second=0, microsecond=0)
        # Fire if we are in [run_time, run_time+10min) and not yet stamped today
        window_end = window_start + timedelta(minutes=10)

        if window_start <= now < window_end and not _already_ran_today(now):
            _log("daily window open → run_daily")
            code = run_daily_job()
            # stamp even on non-zero to avoid hammering; ops can delete stamp to retry
            _mark_ran(now)
            if code != 0:
                _log(f"job exit {code} (stamped; delete {STAMP_FILE} to retry today)")
            # sleep past window
            time.sleep(60)
            continue

        nxt = _next_run_after(now)
        sleep_s = max(5.0, min(60.0, (nxt - now).total_seconds()))
        # log once per ~30 min when far away
        if int(now.timestamp()) % 1800 < 65:
            _log(f"idle next_run={nxt.isoformat()} sleep={sleep_s:.0f}s")
        time.sleep(sleep_s)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        _log("scheduler stopped")
        raise SystemExit(0)
