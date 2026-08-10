#!/usr/bin/env python3
"""Profil A: L2, risk 3%, max 3, cross — perfs annualisées."""

from __future__ import annotations

import statistics

from backtest_fade import RuleSet, load_or_fetch, run_rule
from cross_margin_stats import sim_cross
from kraken_data import load_or_refresh


def pct(x: float | None) -> str:
    if x is None:
        return "n/a"
    return f"{x * 100:+.2f}%"


def main() -> None:
    ohlc_bt = load_or_fetch(refresh=False)
    ohlc_map, _ = load_or_refresh(refresh=False)
    rule = RuleSet(
        name="G",
        description="x",
        pump_min=0.40,
        min_rsi=70,
        min_vol_spike=3,
        min_dist_sma20=0.20,
        hold_days=3,
        take_profit=None,
        stop_loss=None,
        min_vol_usd=10_000,
    )
    trades = run_rule(ohlc_bt, rule)
    first = min(t.entry_date for t in trades)
    last = max(t.exit_date for t in trades)
    # ~23 months sample
    months = 23.0

    print("=" * 80)
    print("PROFIL A — full auto survivable")
    print("  $100 | CROSS | leverage 2x | risk/marge 3%/trade | max 3 pos")
    print("  notional/trade = 6% equity ($6) | gross max = 18% equity")
    print(f"  data window: {first} → {last} (~{months:.0f}m)")
    print("=" * 80)

    for mark in ("high", "close"):
        r = sim_cross(
            trades,
            ohlc_map,
            leverage=2,
            risk_pct=0.03,
            adverse_ref=1.0,
            max_open=3,
            start_eq=100.0,
            max_margin_pct=0.03,
            mark_mode=mark,
        )
        cagr = (r["end"] / 100.0) ** (12.0 / months) - 1.0 if r["end"] > 0 else None
        rets = [v for v in (r.get("yearly") or {}).values() if v is not None]
        geo = None
        if rets:
            g = 1.0
            for x in rets:
                g *= 1 + x
            geo = g ** (1 / len(rets)) - 1

        print(f"\n## Mark = {mark} (stress {'max' if mark=='high' else 'modéré'})")
        print(f"  Total fenêtre     : {pct(r['total'])}   ($100 → ${r['end']:.2f})")
        print(f"  CAGR ~annualisé   : {pct(cagr)}   (sur {months:.0f} mois)")
        print(f"  MDD               : {pct(r['mdd'])}")
        print(f"  Wipe / acct liq   : {r.get('wiped')} / {r.get('account_liqs')}")
        print(f"  Trades pris       : {r.get('taken')}  (skip max-open: {r.get('skipped')})")
        print("  Par année calendaire:")
        for y, ret in sorted((r.get("yearly") or {}).items()):
            print(f"    {y}: {pct(ret)}")
        if geo is not None:
            print(f"  Moy. géom. des années présentes : {pct(geo)}")
        if rets:
            print(f"  Moy. arith. des années présentes: {pct(statistics.mean(rets))}")
        # best anchor year
        y25 = (r.get("yearly") or {}).get("2025")
        print(f"  Ancre année pleine 2025 : {pct(y25)}")

    print("\n## Sensibilité (mark=high)")
    print(f"  {'setup':<24} {'Total':>10} {'CAGR~':>10} {'MDD':>9} {'Y2025':>9} {'End':>8}")
    variants = [
        (2, 0.03, 3, "A (base)"),
        (2, 0.05, 3, "A risk 5%"),
        (2, 0.03, 5, "A max 5 pos"),
        (1, 0.03, 3, "A en 1x"),
        (3, 0.03, 3, "A en 3x"),
        (2, 0.02, 3, "A risk 2%"),
    ]
    for L, risk, mx, lab in variants:
        r = sim_cross(
            trades,
            ohlc_map,
            leverage=L,
            risk_pct=risk,
            adverse_ref=1.0,
            max_open=mx,
            start_eq=100.0,
            max_margin_pct=risk,
            mark_mode="high",
        )
        cagr = (r["end"] / 100.0) ** (12.0 / months) - 1.0 if r["end"] > 0 else None
        print(
            f"  {lab:<24} {pct(r['total']):>10} {pct(cagr):>10} {pct(r['mdd']):>9} "
            f"{pct(r.get('y2025')):>9} {r['end']:>8.1f}"
        )

    print("\n## Synthèse honnête Profil A")
    r = sim_cross(
        trades, ohlc_map, leverage=2, risk_pct=0.03, adverse_ref=1.0,
        max_open=3, start_eq=100, max_margin_pct=0.03, mark_mode="high",
    )
    y25 = (r.get("yearly") or {}).get("2025")
    print(f"  Fourchette annualisée réaliste: ~{pct(y25)} (année 2025) à ~+15–25% selon mark/CAGR")
    print("  Ce n'est PAS du +100%/an — c'est un profil de survie avec edge modeste amplifié 2x.")


if __name__ == "__main__":
    main()
