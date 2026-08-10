#!/usr/bin/env python3
"""
Impact du levier sur FADE-BLOWOFF-T3 (time exit 3d).

Modèle:
  - margin_frac = risk_pct / adverse_ref   (ex 0.5%/40% = 1.25% equity en marge)
  - notional = margin * leverage
  - pnl_usd = net_pnl * notional
  - liquidation short si high adverse >= (1/L - buffer) pendant le trade
    (approx daily: MAE du trade >= liq_threshold)
  - funding optionnel: coût journalier * hold_days * notional (short perps souvent + ou -)

  python leverage_stats.py
"""

from __future__ import annotations

import statistics
from collections import defaultdict

from backtest_fade import FEE_RATE, RuleSet, load_or_fetch, run_rule


def pct(x: float | None) -> str:
    if x is None:
        return "n/a"
    return f"{x * 100:+.2f}%"


def sim(
    trades,
    leverage: float = 1.0,
    start_eq: float = 100.0,
    risk_pct: float = 0.005,
    adverse_ref: float = 0.40,
    max_open: int = 3,
    max_margin_pct: float = 0.10,
    liq_buffer: float = 0.05,
    funding_per_day: float = 0.0,
    compound: bool = True,
):
    """
    liq_buffer: liquidate if adverse >= (1/L)*(1-liq_buffer) roughly maintenance.
    For L=5, 1/L=20%; with buffer 5% of that → ~19%. We use:
      liq_level = max(0.05, 1/leverage - 0.02)  # 2pp cushion
    """
    if leverage < 1:
        leverage = 1.0

    liq_level = max(0.05, (1.0 / leverage) - 0.02) if leverage > 1 else 9.0  # no liq at 1x spot-like

    events = []
    for t in trades:
        events.append((t.exit_date, 0, "exit", t))
        events.append((t.entry_date, 1, "entry", t))
    events.sort(key=lambda x: (x[0], x[1]))

    equity = start_eq
    peak = start_eq
    mdd = 0.0
    open_ids: dict[int, dict] = {}
    open_count = 0
    max_concurrent = 0
    skipped = 0
    liquidations = 0
    trade_pnls: list[float] = []
    year_start: dict[str, float] = {}
    year_end: dict[str, float] = {}
    n_taken = 0

    for day, _, kind, t in events:
        y = day[:4]
        if y not in year_start:
            year_start[y] = equity

        if kind == "exit":
            tid = id(t)
            if tid not in open_ids:
                continue
            meta = open_ids.pop(tid)
            open_count -= 1
            notional = meta["notional"]

            # liquidation check via MAE (worst high vs short entry)
            if t.mae >= liq_level and leverage > 1:
                # lose the margin posted (simplified: -margin, not full -notional)
                pnl = -meta["margin"]
                liquidations += 1
                exit_reason = "liquidation"
            else:
                pnl = t.net_pnl * notional
                # funding on notional over hold
                hold = max(1, t.hold_days_actual)
                pnl -= funding_per_day * hold * notional
                exit_reason = "time"

            equity += pnl
            trade_pnls.append(pnl)
            n_taken += 1
            peak = max(peak, equity)
            mdd = min(mdd, equity / peak - 1.0 if peak else 0.0)
            year_end[y] = equity
            if equity <= 0:
                equity = 0.0
                # wipe remaining
                open_ids.clear()
                open_count = 0
                year_end[y] = 0.0
                break
        else:
            if equity <= 0:
                skipped += 1
                continue
            if open_count >= max_open:
                skipped += 1
                continue
            base = equity if compound else start_eq
            margin = min(base * (risk_pct / adverse_ref), base * max_margin_pct)
            notional = margin * leverage
            if margin <= 0 or notional <= 0:
                skipped += 1
                continue
            open_ids[id(t)] = {"margin": margin, "notional": notional}
            open_count += 1
            max_concurrent = max(max_concurrent, open_count)
            year_end[y] = equity

    years = sorted(set(year_start) | set(year_end))
    yearly = {}
    for y in years:
        ys = year_start.get(y, start_eq)
        ye = year_end.get(y, ys)
        yearly[y] = {"start": ys, "end": ye, "ret": (ye / ys - 1.0) if ys > 0 else None}

    rets = [v["ret"] for v in yearly.values() if v["ret"] is not None]
    geo = None
    if rets:
        g = 1.0
        for x in rets:
            g *= 1 + x
        geo = g ** (1 / len(rets)) - 1

    return {
        "leverage": leverage,
        "liq_level": liq_level if leverage > 1 else None,
        "end_eq": equity,
        "total_ret": equity / start_eq - 1.0 if start_eq else None,
        "mdd": mdd,
        "n_taken": n_taken,
        "liquidations": liquidations,
        "liq_rate": liquidations / n_taken if n_taken else 0.0,
        "skipped": skipped,
        "max_concurrent": max_concurrent,
        "yearly": yearly,
        "geo_year": geo,
        "mean_pnl_usd": statistics.mean(trade_pnls) if trade_pnls else 0.0,
        "y2025": yearly.get("2025", {}).get("ret"),
        "y2026": yearly.get("2026", {}).get("ret"),
        "y2024": yearly.get("2024", {}).get("ret"),
    }


