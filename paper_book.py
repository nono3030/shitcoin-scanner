#!/usr/bin/env python3
"""
Paper trading ledger for FADE-BLOWOFF-T3.

Flow:
  1. python scan_fade_signals.py --paper-open   # queue signals
  2. python paper_book.py fill-opens            # fill at next open (uses latest OHLC)
  3. python paper_book.py mark                  # mark-to-market open shorts
  4. python paper_book.py close-due             # time-exit when hold_days reached
  5. python paper_book.py status

State files under paper/
"""

from __future__ import annotations

import argparse
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import (
    FEE_RT,
    HOLD_DAYS,
    MAX_OPEN_POSITIONS,
    PAPER_DIR,
    PAPER_EQUITY_USD,
    PAPER_LEDGER,
    PAPER_STATE,
    active_rule,
    position_notional,
)
from kraken_data import load_or_refresh

# pending = signal taken, waiting for entry fill at next open
# open = short filled
# closed = done


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_state() -> dict[str, Any]:
    PAPER_DIR.mkdir(parents=True, exist_ok=True)
    if not PAPER_STATE.exists():
        return {
            "equity_start": PAPER_EQUITY_USD,
            "cash_pnl": 0.0,
            "positions": [],
        }
    return json.loads(PAPER_STATE.read_text(encoding="utf-8"))


def _save_state(state: dict[str, Any]) -> None:
    PAPER_DIR.mkdir(parents=True, exist_ok=True)
    PAPER_STATE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _append_ledger(event: dict[str, Any]) -> None:
    PAPER_DIR.mkdir(parents=True, exist_ok=True)
    with PAPER_LEDGER.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event) + "\n")


def open_from_signals(signals: list[dict]) -> None:
    state = _load_state()
    openish = [p for p in state["positions"] if p["status"] in ("pending", "open")]
    slots = max(0, MAX_OPEN_POSITIONS - len(openish))
    if slots == 0:
        print(f"Max open positions reached ({MAX_OPEN_POSITIONS}).")
        return

    existing_pairs = {p["pair"] for p in openish}
    notional = position_notional(PAPER_EQUITY_USD)
    rule = active_rule()
    added = 0

    for s in signals:
        if added >= slots:
            break
        if s["pair"] in existing_pairs:
            continue
        pos = {
            "id": str(uuid.uuid4())[:8],
            "pair": s["pair"],
            "status": "pending",
            "rule": rule.name,
            "signal_date": s["signal_date"],
            "signal_close": s["close"],
            "ret_3d": s.get("ret_3d"),
            "rsi14": s.get("rsi14"),
            "vol_spike": s.get("vol_spike"),
            "dist_sma20": s.get("dist_sma20"),
            "notional_usd": s.get("suggested_notional_usd", notional),
            "hold_days": s.get("hold_days", HOLD_DAYS),
            "entry_date": None,
            "entry_px": None,
            "exit_date": None,
            "exit_px": None,
            "bars_held": 0,
            "realized_pnl_usd": None,
            "realized_pnl_pct": None,
            "created_at": _now(),
        }
        state["positions"].append(pos)
        existing_pairs.add(s["pair"])
        added += 1
        _append_ledger({"ts": _now(), "event": "signal_queued", "position": pos})
        print(f"  queued pending short {pos['pair']} id={pos['id']} notional=${pos['notional_usd']}")

    _save_state(state)
    if added == 0:
        print("No new paper positions queued.")


def fill_opens() -> None:
    """Fill pending shorts at today's open if signal_date < last bar date."""
    state = _load_state()
    ohlc, _ = load_or_refresh(refresh=False)
    filled = 0
    for p in state["positions"]:
        if p["status"] != "pending":
            continue
        series = ohlc.get(p["pair"])
        if not series:
            print(f"  skip {p['pair']}: no OHLC")
            continue
        # find first bar strictly after signal_date
        entry_bar = None
        for c in series:
            if c.date > p["signal_date"]:
                entry_bar = c
                break
        if entry_bar is None:
            print(f"  wait {p['pair']}: next open not available yet")
            continue
        p["status"] = "open"
        p["entry_date"] = entry_bar.date
        p["entry_px"] = entry_bar.o
        p["bars_held"] = 0
        # entry fee half of RT
        p["entry_fee_pct"] = FEE_RT / 2
        filled += 1
        _append_ledger({"ts": _now(), "event": "filled_short", "position": dict(p)})
        print(f"  FILLED short {p['pair']} @ {p['entry_px']} on {p['entry_date']}")
    _save_state(state)
    print(f"Filled {filled} position(s).")


def _bars_since_entry(pair: str, entry_date: str, ohlc: dict) -> tuple[int, float | None, str | None]:
    series = ohlc.get(pair) or []
    bars = [c for c in series if c.date >= entry_date]
    if not bars:
        return 0, None, None
    # bars_held = number of completed daily bars from entry (entry day counts as 1 once day exists)
    last = bars[-1]
    return len(bars), last.c, last.date


