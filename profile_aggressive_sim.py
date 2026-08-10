#!/usr/bin/env python3
"""
Simu profil user:
  bankroll $100, risk 5%/trade, cross, max 5 ou 7 pos, leverage 10x
"""

from __future__ import annotations

import statistics
from collections import defaultdict

from backtest_fade import RuleSet, load_or_fetch, run_rule
from cross_margin_stats import sim_cross, sim_isolated
from kraken_data import load_or_refresh


def pct(x):
    if x is None:
        return "n/a"
    return f"{x*100:+.2f}%"


def main():
    ohlc_bt = load_or_fetch(refresh=False)
    ohlc_map, _ = load_or_refresh(refresh=False)
    rule = RuleSet(
        name="G",
        description="blowoff",
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
    print(f"Universe trades={len(trades)} MAE_mean={statistics.mean(t.mae for t in trades)*100:.1f}%")
    print()
    print("PROFIL CIBLE: $100 | risk 5%/trade | CROSS | max 5–7 pos | 10x")
    print("Sizing A: margin = risk% * equity (=5%), notional = margin * L  → 50% equity notional/trade")
    print("Sizing B: margin = risk%/0.40 * equity (ancien), cap 50%   → plus gros")
    print("=" * 100)

    configs = []
    # Sizing A: classic "risk 5% = margin 5% at this leverage framing"
    # For cross sim: risk_pct/adverse_ref = margin fraction
    # We want margin_frac = 0.05 → set risk_pct=0.05, adverse_ref=1.0
    # notional = 0.05 * L = 0.50
    for max_open in (5, 7):
        for L in (10, 5, 3, 2, 1):
            configs.append({
                "label": f"A margin5% L{L}x max{max_open}",
                "L": L,
                "max_open": max_open,
                "risk_pct": 0.05,
                "adverse_ref": 1.0,  # margin = 5% equity
                "max_margin_pct": 0.05,
            })

    # Sizing B: risk 5% of account if 40% adverse on notional: notional = 5%/40% = 12.5%, *10x margin would be wrong
    # Better: notional_frac = risk/adverse = 0.05/0.40 = 0.125, margin = notional/L
    # In our sim: notional = (risk/adverse)*L * equity with adverse=0.4, risk=0.05 → notional = 0.125*L
    # For L=10 notional = 125% equity per trade — insane. Cap max_margin at 0.05 still.

    print(f"\n{'Label':<28} {'Not~/tr':>8} {'Total':>10} {'MDD':>9} {'Y25':>9} {'Y26':>9} "
          f"{'Wipe':>5} {'End$':>8} {'#posLiq':>8}")
    print("-" * 110)

    for c in configs:
        L = c["L"]
        notional_frac = min(c["risk_pct"] / c["adverse_ref"], c["max_margin_pct"]) * L
        iso = sim_isolated(
            trades,
            leverage=L,
            risk_pct=c["risk_pct"],
            adverse_ref=c["adverse_ref"],
            max_open=c["max_open"],
            start_eq=100.0,
            max_margin_pct=c["max_margin_pct"],
        )
        crx = sim_cross(
            trades,
            ohlc_map,
            leverage=L,
            risk_pct=c["risk_pct"],
            adverse_ref=c["adverse_ref"],
            max_open=c["max_open"],
            start_eq=100.0,
            max_margin_pct=c["max_margin_pct"],
            mark_mode="high",
        )
        wipe = "YES" if crx.get("wiped") else "no"
        print(
            f"{c['label']:<28} {notional_frac*100:>7.1f}% "
            f"{pct(crx['total']):>10} {pct(crx['mdd']):>9} {pct(crx.get('y2025')):>9} {pct(crx.get('y2026')):>9} "
            f"{wipe:>5} {crx['end']:>8.1f} isoLiq={iso['liqs']}"
        )

    # Explicit user profile variations
    print("\n" + "=" * 100)
    print("PROFIL USER STRICT: risk5% margin, L=10, cross, max5 et max7")
    for max_open in (5, 7):
        crx_h = sim_cross(
            trades, ohlc_map, leverage=10, risk_pct=0.05, adverse_ref=1.0,
            max_open=max_open, start_eq=100, max_margin_pct=0.05, mark_mode="high",
        )
        crx_c = sim_cross(
            trades, ohlc_map, leverage=10, risk_pct=0.05, adverse_ref=1.0,
            max_open=max_open, start_eq=100, max_margin_pct=0.05, mark_mode="close",
        )
        iso = sim_isolated(
            trades, leverage=10, risk_pct=0.05, adverse_ref=1.0,
            max_open=max_open, start_eq=100, max_margin_pct=0.05,
        )
        print(f"\n--- max_open={max_open}, notional/trade=50% equity, up to {max_open*50}% gross exposure ---")
        print(f"  CROSS mark=HIGH : total={pct(crx_h['total'])} MDD={pct(crx_h['mdd'])} "
              f"Y25={pct(crx_h.get('y2025'))} Y26={pct(crx_h.get('y2026'))} "
              f"wipe={crx_h.get('wiped')} end=${crx_h['end']:.2f} acct_liq={crx_h['account_liqs']}")
        print(f"  CROSS mark=CLOSE: total={pct(crx_c['total'])} MDD={pct(crx_c['mdd'])} "
              f"Y25={pct(crx_c.get('y2025'))} wipe={crx_c.get('wiped')} end=${crx_c['end']:.2f}")
        print(f"  ISOLATED        : total={pct(iso['total'])} MDD={pct(iso['mdd'])} "
              f"pos_liqs={iso['liqs']}/{iso['taken']} end=${iso['end']:.2f}")

    # Safer neighbors for same "full auto" spirit
    print("\n" + "=" * 100)
    print("ALTERNATIVES plus viables (même esprit auto, moins kamikaze)")
    alts = [
        (10, 0.02, 1.0, 0.02, 5, "L10 risk2% max5"),
        (5, 0.05, 1.0, 0.05, 5, "L5 risk5% max5"),
        (3, 0.05, 1.0, 0.05, 5, "L3 risk5% max5"),
        (2, 0.05, 1.0, 0.05, 5, "L2 risk5% max5"),
        (5, 0.03, 1.0, 0.03, 5, "L5 risk3% max5"),
        (10, 0.05, 1.0, 0.05, 3, "L10 risk5% max3"),
    ]
    print(f"  {'setup':<22} {'not~/tr':>8} {'Total':>10} {'MDD':>9} {'Y25':>9} {'wipe':>5} {'end':>8}")
    for L, risk, adv, cap, mx, label in alts:
        r = sim_cross(
            trades, ohlc_map, leverage=L, risk_pct=risk, adverse_ref=adv,
            max_open=mx, start_eq=100, max_margin_pct=cap, mark_mode="high",
        )
        nf = min(risk / adv, cap) * L
        print(
            f"  {label:<22} {nf*100:>7.1f}% {pct(r['total']):>10} {pct(r['mdd']):>9} "
            f"{pct(r.get('y2025')):>9} {'YES' if r.get('wiped') else 'no':>5} {r['end']:>8.1f}"
        )

    print("\n## Math du profil user")
    print("  equity $100")
    print("  margin/trade = 5% = $5")
    print("  notional/trade @10x = $50")
    print("  5 positions = $250 notional (2.5x le compte)")
    print("  7 positions = $350 notional (3.5x le compte)")
    print("  Liq approx unitaire ~10% de hausse contre le short")
    print("  MAE moyen historique setup = 35% → très au-dessus du liq 10%")
    print("  En isolated: ~70% des trades liquidés")
    print("  En cross: tu survis les wicks unitaires tant que le PORTEFEUILLE tient")
    print("  5–7 shorts shitcoin corrélés @ 50% notional chacun = risque de wipe élevé hors sample")


if __name__ == "__main__":
    main()