def main() -> None:
    ohlc = load_or_fetch(refresh=False)
    rule = RuleSet(
        name="G",
        description="blowoff t3",
        pump_min=0.40,
        min_rsi=70,
        min_vol_spike=3,
        min_dist_sma20=0.20,
        hold_days=3,
        take_profit=None,
        stop_loss=None,
        min_vol_usd=10_000,
    )
    trades = run_rule(ohlc, rule)
    print("=" * 96)
    print("LEVIER × FADE-BLOWOFF-T3 (time exit 3d, max 3 pos, fees inclus)")
    print(f"n_trades universe={len(trades)} | MAE moyen={statistics.mean(t.mae for t in trades)*100:.1f}%")
    print("Liquidation approx si MAE >= 1/L - 2pp (daily OHLC → sous-estime les wicks intra)")
    print("=" * 96)

    # fraction of trades that would liq at various L
    maes = [t.mae for t in trades]
    print("\n## A) Fragilité unitaire (sans sizing) — % trades où MAE touche le niveau de liq")
    print(f"  {'L':>4} {'liq@':>8} {'%trades liq':>12} {'mean net*L':>12}")
    for L in (1, 2, 3, 5, 7, 10, 20):
        thr = max(0.05, 1 / L - 0.02) if L > 1 else 9
        rate = sum(1 for m in maes if m >= thr) / len(maes) if L > 1 else 0
        # naive: if not liq, L*net else -1 (lose full 1R margin unit)
        print(f"  {L:>4}x {pct(thr) if L>1 else '  n/a':>8} {pct(rate):>12} {pct(statistics.mean(maes)*0):>12}")

    # more useful: expected net with liq = -100% margin equivalent on unit notional/L
    print("\n  Expected net SUR NOTIONAL (si liq → perte 1/L du notional = -margin/notional):")
    print(f"  {'L':>4} {'E[r_notional]':>14} {'WR no-liq path':>14}")
    for L in (1, 2, 3, 5, 10):
        thr = max(0.05, 1 / L - 0.02) if L > 1 else 9
        rs = []
        for t in trades:
            if L > 1 and t.mae >= thr:
                rs.append(-1.0 / L)  # lose margin / notional
            else:
                rs.append(t.net_pnl)  # return on notional
        print(f"  {L:>4}x {pct(statistics.mean(rs)):>14} {pct(sum(1 for r in rs if r>0)/len(rs)):>14}")

    print("\n## B) Portefeuille — risk 0.5% marge conceptuelle, levier sur le notional")
    print("  margin ≈ 1.25% equity ; notional = margin × L")
    print(f"  {'L':>4} {'Tot ret':>10} {'MDD':>9} {'Y2025':>9} {'Y2026':>9} {'Géo/an':>9} {'Liq%':>8} {'#liq':>6} {'End$':>8}")
    for L in (1, 2, 3, 5, 7, 10):
        r = sim(trades, leverage=L, risk_pct=0.005)
        print(
            f"  {L:>4}x {pct(r['total_ret']):>10} {pct(r['mdd']):>9} {pct(r['y2025']):>9} "
            f"{pct(r['y2026']):>9} {pct(r['geo_year']):>9} {pct(r['liq_rate']):>8} "
            f"{r['liquidations']:>6} {r['end_eq']:>8.1f}"
        )

    print("\n## C) Même chose — risk 1.0% (margin ≈ 2.5% equity)")
    print(f"  {'L':>4} {'Tot ret':>10} {'MDD':>9} {'Y2025':>9} {'Y2026':>9} {'Géo/an':>9} {'Liq%':>8} {'#liq':>6} {'End$':>8}")
    for L in (1, 2, 3, 5, 10):
        r = sim(trades, leverage=L, risk_pct=0.01)
        print(
            f"  {L:>4}x {pct(r['total_ret']):>10} {pct(r['mdd']):>9} {pct(r['y2025']):>9} "
            f"{pct(r['y2026']):>9} {pct(r['geo_year']):>9} {pct(r['liq_rate']):>8} "
            f"{r['liquidations']:>6} {r['end_eq']:>8.1f}"
        )

    print("\n## D) Funding drag (perp short) — risk 0.5%, L=3")
    print("  funding_per_day sur notional (ex +0.01%/jour = coût si rates positifs pour short)")
    for fund in (0.0, 0.0001, 0.0003, 0.0005, 0.001):
        r = sim(trades, leverage=3, risk_pct=0.005, funding_per_day=fund)
        print(
            f"  fund={fund*100:.2f}%/j  total={pct(r['total_ret'])}  Y2025={pct(r['y2025'])}  "
            f"MDD={pct(r['mdd'])}  liq={r['liquidations']}"
        )

    print("\n## E) Risk iso-notional: comparer 1x risk2% vs 2x risk1% vs 4x risk0.5%")
    # same target notional ~5% equity
    combos = [
        (1, 0.02),
        (2, 0.01),
        (4, 0.005),
        (5, 0.004),
    ]
    print(f"  {'setup':<18} {'notional~':>10} {'Tot':>10} {'MDD':>9} {'Y2025':>9} {'Liq%':>8}")
    for L, risk in combos:
        # notional_frac ≈ (risk/0.4)*L
        nfrac = (risk / 0.40) * L
        r = sim(trades, leverage=L, risk_pct=risk)
        print(
            f"  {L}x @ risk{risk*100:.1f}%{'':<4} {nfrac*100:>9.2f}% {pct(r['total_ret']):>10} "
            f"{pct(r['mdd']):>9} {pct(r['y2025']):>9} {pct(r['liq_rate']):>8}"
        )

    # MAE distribution vs leverage thresholds
    print("\n## F) Distribution MAE (adverse max 3j) — pourquoi le levier fait mal")
    for thr_label, thr in [("10%", 0.10), ("15%", 0.15), ("20%", 0.20), ("25%", 0.25), ("33%", 0.33), ("50%", 0.50)]:
        rate = sum(1 for m in maes if m >= thr) / len(maes)
        print(f"  MAE >= {thr_label:>4}: {pct(rate)} des trades  (≈ liq si L≈{1/thr:.0f}x)")

    print("\n## Verdict")
    r1 = sim(trades, leverage=1, risk_pct=0.005)
    r2 = sim(trades, leverage=2, risk_pct=0.005)
    r3 = sim(trades, leverage=3, risk_pct=0.005)
    r5 = sim(trades, leverage=5, risk_pct=0.005)
    print(f"  1x risk0.5%: total {pct(r1['total_ret'])}, MDD {pct(r1['mdd'])}, liq {r1['liquidations']}")
    print(f"  2x risk0.5%: total {pct(r2['total_ret'])}, MDD {pct(r2['mdd'])}, liq {r2['liquidations']} ({pct(r2['liq_rate'])})")
    print(f"  3x risk0.5%: total {pct(r3['total_ret'])}, MDD {pct(r3['mdd'])}, liq {r3['liquidations']} ({pct(r3['liq_rate'])})")
    print(f"  5x risk0.5%: total {pct(r5['total_ret'])}, MDD {pct(r5['mdd'])}, liq {r5['liquidations']} ({pct(r5['liq_rate'])})")
    print("  → Le levier multiplie le PnL MAIS la MAE moyenne (~35%) liquid beaucoup au-delà de 2–3x.")
    print("  → Mieux: monter le risk% à 1x que de passer 5–10x sur ce setup time-exit sans SL.")


if __name__ == "__main__":
    main()
