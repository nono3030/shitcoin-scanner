#!/usr/bin/env python3
"""
Live trading book — FADE-BLOWOFF-T3 on Bybit linear USDT perps.

Timing (daily UTC, aligned with backtest):
  1. Signal on last *closed* Kraken daily bar
  2. Queue pending (no order yet)
  3. fill_opens → market short when next daily open exists
  4. process_dca → soft add short at +10%/+20% adverse (cap 2×), if enabled
  5. close_due → reduceOnly after hold_days daily bars

Exchange orders only inside LIVE_TRADE_UTC_* window (post daily close),
unless force_trade=True / --force-trade / env LIVE_FORCE_TRADE=1.

Usage:
  python live_book.py status
  python live_book.py close-due
  python live_book.py fill-opens
  python live_book.py open-from-signals
  python live_book.py open-from-signals --force-trade
"""

from __future__ import annotations

import argparse
import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any

from config import (
    COMPOUNDING,
    DCA_ADD_SIZE,
    DCA_BLOCK_DD,
    DCA_ENABLED,
    DCA_LEVELS,
    DCA_MAX_SIZE,
    ENTRY_MODE,
    EQUITY_USD,
    HOLD_DAYS,
    LEVERAGE,
    LIVE_DIR,
    LIVE_LEDGER,
    LIVE_STATE,
    LIVE_TRADE_UTC_END_HOUR,
    LIVE_TRADE_UTC_START_HOUR,
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


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def in_trade_window(force: bool = False, now: datetime | None = None) -> tuple[bool, str]:
    """
    True if exchange entries/exits are allowed.
    Window is [LIVE_TRADE_UTC_START_HOUR, LIVE_TRADE_UTC_END_HOUR) UTC.
    """
    if force or os.environ.get("LIVE_FORCE_TRADE", "").strip() in ("1", "true", "yes"):
        return True, "forced"
    n = now or _utc_now()
    h = n.hour
    start = int(LIVE_TRADE_UTC_START_HOUR)
    end = int(LIVE_TRADE_UTC_END_HOUR)
    if start <= h < end:
        return True, f"in_window {start:02d}:00-{end:02d}:00Z (now {h:02d}:{n.minute:02d}Z)"
    return (
        False,
        f"outside trade window {start:02d}:00-{end:02d}:00Z "
        f"(now {h:02d}:{n.minute:02d}Z) — use --force-trade to override",
    )


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
    state = json.loads(LIVE_STATE.read_text(encoding="utf-8"))
    # Keep start capital aligned with config (dashboard PnL = live_eq - equity_start)
    if float(state.get("equity_start") or 0) != float(EQUITY_USD):
        state["equity_start"] = float(EQUITY_USD)
        try:
            _save_state(state)
        except Exception:
            pass
    return state


def _save_state(state: dict[str, Any]) -> None:
    LIVE_DIR.mkdir(parents=True, exist_ok=True)
    LIVE_STATE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _append_ledger(event: dict[str, Any]) -> None:
    LIVE_DIR.mkdir(parents=True, exist_ok=True)
    with LIVE_LEDGER.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event) + "\n")


def _openish(state: dict[str, Any]) -> list[dict[str, Any]]:
    return [p for p in state.get("positions", []) if p.get("status") in ("pending", "open")]


def _bars_since_entry(pair: str, entry_date: str, ohlc: dict) -> tuple[int, float | None, str | None]:
    series = ohlc.get(pair) or []
    bars = [c for c in series if c.date >= entry_date]
    if not bars:
        return 0, None, None
    last = bars[-1]
    return len(bars), last.c, last.date


def _last_closed_date(series: list) -> str | None:
    """Last fully closed UTC daily bar (skip in-progress 'today' bar)."""
    if not series:
        return None
    today = _utc_now().strftime("%Y-%m-%d")
    last = series[-1]
    if last.date == today and len(series) >= 2:
        return series[-2].date
    return last.date


