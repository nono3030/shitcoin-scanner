#!/usr/bin/env python3
"""Annual / portfolio stats for FADE-BLOWOFF-T3 (time-only blowoff)."""

from __future__ import annotations

import statistics
from collections import defaultdict

from backtest_fade import FEE_RATE, RuleSet, load_or_fetch, run_rule


def pct(x: float | None) -> str:
    if x is None:
        return "n/a"
    return f"{x * 100:+.2f}%"


def portfolio_sim(
    trades,
    start_eq: float = 100.0,
    risk_pct: float = 0.005,
    adverse: float = 0.40,
    max_open: int = 3,
    compound: bool = True,
    max_notional_pct: float = 0.10,
):
    """
    Event-driven portfolio:
      - size notional so that adverse move * notional ~= risk_pct * equity
      - max_open concurrent positions
      - exits credit pnl = net_pnl * notional
    """
    events = []
    for t in trades:
        events.append((t.exit_date, 0, "exit", t))
        events.append((t.entry_date, 1, "entry", t))
    events.sort(key=lambda x: (x[0], x[1]))

    equity = start_eq
    peak = start_eq
    mdd = 0.0
    open_ids: dict[int, float] = {}
    open_count = 0
    max_concurrent = 0
    skipped = 0
    trade_pnls_usd: list[float] = []
    year_start: dict[str, float] = {}
    year_end: dict[str, float] = {}
    equity_curve: list[tuple[str, float]] = [(trades[0].entry_date if trades else "", start_eq)]

    for day, _, kind, t in events:
        y = day[:4]
        if y not in year_start:
            year_start[y] = equity

        if kind == "exit":
            tid = id(t)
            if tid not in open_ids:
                continue
            notional = open_ids.pop(tid)
            open_count -= 1
            pnl = t.net_pnl * notional
            equity += pnl
            trade_pnls_usd.append(pnl)
            peak = max(peak, equity)
            mdd = min(mdd, equity / peak - 1.0 if peak > 0 else 0.0)
            year_end[y] = equity
            equity_curve.append((day, equity))
        else:
            if open_count >= max_open:
                skipped += 1
                continue
            base = equity if compound else start_eq
            if base <= 0:
                skipped += 1
                continue
            notional = min(base * (risk_pct / adverse), base * max_notional_pct)
            if notional <= 0:
                skipped += 1
                continue
            open_ids[id(t)] = notional
            open_count += 1
            max_concurrent = max(max_concurrent, open_count)
            year_end[y] = equity

    years = sorted(set(year_start) | set(year_end))
    yearly = {}
    for y in years:
        ys = year_start.get(y, start_eq)
        ye = year_end.get(y, ys)
        yearly[y] = {"start": ys, "end": ye, "ret": (ye / ys - 1.0) if ys > 0 else None}

    # full-year annualized from first to last
    if len(equity_curve) >= 2:
        d0 = equity_curve[0][0]
        d1 = equity_curve[-1][0]
    else:
        d0 = d1 = ""

    return {
        "end_eq": equity,
        "total_ret": equity / start_eq - 1.0,
        "mdd": mdd,
        "n_taken": len(trade_pnls_usd),
        "skipped": skipped,
        "max_concurrent": max_concurrent,
        "yearly": yearly,
        "mean_trade_usd": statistics.mean(trade_pnls_usd) if trade_pnls_usd else 0.0,
        "sum_trade_usd": sum(trade_pnls_usd) if trade_pnls_usd else 0.0,
        "window": (d0, d1),
    }


