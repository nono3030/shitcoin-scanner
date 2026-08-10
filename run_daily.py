#!/usr/bin/env python3
"""
Job daily full-auto (LIVE Bybit or paper).

Pipeline:
  1. Refresh OHLC (optionnel)
  2. Scan FADE-BLOWOFF-T3 (Kraken)
  3. If live: close_due → open_from_signals (Bybit), no paper fallback
     If paper: queue → fill → mark → close
  4. Regen dashboard HTML
  5. Log résumé

Usage:
  python run_daily.py              # job normal
  python run_daily.py --no-refresh # plus rapide (cache)
  python run_daily.py --dry-run    # scan only, no trade actions
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
    COMPOUNDING,
    ENTRY_MODE,
    EQUITY_USD,
    EXECUTION_MODE,
    FULL_AUTO,
    HOLD_DAYS,
    LIVE_STATE,
    LIVE_TRADE_UTC_END_HOUR,
    LIVE_TRADE_UTC_START_HOUR,
    MAX_OPEN_POSITIONS,
    MIN_EQUITY_USD,
    OUT_DIR,
    PAPER_STATE,
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
        "equity_usd": EQUITY_USD,
        "paper_equity_usd": EQUITY_USD,
        "notional_per_trade_usd": position_notional(EQUITY_USD),
        "execution_mode": EXECUTION_MODE,
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

    from paper_book import close_due, fill_opens, mark, open_from_signals

    log(f"PAPER queue up to {MAX_OPEN_POSITIONS} slots | FULL_AUTO={FULL_AUTO}")
    if not FULL_AUTO:
        log("FULL_AUTO=False — signals logged only")
        return

    open_from_signals(signals)
    fill_opens()
    mark()
    close_due()
    if PAPER_STATE.exists():
        st = json.loads(PAPER_STATE.read_text(encoding="utf-8"))
        open_n = sum(1 for p in st.get("positions", []) if p.get("status") == "open")
        pend_n = sum(1 for p in st.get("positions", []) if p.get("status") == "pending")
        closed_n = sum(1 for p in st.get("positions", []) if p.get("status") == "closed")
        log(
            f"PAPER state open={open_n} pending={pend_n} closed={closed_n} "
            f"realized=${st.get('cash_pnl', 0):+.2f}"
        )


def step_live(signals: list[dict], dry: bool, force_trade: bool = False) -> None:
    """
    LIVE path (daily close process):
      1. close_due   — time exit if bars_held >= hold_days (exchange only in UTC window)
      2. fill_opens  — pending → market at next daily open
      3. open_from_signals — queue new signals as pending (ENTRY_MODE=next_open)
      4. fill_opens  — fill any newly ready pendings in the same EOD run
    No paper fallback. No market entry merely because the script was launched mid-day.
    """
    if dry:
        log("LIVE dry-run — skip close/fill/open")
        return

    if not FULL_AUTO:
        log("FULL_AUTO=False — signals logged only (live)")
        return

    from live_book import check_kill, close_due, fill_opens, in_trade_window, open_from_signals
    from broker_bybit import default_broker

    ok_win, win_msg = in_trade_window(force=force_trade)
    log(
        f"LIVE path | entry={ENTRY_MODE} | window "
        f"{LIVE_TRADE_UTC_START_HOUR:02d}-{LIVE_TRADE_UTC_END_HOUR:02d}Z | "
        f"{win_msg} | max_pos={MAX_OPEN_POSITIONS} compound={COMPOUNDING} "
        f"min_eq=${MIN_EQUITY_USD}"
    )
    if not ok_win:
        log(
            "LIVE note: exchange fills/closes deferred outside post-close window. "
            "Signals still scanned & may be queued pending. "
            "Schedule run_daily ~00:05–03:00 UTC (or use --force-trade)."
        )

    try:
        br = default_broker()
        killed, equity, reason = check_kill(br)
        log(f"LIVE equity_usdt=${equity:.4f} killed={killed}")
        if killed:
            log(f"LIVE KILL — abort new entries: {reason}")
            close_summary = close_due(broker=br, force_trade=force_trade)
            log(f"LIVE close_due closed={close_summary.get('closed_n', 0)}")
            return
    except Exception as e:
        log(f"LIVE broker init/equity FAILED: {e}")
        raise

    # 1) time exits first (free slots) — only hits exchange in trade window
    close_summary = close_due(broker=br, force_trade=force_trade)
    log(
        f"LIVE close_due closed={close_summary.get('closed_n', 0)} "
        f"deferred={len(close_summary.get('deferred') or [])}"
    )

    # 2) fill pending from previous days (next open ready)
    fill1 = fill_opens(broker=br, force_trade=force_trade)
    log(
        f"LIVE fill_opens#1 filled={fill1.get('filled_n', 0)} "
        f"waiting={len(fill1.get('waiting') or [])}"
    )

    # 3) queue new signals (pending if next_open — no immediate market)
    open_summary = open_from_signals(signals, broker=br, force_trade=force_trade)
    log(
        f"LIVE open_from_signals queued={open_summary.get('queued_n', 0)} "
        f"opened={open_summary.get('opened_n', 0)} "
        f"killed={open_summary.get('killed', False)}"
    )
    for o in open_summary.get("queued") or []:
        log(f"  queued {o.get('pair')}→{o.get('symbol')} signal={o.get('signal_date')}")
    for o in open_summary.get("opened") or []:
        log(f"  opened {o.get('pair')}→{o.get('symbol')} notional=${o.get('notional')}")
    for s in (open_summary.get("skipped") or [])[:10]:
        log(f"  skipped {s}")

    # 4) same EOD run: if next open already in OHLC, fill immediately
    fill2 = fill_opens(broker=br, force_trade=force_trade)
    log(
        f"LIVE fill_opens#2 filled={fill2.get('filled_n', 0)} "
        f"waiting={len(fill2.get('waiting') or [])}"
    )
    for f in fill2.get("filled") or []:
        log(f"  filled {f.get('pair')} entry={f.get('entry_date')} @ {f.get('entry_px')}")

    if LIVE_STATE.exists():
        st = json.loads(LIVE_STATE.read_text(encoding="utf-8"))
        open_n = sum(1 for p in st.get("positions", []) if p.get("status") == "open")
        pend_n = sum(1 for p in st.get("positions", []) if p.get("status") == "pending")
        closed_n = sum(1 for p in st.get("positions", []) if p.get("status") == "closed")
        log(
            f"LIVE state open={open_n} pending={pend_n} closed={closed_n} "
            f"est_realized=${st.get('cash_pnl', 0):+.2f} killed={st.get('killed', False)}"
        )


def step_execution(signals: list[dict], dry: bool, force_trade: bool = False) -> None:
    mode = (EXECUTION_MODE or "").strip().lower()
    if mode == "live":
        step_live(signals, dry=dry, force_trade=force_trade)
    elif mode == "paper":
        step_paper(signals, dry=dry)
    else:
        raise RuntimeError(
            f"Unknown EXECUTION_MODE={EXECUTION_MODE!r}. Use 'live' or 'paper'."
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
    ap.add_argument(
        "--force-trade",
        action="store_true",
        help="Live: ignore UTC post-close window (fills/closes anytime)",
    )
    args = ap.parse_args()

    refresh = REFRESH_OHLC_ON_RUN
    if args.no_refresh:
        refresh = False
    if args.refresh:
        refresh = True

    log("=" * 72)
    log(f"RUN_DAILY start | {profile_summary()}")
    log(f"rule={active_rule().describe()}")
    log(
        f"hold_days={HOLD_DAYS} | EXECUTION_MODE={EXECUTION_MODE} | "
        f"ENTRY_MODE={ENTRY_MODE} | force_trade={args.force_trade}"
    )

    try:
        signals = step_scan(refresh=refresh)
        step_execution(signals, dry=args.dry_run, force_trade=args.force_trade)
        if not args.skip_dashboard:
            step_dashboard()
        log("RUN_DAILY OK")
        return 0
    except Exception:
        log("RUN_DAILY FAILED\n" + traceback.format_exc())
        return 1


if __name__ == "__main__":
    sys.exit(main())
