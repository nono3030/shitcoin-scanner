#!/usr/bin/env python3
"""
Job daily full-auto (paper d'abord, live plus tard).

Pipeline:
  1. Refresh OHLC (optionnel)
  2. Scan FADE-BLOWOFF-T3
  3. Queue paper/live entries (cap max positions)
  4. Fill opens @ next open (si dispo)
  5. Mark MTM
  6. Close time-exit (hold_days)
  7. Regen dashboard HTML
  8. Log résumé

Usage:
  python run_daily.py              # job normal
  python run_daily.py --no-refresh # plus rapide (cache)
  python run_daily.py --dry-run    # scan only, no paper actions
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

from config import (
    BOT_LOG,
    EQUITY_USD,
    EXECUTION_MODE,
    FULL_AUTO,
    HOLD_DAYS,
    MAX_OPEN_POSITIONS,
    OUT_DIR,
    PAPER_STATE,
    PROFILE_NAME,
    REFRESH_OHLC_ON_RUN,
    SIGNALS_FILE,
    active_rule,
    position_notional,
    profile_summary,
)


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[{ts}] {msg}"
    print(line)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with BOT_LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def step_scan(refresh: bool) -> list[dict]:
    from scan_fade_signals import scan
    from kraken_data import load_or_refresh

    log(f"SCAN start refresh={refresh}")
    ohlc, _ = load_or_refresh(refresh=refresh)
    signals, near = scan(ohlc)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "rule": active_rule().describe(),
        "profile": profile_summary(),
        "paper_equity_usd": EQUITY_USD,
        "notional_per_trade_usd": position_notional(EQUITY_USD),
        "signals": signals,
        "near_misses": near[:20],
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    SIGNALS_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    log(f"SCAN done signals={len(signals)} near={len(near)} notional=${position_notional():.2f}")
    for i, s in enumerate(signals[:10], 1):
        log(
            f"  #{i} {s['pair']} 3d={s.get('ret_3d')} RSI={s.get('rsi14')} "
            f"vol={s.get('vol_spike')} sig={s.get('signal_date')}"
        )
    return signals


def step_paper(signals: list[dict], dry: bool) -> None:
    if dry:
        log("PAPER dry-run — skip open/fill/close")
        return
    if EXECUTION_MODE != "paper":
        log(f"EXECUTION_MODE={EXECUTION_MODE} — live path not wired yet, falling back to paper actions")

    from paper_book import close_due, fill_opens, mark, open_from_signals, status

    log(f"PAPER queue up to {MAX_OPEN_POSITIONS} slots | FULL_AUTO={FULL_AUTO}")
    if not FULL_AUTO:
        log("FULL_AUTO=False — signals logged only")
        return

    open_from_signals(signals)
    fill_opens()
    mark()
    close_due()
    # status to log
    if PAPER_STATE.exists():
        st = json.loads(PAPER_STATE.read_text(encoding="utf-8"))
        open_n = sum(1 for p in st.get("positions", []) if p.get("status") == "open")
        pend_n = sum(1 for p in st.get("positions", []) if p.get("status") == "pending")
        closed_n = sum(1 for p in st.get("positions", []) if p.get("status") == "closed")
        log(
            f"PAPER state open={open_n} pending={pend_n} closed={closed_n} "
            f"realized=${st.get('cash_pnl', 0):+.2f}"
        )


def step_dashboard() -> None:
    try:
        from dashboard import write_dashboard

        path = write_dashboard(auto_refresh=False, use_ohlc=True)
        log(f"DASHBOARD → {path}")
    except Exception as e:
        log(f"DASHBOARD warn: {e}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Fade daily automation job")
    ap.add_argument("--no-refresh", action="store_true", help="Skip full OHLC re-download")
    ap.add_argument("--refresh", action="store_true", help="Force full OHLC refresh")
    ap.add_argument("--dry-run", action="store_true", help="Scan only")
    ap.add_argument("--skip-dashboard", action="store_true")
    args = ap.parse_args()

    refresh = REFRESH_OHLC_ON_RUN
    if args.no_refresh:
        refresh = False
    if args.refresh:
        refresh = True

    log("=" * 72)
    log(f"RUN_DAILY start | {profile_summary()}")
    log(f"rule={active_rule().describe()}")
    log(f"hold_days={HOLD_DAYS}")

    try:
        signals = step_scan(refresh=refresh)
        step_paper(signals, dry=args.dry_run)
        if not args.skip_dashboard:
            step_dashboard()
        log("RUN_DAILY OK")
        return 0
    except Exception:
        log("RUN_DAILY FAILED\n" + traceback.format_exc())
        return 1


if __name__ == "__main__":
    sys.exit(main())
