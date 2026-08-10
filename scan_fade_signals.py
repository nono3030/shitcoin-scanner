#!/usr/bin/env python3
"""
Daily scanner — FADE-BLOWOFF-T3 setups.

Signal on last *completed* daily bar when possible:
  - If last candle is still "today" UTC and incomplete, use previous bar as signal day.
  - Entry instruction: SHORT next daily open after signal.

Usage:
  python scan_fade_signals.py              # use cache (fast)
  python scan_fade_signals.py --refresh    # full re-download
  python scan_fade_signals.py --near       # also show near-misses (almost signals)
  python scan_fade_signals.py --paper-open # write paper entries for confirmed signals
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from typing import Any

from config import (
    FEE_RT,
    MAX_OPEN_POSITIONS,
    OUT_DIR,
    PAPER_EQUITY_USD,
    SIGNALS_FILE,
    active_rule,
    position_notional,
)
from features import features_at
from kraken_data import Candle, load_or_refresh


def signal_index(candles: list[Candle]) -> int:
    """
    Prefer last fully closed UTC day.
    If the last bar timestamp date == today UTC, treat it as in-progress and use i-1.
    """
    if len(candles) < 30:
        return -1
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    last = candles[-1]
    if last.date == today and len(candles) >= 2:
        return len(candles) - 2
    return len(candles) - 1


def passes(f: dict[str, float | None], rule) -> tuple[bool, list[str]]:
    fails = []
    ret = f.get(f"ret_{rule.pump_lookback}d")
    if ret is None or ret < rule.pump_min:
        fails.append(f"ret_{rule.pump_lookback}d<{rule.pump_min*100:.0f}%")
    rsi = f.get("rsi14")
    if rsi is None or rsi < rule.min_rsi:
        fails.append(f"RSI<{rule.min_rsi:.0f}")
    vs = f.get("vol_spike")
    if vs is None or vs < rule.min_vol_spike:
        fails.append(f"vol<{rule.min_vol_spike}x")
    dist = f.get("dist_sma20")
    if dist is None or dist < rule.min_dist_sma20:
        fails.append(f"distSMA20<{rule.min_dist_sma20*100:.0f}%")
    liq = f.get("avg_vol_usd_10d") or 0.0
    if liq < rule.min_avg_vol_usd_10d:
        fails.append(f"liq10d<{rule.min_avg_vol_usd_10d:.0f}")
    return (len(fails) == 0, fails)


def near_score(f: dict[str, float | None], rule) -> float:
    """0..1 how close to full signal (for near-miss list)."""
    parts = []
    ret = f.get(f"ret_{rule.pump_lookback}d")
    parts.append(min(1.0, (ret or 0) / rule.pump_min) if rule.pump_min else 0)
    rsi = f.get("rsi14")
    parts.append(min(1.0, (rsi or 0) / rule.min_rsi) if rule.min_rsi else 0)
    vs = f.get("vol_spike")
    parts.append(min(1.0, (vs or 0) / rule.min_vol_spike) if rule.min_vol_spike else 0)
    dist = f.get("dist_sma20")
    parts.append(min(1.0, (dist or 0) / rule.min_dist_sma20) if rule.min_dist_sma20 else 0)
    liq = f.get("avg_vol_usd_10d") or 0
    parts.append(min(1.0, liq / rule.min_avg_vol_usd_10d) if rule.min_avg_vol_usd_10d else 0)
    return sum(parts) / len(parts)


def pct(x: float | None) -> str:
    if x is None:
        return "n/a"
    return f"{x*100:+.1f}%"


def num(x: float | None, d: int = 2) -> str:
    if x is None:
        return "n/a"
    return f"{x:.{d}f}"


def scan(ohlc: dict[str, list[Candle]]) -> tuple[list[dict], list[dict]]:
    rule = active_rule()
    signals = []
    near = []
    notional = position_notional(PAPER_EQUITY_USD)

    for ws, candles in ohlc.items():
        i = signal_index(candles)
        if i < 25:
            continue
        f = features_at(candles, i)
        ok, fails = passes(f, rule)
        row = {
            "pair": ws,
            "rule": rule.name,
            "signal_date": candles[i].date,
            "signal_index": i,
            "close": f["close"],
            "ret_1d": f.get("ret_1d"),
            "ret_3d": f.get("ret_3d"),
            "ret_7d": f.get("ret_7d"),
            "rsi14": f.get("rsi14"),
            "vol_spike": f.get("vol_spike"),
            "dist_sma20": f.get("dist_sma20"),
            "avg_vol_usd_10d": f.get("avg_vol_usd_10d"),
            "action": "SHORT_NEXT_OPEN",
            "hold_days": rule.hold_days,
            "exit_rule": f"close after {rule.hold_days} daily bars from entry (time exit only)",
            "suggested_notional_usd": round(notional, 2),
            "assumed_fee_rt": FEE_RT,
            "fails": fails,
            "near_score": round(near_score(f, rule), 3),
        }
        if ok:
            signals.append(row)
        else:
            # keep only competitive near-misses
            if near_score(f, rule) >= 0.75 and (f.get("ret_3d") or 0) >= 0.20:
                near.append(row)

    signals.sort(key=lambda r: (r.get("ret_3d") or 0), reverse=True)
    near.sort(key=lambda r: r["near_score"], reverse=True)
    return signals, near


def print_report(signals: list[dict], near: list[dict], show_near: bool) -> None:
    rule = active_rule()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    notional = position_notional(PAPER_EQUITY_USD)

    print("=" * 96)
    print(f"FADE SCANNER  |  {now}")
    print(rule.describe())
    print(
        f"Paper equity=${PAPER_EQUITY_USD:.0f}  notional/trade=${notional:.2f}  "
        f"max_open={MAX_OPEN_POSITIONS}  fee_rt={FEE_RT*100:.2f}%"
    )
    print("=" * 96)

    if not signals:
        print("\nAucun signal FADE confirmé aujourd'hui.")
    else:
        print(f"\n✅ SIGNALS CONFIRMÉS ({len(signals)}) — short next open, hold {rule.hold_days}d time-exit\n")
        hdr = (
            f"{'#':>2} {'Pair':<14} {'SigDate':<12} {'Close':>12} {'3d':>8} {'RSI':>6} "
            f"{'VolX':>6} {'vsSMA20':>8} {'Liq10d$':>10} {'Notional$':>10}"
        )
        print(hdr)
        print("-" * len(hdr))
        for i, s in enumerate(signals, 1):
            print(
                f"{i:>2} {s['pair']:<14} {s['signal_date']:<12} {s['close']:>12.6g} "
                f"{pct(s['ret_3d']):>8} {num(s['rsi14'],1):>6} {num(s['vol_spike'],1):>6} "
                f"{pct(s['dist_sma20']):>8} {s['avg_vol_usd_10d']:>10,.0f} "
                f"{s['suggested_notional_usd']:>10.2f}"
            )
        print("\nChecklist exécution:")
        print("  1. Attendre l'open daily suivant le signal_date (UTC)")
        print("  2. Short market/limit près de l'open")
        print("  3. Ne PAS placer de SL serré (edge backtest = time exit)")
        print(f"  4. Exit au close après {rule.hold_days} bougies daily d'holding")
        print("  5. Cap corrélation: max", MAX_OPEN_POSITIONS, "positions ouvertes")

    if show_near:
        print(f"\n⚠️  NEAR-MISSES (score>=0.75, ret3d>=20%) — top 15 / {len(near)}")
        for i, s in enumerate(near[:15], 1):
            print(
                f"  {i:>2}. {s['pair']:<14} score={s['near_score']:.2f}  3d={pct(s['ret_3d'])}  "
                f"RSI={num(s['rsi14'],1)}  vol={num(s['vol_spike'],1)}  "
                f"fails={','.join(s['fails'])}"
            )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true", help="Re-download full OHLC universe")
    ap.add_argument("--near", action="store_true", help="Show near-miss candidates")
    ap.add_argument("--paper-open", action="store_true", help="Open paper positions for signals")
    args = ap.parse_args()

    ohlc, _pairs = load_or_refresh(refresh=args.refresh)
    signals, near = scan(ohlc)
    print_report(signals, near, show_near=args.near or not signals)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "rule": active_rule().describe(),
        "paper_equity_usd": PAPER_EQUITY_USD,
        "notional_per_trade_usd": position_notional(PAPER_EQUITY_USD),
        "signals": signals,
        "near_misses": near[:30],
    }
    SIGNALS_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nSaved → {SIGNALS_FILE}")

    if args.paper_open:
        from paper_book import open_from_signals

        open_from_signals(signals)
        print("Paper positions updated.")


if __name__ == "__main__":
    main()