def main() -> None:
    ohlc = load_or_fetch(refresh=False)
    rule = RuleSet(
        name="G_TIME_ONLY_BLOWOFF",
        description="blowoff time 3d",
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
    print("=" * 88)
    print("FADE-BLOWOFF-T3 (time exit 3d) — stats annuelles")
    print(f"Fees RT={FEE_RATE*2*100:.2f}% | n_trades={len(trades)}")
    print("=" * 88)
    if not trades:
        return

    nets = [t.net_pnl for t in trades]
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x <= 0]
    pf = sum(wins) / abs(sum(losses)) if losses else None

    print("\n## 1) Stats PAR TRADE (toutes années confondues)")
    print(f"  Trades              : {len(nets)}")
    print(f"  Win rate            : {pct(len(wins)/len(nets))}")
    print(f"  Mean net            : {pct(statistics.mean(nets))}")
    print(f"  Median net          : {pct(statistics.median(nets))}")
    print(f"  Stdev               : {pct(statistics.pstdev(nets))}")
    print(f"  Avg win / avg loss  : {pct(statistics.mean(wins))} / {pct(statistics.mean(losses))}")
    print(f"  Profit factor       : {pf:.2f}" if pf else "  Profit factor       : n/a")
    print(f"  Mean MFE / MAE      : {pct(statistics.mean(t.mfe for t in trades))} / {pct(statistics.mean(t.mae for t in trades))}")
    q = statistics.quantiles(nets, n=4)
    print(f"  P25 / P75           : {pct(q[0])} / {pct(q[2])}")

    by_y: dict[str, list] = defaultdict(list)
    for t in trades:
        by_y[t.entry_date[:4]].append(t)

    first = min(t.entry_date for t in trades)
    last = max(t.exit_date for t in trades)
    print(f"\n  Fenêtre données    : {first} → {last}")

    print("\n## 2) Par année calendaire — stats trade (1 unit notionnel, NON = return compte)")
    print(f"  {'Year':<6} {'n':>5} {'WR':>8} {'Mean':>9} {'Med':>9} {'Sum_R*':>10} {'period'}")
    for y in sorted(by_y):
        xs = by_y[y]
        nets_y = [t.net_pnl for t in xs]
        d0 = min(t.entry_date for t in xs)
        d1 = max(t.exit_date for t in xs)
        print(
            f"  {y:<6} {len(xs):>5} {pct(sum(1 for x in nets_y if x>0)/len(nets_y)):>8} "
            f"{pct(statistics.mean(nets_y)):>9} {pct(statistics.median(nets_y)):>9} "
            f"{pct(sum(nets_y)):>10} {d0}→{d1}"
        )
    print("  * Sum_R = somme des returns si chaque trade = 100% du capital → ABSURDE pour un compte réel.")

    print("\n## 3) Return COMPTE réaliste (risk sizing + max 3 positions)")
    print("  Hypothèses: start=$100, adverse stop-concept 40%, cap notional 10% equity, fees inclus.")
    print("  Notional/trade ≈ risk%/40%  (ex risk 0.5% → ~1.25% du capital par trade)")

    for risk in (0.005, 0.01, 0.02):
        r = portfolio_sim(trades, risk_pct=risk, compound=True)
        print(f"\n  --- Risk {risk*100:.1f}% / trade  (notional ≈ {risk/0.40*100:.2f}% equity) ---")
        print(f"  Total return fenêtre : {pct(r['total_ret'])}  |  end equity={r['end_eq']:.2f}")
        print(f"  Max drawdown         : {pct(r['mdd'])}")
        print(f"  Trades pris / skip   : {r['n_taken']} / {r['skipped']} (cap max open)")
        print(f"  Max concurrent       : {r['max_concurrent']}")
        print(f"  {'Year':<6} {'Return':>10} {'Eq start':>10} {'Eq end':>10}")
        for y, v in r["yearly"].items():
            print(f"  {y:<6} {pct(v['ret']):>10} {v['start']:>10.2f} {v['end']:>10.2f}")

        # annualize if multi-year
        rets = [v["ret"] for v in r["yearly"].values() if v["ret"] is not None]
        if rets:
            # simple average of calendar year returns (2026 partial!)
            print(f"  Moyenne des returns annuels calendaires : {pct(statistics.mean(rets))}")
            # geometric
            geoms = 1.0
            for x in rets:
                geoms *= 1 + x
            geo = geoms ** (1 / len(rets)) - 1
            print(f"  Moyenne géométrique (par 'année' présente) : {pct(geo)}")

    print("\n## 4) Lecture honnête annualisée")
    r05 = portfolio_sim(trades, risk_pct=0.005, compound=True)
    r10 = portfolio_sim(trades, risk_pct=0.01, compound=True)
    # exclude incomplete narrative
    y2025_05 = r05["yearly"].get("2025", {}).get("ret")
    y2025_10 = r10["yearly"].get("2025", {}).get("ret")
    y2024_05 = r05["yearly"].get("2024", {}).get("ret")
    y2026_05 = r05["yearly"].get("2026", {}).get("ret")

    print("  Année la plus 'complète' dans l'échantillon: 2025")
    print(f"    2025 @ risk 0.5%: {pct(y2025_05)}")
    print(f"    2025 @ risk 1.0%: {pct(y2025_10)}")
    print(f"    2024 @ risk 0.5%: {pct(y2024_05)}  (souvent année partielle listing)")
    print(f"    2026 @ risk 0.5%: {pct(y2026_05)}  (année en cours / partielle)")
    print()
    print("  Fourchette réaliste annualisée (risk 0.5–1%, sizing conservateur):")
    if y2025_05 is not None and y2024_05 is not None:
        low = min(y2024_05, y2025_05)
        high = max(y2024_05, y2025_05, y2026_05 or 0)
        print(f"    ~ {pct(low)} à {pct(high)} selon l'année et le risk")
    print("  Ce n'est PAS du 100%+/an sauf si tu size trop gros (et le MDD explose).")
    print("  Les pertes unitaires restent ~-30% notional: le risk% par trade est critique.")


if __name__ == "__main__":
    main()
