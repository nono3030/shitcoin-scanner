#!/usr/bin/env python3
"""
Live trading book — FADE-BLOWOFF-T3 on Bybit linear USDT perps.

- Scan signals stay Kraken-derived (pair = BASE/USD)
- Execution maps to Bybit PAIRUSDT via broker_bybit
- SHORT market, exit time-only after hold_days daily bars
- Compounding on live equity when COMPOUNDING=True
- Kill if equity < MIN_EQUITY_USD

State: live/open_positions.json  ·  Ledger: live/ledger.jsonl

Usage:
  python live_book.py status
  python live_book.py close-due
  python live_book.py open-from-signals   # uses out/fade_signals_latest.json
"""

from __future__ import annotations

import argparse
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import (
    COMPOUNDING,
    EQUITY_USD,
    HOLD_DAYS,
    LEVERAGE,
    LIVE_DIR,
    LIVE_LEDGER,
    LIVE_STATE,
    MAX_OPEN_POSITIONS,
    MIN_EQUITY_USD,
    SIGNALS_FILE,
    active_rule,
    position_notional,
    profile_summary,
)
from broker_bybit import BybitBroker, default_broker, kraken_to_bybit_symbol
from kraken_data import load_or_refresh


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_state() -> dict[str, Any]:
    LIVE_DIR.mkdir(parents=True, exist_ok=True)
    if not LIVE_STATE.exists():
        return {
            "equity_start": EQUITY_USD,
            "profile": profile_summary(),
            "killed": False,
            "kill_reason": None,
            "cash_pnl": 0.0,
            "positions": [],
        }
    return json.loads(LIVE_STATE.read_text(encoding="utf-8"))


def _save_state(state: dict[str, Any]) -> None:
    LIVE_DIR.mkdir(parents=True, exist_ok=True)
    LIVE_STATE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _append_ledger(event: dict[str, Any]) -> None:
    LIVE_DIR.mkdir(parents=True, exist_ok=True)
    with LIVE_LEDGER.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event) + "\n")


def _openish(state: dict[str, Any]) -> list[dict[str, Any]]:
    return [p for p in state.get("positions", []) if p.get("status") == "open"]


def _bars_since_entry(pair: str, entry_date: str, ohlc: dict) -> tuple[int, float | None, str | None]:
    series = ohlc.get(pair) or []
    bars = [c for c in series if c.date >= entry_date]
    if not bars:
        return 0, None, None
    last = bars[-1]
    return len(bars), last.c, last.date


def _live_equity(broker: BybitBroker) -> float:
    return float(broker.get_equity_usdt())


def _sizing_equity(broker: BybitBroker, state: dict[str, Any]) -> float:
    if COMPOUNDING:
        try:
            return _live_equity(broker)
        except Exception as e:
            print(f"  warn: live equity fetch failed ({e}); fallback equity_start")
            return float(state.get("equity_start") or EQUITY_USD)
    return float(state.get("equity_start") or EQUITY_USD)


def check_kill(broker: BybitBroker | None = None, state: dict[str, Any] | None = None) -> tuple[bool, float, str | None]:
    """Return (killed, equity, reason). Persists kill flag on state if provided."""
    br = broker or default_broker()
    st = state if state is not None else _load_state()
    try:
        eq = _live_equity(br)
    except Exception as e:
        return True, 0.0, f"equity_fetch_failed: {e}"

    if eq < MIN_EQUITY_USD:
        reason = f"equity ${eq:.2f} < MIN_EQUITY_USD ${MIN_EQUITY_USD:.2f}"
        st["killed"] = True
        st["kill_reason"] = reason
        st["killed_at"] = _now()
        if state is not None:
            _save_state(st)
        else:
            _save_state(st)
        _append_ledger({"ts": _now(), "event": "kill_switch", "equity": eq, "reason": reason})
        return True, eq, reason

    # clear kill if equity recovered (optional safety — stay killed once tripped)
    return bool(st.get("killed")), eq, st.get("kill_reason")