def _entry_bar_after_signal(pair: str, signal_date: str, ohlc: dict):
    """First daily bar strictly after signal_date (= next open for time-series)."""
    series = ohlc.get(pair) or []
    for c in series:
        if c.date > signal_date:
            return c
    return None


def _live_equity(broker: BybitBroker) -> float:
    return float(broker.get_equity_usdt())


def _update_equity_peak(state: dict[str, Any], equity: float) -> float:
    """Track peak equity for account DD (DCA block). Returns current DD (negative or 0)."""
    peak = float(state.get("equity_peak") or state.get("equity_start") or EQUITY_USD)
    peak = max(peak, equity)
    state["equity_peak"] = peak
    if peak <= 0:
        return 0.0
    return equity / peak - 1.0


def _init_dca_fields(pos: dict[str, Any]) -> None:
    """Ensure DCA tracking fields exist on a position."""
    if pos.get("dca_size_units") is None:
        pos["dca_size_units"] = 1.0
    if pos.get("dca_levels_hit") is None:
        pos["dca_levels_hit"] = []
    if pos.get("dca_legs") is None:
        legs = []
        if pos.get("entry_px") and pos.get("notional_usd"):
            legs.append({
                "leg": 0,
                "price": pos.get("entry_px"),
                "notional_usd": pos.get("notional_usd"),
                "qty": pos.get("qty"),
                "order_id": pos.get("entry_order_id"),
                "ts": pos.get("filled_at") or pos.get("created_at"),
            })
        pos["dca_legs"] = legs
    if pos.get("first_notional_usd") is None:
        pos["first_notional_usd"] = pos.get("notional_usd")
    if pos.get("first_entry_px") is None:
        pos["first_entry_px"] = pos.get("entry_px")


def _adverse_vs_first(pos: dict[str, Any], last_px: float, ohlc: dict) -> float:
    """
    Adverse move for short = price up vs first entry.
    Use max(live last, max Kraken daily high since entry_date) when available.
    """
    first = float(pos.get("first_entry_px") or pos.get("entry_px") or 0)
    if first <= 0:
        return 0.0
    high = last_px
    entry_date = pos.get("entry_date") or ""
    pair = pos.get("pair") or ""
    series = ohlc.get(pair) or []
    for c in series:
        if entry_date and c.date >= entry_date:
            high = max(high, float(c.h))
    return (high - first) / first


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
        _save_state(st)
        _append_ledger({"ts": _now(), "event": "kill_switch", "equity": eq, "reason": reason})
        return True, eq, reason

    return bool(st.get("killed")), eq, st.get("kill_reason")