def mark() -> None:
    state = _load_state()
    ohlc, _ = load_or_refresh(refresh=False)
    print("Mark-to-market open shorts:")
    for p in state["positions"]:
        if p["status"] != "open":
            continue
        n, last_c, last_d = _bars_since_entry(p["pair"], p["entry_date"], ohlc)
        p["bars_held"] = n
        if last_c is None or not p.get("entry_px"):
            continue
        # short pnl
        gross = (p["entry_px"] - last_c) / p["entry_px"]
        # only entry fee paid so far; exit fee reserved
        net = gross - FEE_RT / 2
        u_pnl = net * p["notional_usd"]
        print(
            f"  {p['pair']:<14} entry={p['entry_px']:.6g} last={last_c:.6g} ({last_d})  "
            f"bars={n}/{p['hold_days']}  uPnL%={net*100:+.2f}%  uPnL$={u_pnl:+.2f}"
        )
    _save_state(state)


def close_due() -> None:
    state = _load_state()
    ohlc, _ = load_or_refresh(refresh=False)
    closed = 0
    for p in state["positions"]:
        if p["status"] != "open":
            continue
        n, last_c, last_d = _bars_since_entry(p["pair"], p["entry_date"], ohlc)
        p["bars_held"] = n
        if n < p["hold_days"]:
            continue
        if last_c is None:
            continue
        # time exit at last close once hold reached
        # Prefer exact hold_days-th bar close if available
        series = ohlc.get(p["pair"]) or []
        bars = [c for c in series if c.date >= p["entry_date"]]
        exit_bar = bars[p["hold_days"] - 1] if len(bars) >= p["hold_days"] else bars[-1]
        exit_px = exit_bar.c
        exit_date = exit_bar.date
        gross = (p["entry_px"] - exit_px) / p["entry_px"]
        net = gross - FEE_RT
        pnl_usd = net * p["notional_usd"]
        p["status"] = "closed"
        p["exit_date"] = exit_date
        p["exit_px"] = exit_px
        p["realized_pnl_pct"] = net
        p["realized_pnl_usd"] = pnl_usd
        p["exit_reason"] = "time_exit"
        state["cash_pnl"] = state.get("cash_pnl", 0.0) + pnl_usd
        closed += 1
        _append_ledger({"ts": _now(), "event": "closed_time_exit", "position": dict(p)})
        print(
            f"  CLOSED {p['pair']} entry={p['entry_px']:.6g} exit={exit_px:.6g} "
            f"net={net*100:+.2f}%  pnl=${pnl_usd:+.2f}"
        )
    _save_state(state)
    print(f"Closed {closed}. cash_pnl=${state.get('cash_pnl', 0):+.2f}")


def status() -> None:
    state = _load_state()
    print("=" * 72)
    print(f"PAPER BOOK  equity_start=${state.get('equity_start', PAPER_EQUITY_USD):.2f}  "
          f"realized_pnl=${state.get('cash_pnl', 0):+.2f}")
    print(f"Rule: {active_rule().describe()}")
    print("=" * 72)
    by = {"pending": [], "open": [], "closed": []}
    for p in state["positions"]:
        by.setdefault(p["status"], []).append(p)
    for st in ("pending", "open", "closed"):
        xs = by.get(st) or []
        print(f"\n[{st.upper()}] n={len(xs)}")
        for p in xs[-20:]:
            if st == "closed":
                print(
                    f"  {p['id']} {p['pair']:<12} {p.get('entry_date')}->{p.get('exit_date')}  "
                    f"pnl={p.get('realized_pnl_pct', 0)*100:+.2f}%  ${p.get('realized_pnl_usd', 0):+.2f}"
                )
            elif st == "open":
                print(
                    f"  {p['id']} {p['pair']:<12} entry={p.get('entry_date')} @ {p.get('entry_px')}  "
                    f"bars={p.get('bars_held')}/{p.get('hold_days')}  notional=${p.get('notional_usd')}"
                )
            else:
                print(
                    f"  {p['id']} {p['pair']:<12} signal={p.get('signal_date')}  "
                    f"ret3d={p.get('ret_3d')}  wait next open"
                )
    closed = by.get("closed") or []
    if closed:
        wins = [p for p in closed if (p.get("realized_pnl_usd") or 0) > 0]
        print(f"\nClosed stats: n={len(closed)} WR={len(wins)/len(closed)*100:.1f}%  "
              f"sum=${sum(p.get('realized_pnl_usd') or 0 for p in closed):+.2f}")


def reset(force: bool = False) -> None:
    if not force:
        print("Pass --force to wipe paper state.")
        return
    if PAPER_STATE.exists():
        PAPER_STATE.unlink()
    print("Paper state reset.")


def main() -> None:
    ap = argparse.ArgumentParser(description="Paper book for fade shorts")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("fill-opens")
    sub.add_parser("mark")
    sub.add_parser("close-due")
    p_reset = sub.add_parser("reset")
    p_reset.add_argument("--force", action="store_true")
    args = ap.parse_args()

    if args.cmd == "status":
        status()
    elif args.cmd == "fill-opens":
        fill_opens()
    elif args.cmd == "mark":
        mark()
    elif args.cmd == "close-due":
        close_due()
    elif args.cmd == "reset":
        reset(force=args.force)


if __name__ == "__main__":
    main()