def open_from_signals(
    signals: list[dict] | None = None,
    broker: BybitBroker | None = None,
) -> dict[str, Any]:
    """
    Rank signals by ret_3d desc, open market shorts up to MAX_OPEN_POSITIONS.
    Sizing: position_notional(live equity) when COMPOUNDING else start equity.
    """
    br = broker or default_broker()
    state = _load_state()
    summary: dict[str, Any] = {"opened": [], "skipped": [], "killed": False}

    killed, equity, reason = check_kill(br, state)
    if killed:
        msg = f"KILL active — no new opens ({reason})"
        print(msg)
        summary["killed"] = True
        summary["kill_reason"] = reason
        summary["equity"] = equity
        return summary

    if signals is None:
        if not SIGNALS_FILE.exists():
            print(f"No signals file at {SIGNALS_FILE}")
            return summary
        payload = json.loads(SIGNALS_FILE.read_text(encoding="utf-8"))
        signals = payload.get("signals") or []

    openish = _openish(state)
    slots = max(0, MAX_OPEN_POSITIONS - len(openish))
    if slots == 0:
        print(f"Max open positions reached ({MAX_OPEN_POSITIONS}).")
        summary["skipped"].append({"reason": "max_positions"})
        return summary

    existing_pairs = {p["pair"] for p in openish}
    existing_symbols = {p.get("bybit_symbol") for p in openish if p.get("bybit_symbol")}
    # also respect exchange-side positions
    try:
        for ep in br.list_open_positions():
            if ep.get("symbol"):
                existing_symbols.add(ep["symbol"])
    except Exception as e:
        print(f"  warn: list_open_positions failed: {e}")

    ranked = sorted(
        signals,
        key=lambda s: float(s.get("ret_3d") or 0.0),
        reverse=True,
    )
    rule = active_rule()
    eq_for_size = _sizing_equity(br, state)
    notional = position_notional(eq_for_size)
    print(
        f"LIVE open_from_signals equity=${eq_for_size:.2f} "
        f"notional=${notional:.2f} slots={slots} signals={len(ranked)}"
    )

    opened = 0
    for s in ranked:
        if opened >= slots:
            break
        pair = s.get("pair")
        if not pair:
            continue
        if pair in existing_pairs:
            summary["skipped"].append({"pair": pair, "reason": "already_tracked"})
            continue
        try:
            symbol = kraken_to_bybit_symbol(pair)
        except ValueError as e:
            summary["skipped"].append({"pair": pair, "reason": f"map_fail: {e}"})
            continue
        if symbol in existing_symbols:
            summary["skipped"].append({"pair": pair, "symbol": symbol, "reason": "already_on_exchange"})
            continue

        # re-check equity kill before each order
        killed, equity, reason = check_kill(br, state)
        if killed:
            print(f"KILL before order: {reason}")
            summary["killed"] = True
            summary["kill_reason"] = reason
            break

        eq_for_size = _sizing_equity(br, state)
        notional = position_notional(eq_for_size)

        try:
            order = br.open_short(symbol, notional)
        except Exception as e:
            print(f"  FAIL open short {pair}→{symbol}: {e}")
            summary["skipped"].append({"pair": pair, "symbol": symbol, "reason": str(e)})
            _append_ledger(
                {
                    "ts": _now(),
                    "event": "open_failed",
                    "pair": pair,
                    "symbol": symbol,
                    "error": str(e),
                }
            )
            continue

        entry_px = None
        try:
            entry_px = br.get_ticker_price(symbol)
        except Exception:
            entry_px = float(s.get("close") or 0) or None

        pos = {
            "id": str(uuid.uuid4())[:8],
            "pair": pair,
            "bybit_symbol": symbol,
            "status": "open",
            "side": "short",
            "rule": rule.name,
            "signal_date": s.get("signal_date"),
            "signal_close": s.get("close"),
            "ret_3d": s.get("ret_3d"),
            "rsi14": s.get("rsi14"),
            "vol_spike": s.get("vol_spike"),
            "dist_sma20": s.get("dist_sma20"),
            "notional_usd": notional,
            "qty": order.get("qty"),
            "leverage": LEVERAGE,
            "hold_days": int(s.get("hold_days") or HOLD_DAYS),
            "entry_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "entry_px": entry_px,
            "entry_order_id": order.get("orderId"),
            "exit_date": None,
            "exit_px": None,
            "exit_order_id": None,
            "bars_held": 0,
            "realized_pnl_usd": None,
            "realized_pnl_pct": None,
            "created_at": _now(),
        }
        state["positions"].append(pos)
        existing_pairs.add(pair)
        existing_symbols.add(symbol)
        opened += 1
        summary["opened"].append({"id": pos["id"], "pair": pair, "symbol": symbol, "notional": notional})
        _append_ledger({"ts": _now(), "event": "opened_short", "position": pos, "order": {
            k: order.get(k) for k in ("orderId", "qty", "notional_usd", "symbol", "side")
        }})
        print(
            f"  OPENED short {pair}→{symbol} id={pos['id']} "
            f"qty={pos['qty']} notional=${notional:.2f} orderId={order.get('orderId')}"
        )

    _save_state(state)
    if opened == 0 and not summary["killed"]:
        print("No new live positions opened.")
    summary["equity"] = equity if "equity" in summary else eq_for_size
    summary["opened_n"] = opened
    return summary