def open_from_signals(
    signals: list[dict] | None = None,
    broker: BybitBroker | None = None,
    force_trade: bool = False,
) -> dict[str, Any]:
    """
    Rank signals by ret_3d, queue up to MAX_OPEN_POSITIONS.

    ENTRY_MODE=next_open  → status=pending only (fill later at next open)
    ENTRY_MODE=at_close   → market if signal is on last closed bar AND in trade window
    ENTRY_MODE=immediate  → market now (legacy)
    """
    br = broker or default_broker()
    state = _load_state()
    mode = (ENTRY_MODE or "next_open").strip().lower()
    summary: dict[str, Any] = {
        "queued": [],
        "opened": [],
        "skipped": [],
        "killed": False,
        "entry_mode": mode,
    }

    killed, equity, reason = check_kill(br, state)
    if killed:
        print(f"KILL active — no new opens ({reason})")
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

    ohlc, _ = load_or_refresh(refresh=False)
    openish = _openish(state)
    slots = max(0, MAX_OPEN_POSITIONS - len(openish))
    if slots == 0:
        print(f"Max open positions reached ({MAX_OPEN_POSITIONS}).")
        summary["skipped"].append({"reason": "max_positions"})
        return summary

    existing_pairs = {p["pair"] for p in openish}
    existing_symbols = {p.get("bybit_symbol") for p in openish if p.get("bybit_symbol")}
    try:
        for ep in br.list_open_positions():
            if ep.get("symbol"):
                existing_symbols.add(ep["symbol"])
    except Exception as e:
        print(f"  warn: list_open_positions failed: {e}")

    ranked = sorted(signals, key=lambda s: float(s.get("ret_3d") or 0.0), reverse=True)
    rule = active_rule()
    eq_for_size = _sizing_equity(br, state)
    notional = position_notional(eq_for_size)
    ok_win, win_msg = in_trade_window(force=force_trade)
    print(
        f"LIVE open_from_signals mode={mode} equity=${eq_for_size:.2f} "
        f"notional=${notional:.2f} slots={slots} signals={len(ranked)} | {win_msg}"
    )

    queued = 0
    opened = 0
    for s in ranked:
        if queued + opened >= slots:
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

        signal_date = s.get("signal_date")
        series = ohlc.get(pair) or []
        last_closed = _last_closed_date(series)

        # --- next_open: queue only ---
        if mode == "next_open":
            pos = {
                "id": str(uuid.uuid4())[:8],
                "pair": pair,
                "bybit_symbol": symbol,
                "status": "pending",
                "side": "short",
                "rule": rule.name,
                "signal_date": signal_date,
                "signal_close": s.get("close"),
                "ret_3d": s.get("ret_3d"),
                "rsi14": s.get("rsi14"),
                "vol_spike": s.get("vol_spike"),
                "dist_sma20": s.get("dist_sma20"),
                "notional_usd": notional,
                "qty": None,
                "leverage": LEVERAGE,
                "hold_days": int(s.get("hold_days") or HOLD_DAYS),
                "entry_date": None,
                "entry_px": None,
                "entry_order_id": None,
                "exit_date": None,
                "exit_px": None,
                "exit_order_id": None,
                "bars_held": 0,
                "realized_pnl_usd": None,
                "realized_pnl_pct": None,
                "created_at": _now(),
                "entry_mode": mode,
                "dca_size_units": 1.0,
                "dca_levels_hit": [],
                "dca_legs": [],
                "first_notional_usd": notional,
                "first_entry_px": None,
            }
            state["positions"].append(pos)
            existing_pairs.add(pair)
            existing_symbols.add(symbol)
            queued += 1
            summary["queued"].append({"id": pos["id"], "pair": pair, "symbol": symbol, "signal_date": signal_date})
            _append_ledger({"ts": _now(), "event": "signal_queued", "position": pos})
            print(f"  QUEUED pending short {pair}→{symbol} signal={signal_date} id={pos['id']}")
            continue

        # --- at_close / immediate: may place market ---
        if mode == "at_close":
            if not signal_date or not last_closed or signal_date != last_closed:
                summary["skipped"].append({
                    "pair": pair,
                    "reason": "stale_or_not_close_bar",
                    "signal_date": signal_date,
                    "last_closed": last_closed,
                })
                continue
            if not ok_win:
                summary["skipped"].append({"pair": pair, "reason": "outside_window", "detail": win_msg})
                continue

        if mode == "immediate" and not ok_win and not force_trade:
            # still allow queue as pending instead of silent skip
            summary["skipped"].append({"pair": pair, "reason": "outside_window_use_next_open_or_force", "detail": win_msg})
            continue

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
            _append_ledger({"ts": _now(), "event": "open_failed", "pair": pair, "symbol": symbol, "error": str(e)})
            continue

        entry_px = None
        try:
            entry_px = br.get_ticker_price(symbol)
        except Exception:
            entry_px = float(s.get("close") or 0) or None

        # at_close: entry_date = signal_date (close process)
        entry_date = signal_date if mode == "at_close" else _utc_now().strftime("%Y-%m-%d")
        pos = {
            "id": str(uuid.uuid4())[:8],
            "pair": pair,
            "bybit_symbol": symbol,
            "status": "open",
            "side": "short",
            "rule": rule.name,
            "signal_date": signal_date,
            "signal_close": s.get("close"),
            "ret_3d": s.get("ret_3d"),
            "rsi14": s.get("rsi14"),
            "vol_spike": s.get("vol_spike"),
            "dist_sma20": s.get("dist_sma20"),
            "notional_usd": notional,
            "qty": order.get("qty"),
            "leverage": LEVERAGE,
            "hold_days": int(s.get("hold_days") or HOLD_DAYS),
            "entry_date": entry_date,
            "entry_px": entry_px if mode != "at_close" else (s.get("close") or entry_px),
            "entry_order_id": order.get("orderId"),
            "exit_date": None,
            "exit_px": None,
            "exit_order_id": None,
            "bars_held": 0,
            "realized_pnl_usd": None,
            "realized_pnl_pct": None,
            "created_at": _now(),
            "entry_mode": mode,
            "dca_size_units": 1.0,
            "dca_levels_hit": [],
            "first_notional_usd": notional,
            "first_entry_px": entry_px if mode != "at_close" else (s.get("close") or entry_px),
            "dca_legs": [{
                "leg": 0,
                "price": entry_px if mode != "at_close" else (s.get("close") or entry_px),
                "notional_usd": notional,
                "qty": order.get("qty"),
                "order_id": order.get("orderId"),
                "ts": _now(),
            }],
        }
        state["positions"].append(pos)
        existing_pairs.add(pair)
        existing_symbols.add(symbol)
        opened += 1
        summary["opened"].append({"id": pos["id"], "pair": pair, "symbol": symbol, "notional": notional})
        _append_ledger({
            "ts": _now(),
            "event": "opened_short",
            "position": pos,
            "order": {k: order.get(k) for k in ("orderId", "qty", "notional_usd", "symbol", "side")},
        })
        print(
            f"  OPENED short {pair}→{symbol} id={pos['id']} "
            f"qty={pos['qty']} notional=${notional:.2f} orderId={order.get('orderId')}"
        )

    _save_state(state)
    if queued == 0 and opened == 0 and not summary["killed"]:
        print("No new live positions queued/opened.")
    summary["equity"] = equity
    summary["queued_n"] = queued
    summary["opened_n"] = opened
    return summary


def fill_opens(broker: BybitBroker | None = None, force_trade: bool = False) -> dict[str, Any]:
    """
    Fill pending shorts at next daily open (first OHLC bar after signal_date).
    Market order on Bybit only inside trade window (post close) unless forced.
    """
    br = broker or default_broker()
    state = _load_state()
    ohlc, _ = load_or_refresh(refresh=False)
    summary: dict[str, Any] = {"filled": [], "waiting": [], "skipped": []}

    ok_win, win_msg = in_trade_window(force=force_trade)
    print(f"LIVE fill_opens | {win_msg}")
    if not ok_win:
        n_pend = sum(1 for p in state.get("positions", []) if p.get("status") == "pending")
        print(f"  skip exchange fills ({n_pend} pending left).")
        summary["skipped"].append({"reason": "outside_window", "detail": win_msg, "pending": n_pend})
        # still report waiting reasons without trading
        for p in state.get("positions", []):
            if p.get("status") != "pending":
                continue
            bar = _entry_bar_after_signal(p["pair"], p.get("signal_date") or "", ohlc)
            if bar is None:
                summary["waiting"].append({"pair": p["pair"], "reason": "next_open_not_in_ohlc_yet"})
            else:
                summary["waiting"].append({
                    "pair": p["pair"],
                    "reason": "ready_but_outside_window",
                    "entry_date": bar.date,
                })
        return summary

    killed, equity, reason = check_kill(br, state)
    if killed:
        print(f"KILL — no fills ({reason})")
        summary["killed"] = True
        summary["kill_reason"] = reason
        return summary

    filled = 0
    for p in state.get("positions", []):
        if p.get("status") != "pending":
            continue
        pair = p["pair"]
        signal_date = p.get("signal_date")
        if not signal_date:
            summary["skipped"].append({"pair": pair, "reason": "no_signal_date"})
            continue

        entry_bar = _entry_bar_after_signal(pair, signal_date, ohlc)
        if entry_bar is None:
            print(f"  wait {pair}: next open after {signal_date} not in OHLC yet")
            summary["waiting"].append({"pair": pair, "signal_date": signal_date})
            continue

        symbol = p.get("bybit_symbol") or kraken_to_bybit_symbol(pair)
        eq_for_size = _sizing_equity(br, state)
        notional = float(p.get("notional_usd") or position_notional(eq_for_size))
        # refresh notional on fill if compounding
        if COMPOUNDING:
            notional = position_notional(eq_for_size)
            p["notional_usd"] = notional

        try:
            order = br.open_short(symbol, notional)
        except Exception as e:
            print(f"  FAIL fill {pair}→{symbol}: {e}")
            summary["skipped"].append({"pair": pair, "symbol": symbol, "reason": str(e)})
            _append_ledger({"ts": _now(), "event": "fill_failed", "pair": pair, "symbol": symbol, "error": str(e)})
            continue

        entry_px = float(entry_bar.o)  # theoretical next open
        try:
            # live fill is market; keep open as reference, store both
            mkt = br.get_ticker_price(symbol)
        except Exception:
            mkt = entry_px

        p["status"] = "open"
        p["entry_date"] = entry_bar.date
        p["entry_px"] = mkt
        p["entry_px_open_ref"] = entry_px
        p["qty"] = order.get("qty")
        p["entry_order_id"] = order.get("orderId")
        p["bars_held"] = 0
        p["filled_at"] = _now()
        p["first_entry_px"] = mkt
        p["first_notional_usd"] = notional
        p["dca_size_units"] = 1.0
        p["dca_levels_hit"] = []
        p["dca_legs"] = [{
            "leg": 0,
            "price": mkt,
            "notional_usd": notional,
            "qty": order.get("qty"),
            "order_id": order.get("orderId"),
            "ts": _now(),
        }]
        filled += 1
        summary["filled"].append({
            "id": p.get("id"),
            "pair": pair,
            "symbol": symbol,
            "entry_date": p["entry_date"],
            "entry_px": p["entry_px"],
            "orderId": order.get("orderId"),
        })
        _append_ledger({
            "ts": _now(),
            "event": "filled_short",
            "position": dict(p),
            "order": {k: order.get(k) for k in ("orderId", "qty", "notional_usd", "symbol", "side")},
        })
        print(
            f"  FILLED short {pair}→{symbol} entry_date={p['entry_date']} "
            f"open_ref={entry_px} mkt={mkt} qty={p['qty']} orderId={order.get('orderId')}"
        )

    _save_state(state)
    print(f"Filled {filled} pending → open.")
    summary["filled_n"] = filled
    summary["equity"] = equity
    return summary