def close_due(broker: BybitBroker | None = None) -> dict[str, Any]:
    """Close shorts when bars_held >= hold_days (Kraken OHLC bar count)."""
    br = broker or default_broker()
    state = _load_state()
    ohlc, _ = load_or_refresh(refresh=False)
    closed = 0
    summary: dict[str, Any] = {"closed": [], "held": []}

    for p in state.get("positions", []):
        if p.get("status") != "open":
            continue
        pair = p["pair"]
        entry_date = p.get("entry_date") or p.get("signal_date")
        if not entry_date:
            print(f"  skip {pair}: no entry_date")
            continue
        n, last_c, last_d = _bars_since_entry(pair, entry_date, ohlc)
        p["bars_held"] = n
        hold = int(p.get("hold_days") or HOLD_DAYS)
        if n < hold:
            summary["held"].append({"pair": pair, "bars": n, "hold": hold})
            continue

        symbol = p.get("bybit_symbol") or kraken_to_bybit_symbol(pair)
        qty = p.get("qty")
        try:
            order = br.close_short(symbol, qty=qty)
        except Exception as e:
            # retry full size from exchange
            try:
                order = br.close_short(symbol, qty=None)
            except Exception as e2:
                print(f"  FAIL close {pair}→{symbol}: {e2}")
                _append_ledger(
                    {
                        "ts": _now(),
                        "event": "close_failed",
                        "pair": pair,
                        "symbol": symbol,
                        "error": str(e2),
                        "prev_error": str(e),
                    }
                )
                continue

        exit_px = None
        try:
            exit_px = br.get_ticker_price(symbol)
        except Exception:
            exit_px = last_c

        entry_px = p.get("entry_px")
        net = None
        pnl_usd = None
        if entry_px and exit_px and float(entry_px) > 0:
            # short PnL
            gross = (float(entry_px) - float(exit_px)) / float(entry_px)
            net = gross  # live fees already on exchange; estimate without FEE_RT
            notional = float(p.get("notional_usd") or 0)
            pnl_usd = net * notional

        p["status"] = "closed"
        p["exit_date"] = last_d or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        p["exit_px"] = exit_px
        p["exit_order_id"] = order.get("orderId")
        p["realized_pnl_pct"] = net
        p["realized_pnl_usd"] = pnl_usd
        p["exit_reason"] = "time_exit"
        if pnl_usd is not None:
            state["cash_pnl"] = float(state.get("cash_pnl") or 0.0) + pnl_usd
        closed += 1
        summary["closed"].append(
            {
                "pair": pair,
                "symbol": symbol,
                "pnl_usd": pnl_usd,
                "orderId": order.get("orderId"),
            }
        )
        _append_ledger({"ts": _now(), "event": "closed_time_exit", "position": dict(p)})
        print(
            f"  CLOSED {pair}→{symbol} bars={n}/{hold} "
            f"entry={entry_px} exit={exit_px} "
            f"pnl%={(net or 0)*100:+.2f}% pnl$={(pnl_usd or 0):+.2f} "
            f"orderId={order.get('orderId')}"
        )

    _save_state(state)
    print(f"Closed {closed}. est_cash_pnl=${state.get('cash_pnl', 0):+.2f}")
    summary["closed_n"] = closed
    return summary