def process_dca(broker: BybitBroker | None = None, force_trade: bool = False) -> dict[str, Any]:
    """
    Soft DCA: if open short is adverse by DCA_LEVELS vs first entry, add short size
    up to DCA_MAX_SIZE units. Blocked when account DD from peak >= DCA_BLOCK_DD.
    """
    br = broker or default_broker()
    state = _load_state()
    summary: dict[str, Any] = {
        "added": [],
        "skipped": [],
        "blocked_dd": 0,
        "enabled": bool(DCA_ENABLED),
    }

    if not DCA_ENABLED:
        print("DCA disabled (DCA_ENABLED=False)")
        return summary

    ok_win, win_msg = in_trade_window(force=force_trade)
    print(f"LIVE process_dca | {win_msg}")
    if not ok_win:
        summary["skipped"].append({"reason": "outside_window", "detail": win_msg})
        print(f"  skip DCA exchange orders ({win_msg})")
        return summary

    killed, equity, reason = check_kill(br, state)
    if killed:
        print(f"KILL — no DCA ({reason})")
        summary["killed"] = True
        summary["kill_reason"] = reason
        return summary

    dd = _update_equity_peak(state, equity)
    summary["equity"] = equity
    summary["equity_dd"] = dd
    print(f"  equity=${equity:.4f} peak=${state.get('equity_peak'):.4f} dd={dd*100:+.2f}%")

    ohlc, _ = load_or_refresh(refresh=False)
    today = _utc_now().strftime("%Y-%m-%d")
    levels = tuple(float(x) for x in DCA_LEVELS)
    added_n = 0

    for p in state.get("positions", []):
        if p.get("status") != "open":
            continue
        _init_dca_fields(p)
        pair = p.get("pair") or ""
        symbol = p.get("bybit_symbol") or kraken_to_bybit_symbol(pair)
        entry_date = p.get("entry_date") or ""

        # no DCA on entry day (same as backtest)
        if entry_date and entry_date >= today:
            summary["skipped"].append({"pair": pair, "reason": "entry_day"})
            continue

        size_u = float(p.get("dca_size_units") or 1.0)
        if size_u >= float(DCA_MAX_SIZE) - 1e-9:
            summary["skipped"].append({"pair": pair, "reason": "max_size", "size": size_u})
            continue

        first_px = float(p.get("first_entry_px") or p.get("entry_px") or 0)
        first_notional = float(p.get("first_notional_usd") or p.get("notional_usd") or 0)
        if first_px <= 0 or first_notional <= 0:
            summary["skipped"].append({"pair": pair, "reason": "missing_first_px_or_notional"})
            continue

        try:
            last_px = float(br.get_ticker_price(symbol))
        except Exception as e:
            summary["skipped"].append({"pair": pair, "reason": f"ticker_fail: {e}"})
            continue

        adverse = _adverse_vs_first(p, last_px, ohlc)
        hit = set(int(x) for x in (p.get("dca_levels_hit") or []))

        # account DD block
        if DCA_BLOCK_DD is not None and dd <= -abs(float(DCA_BLOCK_DD)):
            if any(adverse >= thr and i not in hit for i, thr in enumerate(levels)):
                summary["blocked_dd"] += 1
                summary["skipped"].append({
                    "pair": pair,
                    "reason": "account_dd_block",
                    "dd": dd,
                    "adverse": adverse,
                })
                print(f"  BLOCK DCA {pair}: account dd={dd*100:+.1f}% >= {float(DCA_BLOCK_DD)*100:.0f}%")
            continue

        for li, thr in enumerate(levels):
            if li in hit:
                continue
            size_u = float(p.get("dca_size_units") or 1.0)
            if size_u >= float(DCA_MAX_SIZE) - 1e-9:
                break
            if adverse < thr:
                continue

            add_units = min(float(DCA_ADD_SIZE), float(DCA_MAX_SIZE) - size_u)
            if add_units <= 0:
                break
            add_notional = first_notional * add_units
            if add_notional < 1.0:
                summary["skipped"].append({
                    "pair": pair,
                    "reason": "add_notional_too_small",
                    "notional": add_notional,
                })
                break

            try:
                order = br.open_short(symbol, add_notional)
            except Exception as e:
                print(f"  FAIL DCA {pair}→{symbol} +{thr*100:.0f}%: {e}")
                summary["skipped"].append({"pair": pair, "reason": f"order_fail: {e}"})
                _append_ledger({
                    "ts": _now(),
                    "event": "dca_failed",
                    "pair": pair,
                    "symbol": symbol,
                    "threshold": thr,
                    "error": str(e),
                })
                break

            try:
                fill_px = float(br.get_ticker_price(symbol))
            except Exception:
                fill_px = last_px

            new_size = size_u + add_units
            p["dca_size_units"] = new_size
            hit.add(li)
            p["dca_levels_hit"] = sorted(hit)
            legs = list(p.get("dca_legs") or [])
            legs.append({
                "leg": len(legs),
                "threshold": thr,
                "price": fill_px,
                "notional_usd": add_notional,
                "qty": order.get("qty"),
                "order_id": order.get("orderId"),
                "ts": _now(),
            })
            p["dca_legs"] = legs
            # total notional deployed (sum of legs) for display
            p["notional_usd_total"] = sum(float(x.get("notional_usd") or 0) for x in legs)
            p["last_dca_at"] = _now()
            p["last_dca_threshold"] = thr
            added_n += 1
            summary["added"].append({
                "id": p.get("id"),
                "pair": pair,
                "symbol": symbol,
                "threshold": thr,
                "adverse": adverse,
                "add_notional": add_notional,
                "size_units": new_size,
                "orderId": order.get("orderId"),
            })
            _append_ledger({
                "ts": _now(),
                "event": "dca_add",
                "pair": pair,
                "symbol": symbol,
                "threshold": thr,
                "adverse": adverse,
                "equity_dd": dd,
                "add_notional": add_notional,
                "size_units": new_size,
                "order": {k: order.get(k) for k in ("orderId", "qty", "notional_usd", "symbol", "side")},
                "position_id": p.get("id"),
            })
            print(
                f"  DCA ADD {pair}→{symbol} thr=+{thr*100:.0f}% adverse={adverse*100:+.1f}% "
                f"notional=${add_notional:.2f} size={new_size:.2f}x orderId={order.get('orderId')}"
            )

    _save_state(state)
    summary["added_n"] = added_n
    print(f"DCA done added={added_n} blocked_dd={summary['blocked_dd']}")
    return summary