def status(broker: BybitBroker | None = None) -> None:
    br = broker
    state = _load_state()
    equity = None
    exch_pos: list[dict] = []
    err = None
    try:
        br = br or default_broker()
        equity = _live_equity(br)
        exch_pos = br.list_open_positions()
    except Exception as e:
        err = str(e)

    print("=" * 72)
    print(f"LIVE BOOK  {profile_summary()}")
    print(f"equity_start=${state.get('equity_start', EQUITY_USD):.2f}  "
          f"est_realized_pnl=${state.get('cash_pnl', 0):+.2f}")
    if equity is not None:
        print(f"live_equity_usdt=${equity:.4f}  MIN_EQUITY=${MIN_EQUITY_USD:.2f}")
    if state.get("killed"):
        print(f"*** KILLED *** {state.get('kill_reason')}")
    if err:
        print(f"broker: ERROR {err}")
    print(f"Rule: {active_rule().describe()}")
    print("=" * 72)

    by: dict[str, list] = {"open": [], "closed": []}
    for p in state.get("positions", []):
        by.setdefault(p.get("status") or "?", []).append(p)

    print(f"\n[OPEN] n={len(by.get('open') or [])}")
    for p in (by.get("open") or [])[-20:]:
        print(
            f"  {p.get('id')} {p.get('pair'):<12} → {p.get('bybit_symbol')}  "
            f"entry={p.get('entry_date')} @ {p.get('entry_px')}  "
            f"bars={p.get('bars_held')}/{p.get('hold_days')}  "
            f"notional=${p.get('notional_usd')} qty={p.get('qty')}"
        )

    print(f"\n[CLOSED] n={len(by.get('closed') or [])}")
    for p in (by.get("closed") or [])[-20:]:
        rp = p.get("realized_pnl_pct")
        ru = p.get("realized_pnl_usd")
        print(
            f"  {p.get('id')} {p.get('pair'):<12} {p.get('entry_date')}->{p.get('exit_date')}  "
            f"pnl={(rp or 0)*100:+.2f}%  ${(ru or 0):+.2f}"
        )

    print(f"\n[EXCHANGE POSITIONS] n={len(exch_pos)}")
    for ep in exch_pos:
        print(
            f"  {ep.get('symbol'):<12} side={ep.get('side')} size={ep.get('size')} "
            f"avg={ep.get('avgPrice')} uPnL={ep.get('unrealisedPnl')}"
        )


def reset(force: bool = False) -> None:
    if not force:
        print("Pass --force to wipe live state (does NOT close exchange positions).")
        return
    if LIVE_STATE.exists():
        LIVE_STATE.unlink()
    print("Live state reset (exchange positions untouched).")


def main() -> None:
    ap = argparse.ArgumentParser(description="Live book for fade shorts (Bybit)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("close-due")
    sub.add_parser("open-from-signals")
    p_reset = sub.add_parser("reset")
    p_reset.add_argument("--force", action="store_true")
    args = ap.parse_args()

    if args.cmd == "status":
        status()
    elif args.cmd == "close-due":
        close_due()
    elif args.cmd == "open-from-signals":
        open_from_signals()
    elif args.cmd == "reset":
        reset(force=args.force)


if __name__ == "__main__":
    main()