def close_due(broker: BybitBroker | None = None, force_trade: bool = False) -> dict[str, Any]:
    """Close shorts when bars_held >= hold_days. Exchange close only in trade window."""
    br = broker or default_broker()
    state = _load_state()
    ohlc, _ = load_or_refresh(refresh=False)
    closed = 0
    summary: dict[str, Any] = {"closed": [], "held": [], "deferred": []}

    ok_win, win_msg = in_trade_window(force=force_trade)
    print(f"LIVE close_due | {win_msg}")

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

        if not ok_win:
            summary["deferred"].append({"pair": pair, "bars": n, "hold": hold, "reason": win_msg})
            print(f"  defer close {pair}: bars={n}/{hold} but {win_msg}")
            continue

        symbol = p.get("bybit_symbol") or kraken_to_bybit_symbol(pair)
        qty = p.get("qty")
        try:
            order = br.close_short(symbol, qty=qty)
        except Exception as e:
            try:
                order = br.close_short(symbol, qty=None)
            except Exception as e2:
                print(f"  FAIL close {pair}→{symbol}: {e2}")
                _append_ledger({
                    "ts": _now(),
                    "event": "close_failed",
                    "pair": pair,
                    "symbol": symbol,
                    "error": str(e2),
                    "prev_error": str(e),
                })
                continue

        # Prefer hold_days-th bar close for bookkeeping
        series = ohlc.get(pair) or []
        bars = [c for c in series if c.date >= entry_date]
        exit_bar = bars[hold - 1] if len(bars) >= hold else (bars[-1] if bars else None)
        exit_px_ref = exit_bar.c if exit_bar else last_c
        exit_date = exit_bar.date if exit_bar else last_d

        try:
            exit_px = br.get_ticker_price(symbol)
        except Exception:
            exit_px = exit_px_ref

        entry_px = p.get("entry_px")
        net = None
        pnl_usd = None
        if entry_px and exit_px and float(entry_px) > 0:
            gross = (float(entry_px) - float(exit_px)) / float(entry_px)
            net = gross
            notional = float(p.get("notional_usd") or 0)
            pnl_usd = net * notional

        p["status"] = "closed"
        p["exit_date"] = exit_date or _utc_now().strftime("%Y-%m-%d")
        p["exit_px"] = exit_px
        p["exit_px_close_ref"] = exit_px_ref
        p["exit_order_id"] = order.get("orderId")
        p["realized_pnl_pct"] = net
        p["realized_pnl_usd"] = pnl_usd
        p["exit_reason"] = "time_exit"
        if pnl_usd is not None:
            state["cash_pnl"] = float(state.get("cash_pnl") or 0.0) + pnl_usd
        closed += 1
        summary["closed"].append({
            "pair": pair,
            "symbol": symbol,
            "pnl_usd": pnl_usd,
            "orderId": order.get("orderId"),
        })
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
    summary["window_ok"] = ok_win
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

    ok_win, win_msg = in_trade_window(force=False)
    print("=" * 72)
    print(f"LIVE BOOK  {profile_summary()}")
    print(f"equity_start=${state.get('equity_start', EQUITY_USD):.2f}  "
          f"est_realized_pnl=${state.get('cash_pnl', 0):+.2f}")
    if equity is not None:
        print(f"live_equity_usdt=${equity:.4f}  MIN_EQUITY=${MIN_EQUITY_USD:.2f}")
    print(f"trade_window: {'OPEN' if ok_win else 'CLOSED'} — {win_msg}")
    if state.get("killed"):
        print(f"*** KILLED *** {state.get('kill_reason')}")
    if err:
        print(f"broker: ERROR {err}")
    print(f"Rule: {active_rule().describe()}")
    print("=" * 72)

    by: dict[str, list] = {"pending": [], "open": [], "closed": []}
    for p in state.get("positions", []):
        by.setdefault(p.get("status") or "?", []).append(p)

    print(f"\n[PENDING] n={len(by.get('pending') or [])}  (signaled, wait next open + window)")
    for p in (by.get("pending") or [])[-20:]:
        print(
            f"  {p.get('id')} {p.get('pair'):<12} → {p.get('bybit_symbol')}  "
            f"signal={p.get('signal_date')} ret3d={p.get('ret_3d')}"
        )

    print(f"\n[OPEN] n={len(by.get('open') or [])}")
    for p in (by.get("open") or [])[-20:]:
        _init_dca_fields(p)
        dca_u = p.get("dca_size_units", 1.0)
        hit = p.get("dca_levels_hit") or []
        print(
            f"  {p.get('id')} {p.get('pair'):<12} → {p.get('bybit_symbol')}  "
            f"entry={p.get('entry_date')} @ {p.get('entry_px')}  "
            f"bars={p.get('bars_held')}/{p.get('hold_days')}  "
            f"notional1=${p.get('first_notional_usd') or p.get('notional_usd')} "
            f"dca={dca_u}x hit={hit} qty={p.get('qty')}"
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
    p_close = sub.add_parser("close-due")
    p_close.add_argument("--force-trade", action="store_true", help="Ignore UTC trade window")
    p_fill = sub.add_parser("fill-opens")
    p_fill.add_argument("--force-trade", action="store_true")
    p_dca = sub.add_parser("process-dca", help="Soft DCA adds on adverse shorts")
    p_dca.add_argument("--force-trade", action="store_true")
    p_open = sub.add_parser("open-from-signals")
    p_open.add_argument("--force-trade", action="store_true")
    p_reset = sub.add_parser("reset")
    p_reset.add_argument("--force", action="store_true")
    args = ap.parse_args()

    if args.cmd == "status":
        status()
    elif args.cmd == "close-due":
        close_due(force_trade=args.force_trade)
    elif args.cmd == "fill-opens":
        fill_opens(force_trade=args.force_trade)
    elif args.cmd == "process-dca":
        process_dca(force_trade=args.force_trade)
    elif args.cmd == "open-from-signals":
        open_from_signals(force_trade=args.force_trade)
    elif args.cmd == "reset":
        reset(force=args.force)


if __name__ == "__main__":
    main()
