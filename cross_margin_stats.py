#!/usr/bin/env python3
"""
Isolated vs Cross margin — FADE-BLOWOFF-T3.

Isolated:
  - chaque short a sa marge dédiée
  - liq si MAE >= ~1/L → perte = marge de CE trade seulement

Cross:
  - tout le wallet backe toutes les positions
  - pas de liq unitaire sur wick d'une paire
  - equity MTM = cash + sum uPnL
  - uPnL short marqué au WORST (high) de chaque jour pour stress
  - account liq si equity < maintenance (somme notional/L * mm_factor)
  - en cas de liq compte: equity → 0 (wipe)

  python cross_margin_stats.py
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field

from backtest_fade import RuleSet, load_or_fetch, run_rule
from kraken_data import Candle, load_or_refresh


def pct(x: float | None) -> str:
    if x is None:
        return "n/a"
    return f"{x * 100:+.2f}%"


@dataclass
class OpenPos:
    trade: object
    pair: str
    entry_date: str
    exit_date: str
    entry_px: float
    notional: float
    margin: float  # isolated only meaningful
    hold_days: int
    bars_seen: int = 0


def get_rule_trades(ohlc_bt):
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
    return run_rule(ohlc_bt, rule)


def build_price_path(ohlc_map: dict[str, list[Candle]], pair: str, entry_date: str, hold_days: int):
    """
    Returns list of daily marks after entry:
      [{date, open, high, low, close}, ...] length <= hold_days
    Entry assumed at open of entry_date bar.
    """
    series = ohlc_map.get(pair) or []
    bars = [c for c in series if c.date >= entry_date]
    if not bars:
        return []
    out = []
    for c in bars[:hold_days]:
        out.append({"date": c.date, "o": c.o, "h": c.h, "l": c.l, "c": c.c})
    return out


def sim_isolated(trades, leverage: float, risk_pct: float = 0.005, adverse_ref: float = 0.40,
                 max_open: int = 3, start_eq: float = 100.0, max_margin_pct: float = 0.10):
    liq_level = max(0.05, 1.0 / leverage - 0.02) if leverage > 1 else 9.0
    events = []
    for t in trades:
        events.append((t.exit_date, 0, "exit", t))
        events.append((t.entry_date, 1, "entry", t))
    events.sort(key=lambda x: (x[0], x[1]))

    equity = start_eq
    peak = start_eq
    mdd = 0.0
    open_map = {}
    open_n = 0
    liqs = 0
    taken = 0
    skipped = 0
    year_start, year_end = {}, {}

    for day, _, kind, t in events:
        y = day[:4]
        year_start.setdefault(y, equity)
        if kind == "exit":
            if id(t) not in open_map:
                continue
            meta = open_map.pop(id(t))
            open_n -= 1
            if leverage > 1 and t.mae >= liq_level:
                pnl = -meta["margin"]
                liqs += 1
            else:
                pnl = t.net_pnl * meta["notional"]
            equity = max(0.0, equity + pnl)
            taken += 1
            peak = max(peak, equity)
            mdd = min(mdd, equity / peak - 1 if peak else 0)
            year_end[y] = equity
            if equity <= 0:
                open_map.clear()
                open_n = 0
                break
        else:
            if equity <= 0 or open_n >= max_open:
                skipped += 1
                continue
            margin = min(equity * (risk_pct / adverse_ref), equity * max_margin_pct)
            notional = margin * leverage
            open_map[id(t)] = {"margin": margin, "notional": notional}
            open_n += 1
            year_end[y] = equity

    yearly = {}
    for y in sorted(set(year_start) | set(year_end)):
        ys, ye = year_start.get(y, start_eq), year_end.get(y, year_start.get(y, start_eq))
        yearly[y] = ye / ys - 1 if ys else None
    return {
        "mode": "isolated",
        "L": leverage,
        "total": equity / start_eq - 1,
        "end": equity,
        "mdd": mdd,
        "liqs": liqs,
        "taken": taken,
        "skipped": skipped,
        "account_liqs": 0,
        "y2025": yearly.get("2025"),
        "y2026": yearly.get("2026"),
        "yearly": yearly,
    }


def sim_cross(
    trades,
    ohlc_map: dict[str, list[Candle]],
    leverage: float,
    risk_pct: float = 0.005,
    adverse_ref: float = 0.40,
    max_open: int = 3,
    start_eq: float = 100.0,
    max_margin_pct: float = 0.10,
    mm_of_initial: float = 0.50,
    mark_mode: str = "high",  # stress: mark shorts at daily high
):
    """
    Cross margin event-driven daily:
      - cash changes only on entry (no cash lock beyond accounting) and exit
      - actually: margin is not removed from equity in cross; full equity is available
      - notional sized from equity at entry: margin_concept = risk_frac * equity, notional = margin * L
      - each day: mark all open shorts; if equity_mtm < maintenance → account wipe

    maintenance = sum(notional / L * mm_of_initial) = sum(margin_concept * mm_of_initial)
    """
    # index trades by entry/exit
    by_entry = {}
    for t in trades:
        by_entry.setdefault(t.entry_date, []).append(t)

    # all calendar days from first entry to last exit
    if not trades:
        return {}
    days = sorted({d for t in trades for d in (t.entry_date, t.exit_date)})
    # expand using OHLC calendar union is heavy; step day by day via sorted unique from all involved bars
    day_set = set()
    for t in trades:
        path = build_price_path(ohlc_map, t.pair if hasattr(t, "pair") else t.wsname if False else "", t.entry_date, t.hold_days_actual + 5)
    # fix: Trade has .pair
    for t in trades:
        series = ohlc_map.get(t.pair) or []
        for c in series:
            if t.entry_date <= c.date <= t.exit_date:
                day_set.add(c.date)
    # also include all entry/exit
    for t in trades:
        day_set.add(t.entry_date)
        day_set.add(t.exit_date)
    timeline = sorted(day_set)

    equity_cash = start_eq  # realized settled
    # open positions list
    open_pos: list[dict] = []
    peak = start_eq
    mdd = 0.0
    taken = 0
    skipped = 0
    account_liqs = 0
    pos_closed_normal = 0
    year_start, year_end = {}, {}
    pending_entries = {d: [] for d in timeline}
    pending_exits = {d: [] for d in timeline}
    for t in trades:
        pending_entries.setdefault(t.entry_date, []).append(t)
        pending_exits.setdefault(t.exit_date, []).append(t)

    trade_open_meta = {}  # id -> meta
    wiped = False

    def mtm_equity(day: str) -> float:
        """Equity with open shorts marked at day's high (worst) or close."""
        u = 0.0
        for meta in open_pos:
            t = meta["t"]
            path = meta["path"]
            # find bar for day or last known
            bar = None
            for b in path:
                if b["date"] == day:
                    bar = b
                    break
            if bar is None:
                # use last bar <= day
                for b in path:
                    if b["date"] <= day:
                        bar = b
            if bar is None:
                continue
            px = bar["h"] if mark_mode == "high" else bar["c"]
            # short pnl
            u += (meta["entry_px"] - px) / meta["entry_px"] * meta["notional"]
        return equity_cash + u

    def maintenance() -> float:
        # initial margin concept = notional/L ; maintenance fraction of that
        return sum(meta["notional"] / leverage * mm_of_initial for meta in open_pos)

    def current_eq_peak(day):
        nonlocal peak, mdd
        eq = mtm_equity(day)
        peak = max(peak, eq)
        if peak > 0:
            mdd = min(mdd, eq / peak - 1)
        return eq

    for day in timeline:
        if wiped:
            break
        y = day[:4]
        year_start.setdefault(y, max(mtm_equity(day), equity_cash))

        # 1) process exits first at close of exit day (use close mark)
        still_open = []
        for meta in open_pos:
            t = meta["t"]
            if t.exit_date == day or day >= t.exit_date:
                # settle at close of exit bar
                path = meta["path"]
                bar = path[-1] if path else None
                for b in path:
                    if b["date"] == t.exit_date:
                        bar = b
                        break
                if bar is None:
                    px = meta["entry_px"]
                else:
                    px = bar["c"]
                gross = (meta["entry_px"] - px) / meta["entry_px"]
                # fees already in t.net_pnl approx — use t.net_pnl * notional for consistency with bt
                pnl = t.net_pnl * meta["notional"]
                equity_cash += pnl
                taken += 1
                pos_closed_normal += 1
                trade_open_meta.pop(id(t), None)
            else:
                still_open.append(meta)
        open_pos = still_open

        # 2) new entries
        for t in pending_entries.get(day, []):
            if len(open_pos) >= max_open:
                skipped += 1
                continue
            eq_now = mtm_equity(day)
            if eq_now <= 0:
                skipped += 1
                continue
            margin = min(eq_now * (risk_pct / adverse_ref), eq_now * max_margin_pct)
            notional = margin * leverage
            if notional <= 0:
                skipped += 1
                continue
            path = build_price_path(ohlc_map, t.pair, t.entry_date, t.hold_days_actual + 2)
            if not path:
                skipped += 1
                continue
            entry_px = path[0]["o"]  # open entry
            # align with backtest entry
            entry_px = t.entry_px
            meta = {
                "t": t,
                "notional": notional,
                "margin": margin,
                "entry_px": entry_px,
                "path": path,
            }
            open_pos.append(meta)
            trade_open_meta[id(t)] = meta

        # 3) intraday stress mark (high) after entries
        eq = mtm_equity(day)
        maint = maintenance()
        peak = max(peak, eq)
        if peak > 0:
            mdd = min(mdd, eq / peak - 1)

        # cross liquidation: equity below maintenance
        if open_pos and eq < maint:
            # wipe account
            account_liqs += 1
            equity_cash = 0.0
            open_pos.clear()
            trade_open_meta.clear()
            wiped = True
            year_end[y] = 0.0
            break

        year_end[y] = eq if open_pos else equity_cash

    # close any leftover at last
    if not wiped:
        for meta in list(open_pos):
            t = meta["t"]
            pnl = t.net_pnl * meta["notional"]
            equity_cash += pnl
            taken += 1
        open_pos.clear()

    end = 0.0 if wiped else equity_cash
    yearly = {}
    for y in sorted(set(year_start) | set(year_end)):
        ys = year_start.get(y, start_eq)
        ye = year_end.get(y, ys) if not (wiped and y >= min(year_end or [y])) else year_end.get(y, 0)
        # simplify
        ye = year_end.get(y, ys)
        yearly[y] = (ye / ys - 1) if ys else None

    return {
        "mode": "cross",
        "L": leverage,
        "total": end / start_eq - 1,
        "end": end,
        "mdd": mdd,
        "liqs": 0,  # position-level
        "account_liqs": account_liqs,
        "taken": taken,
        "skipped": skipped,
        "y2025": yearly.get("2025"),
        "y2026": yearly.get("2026"),
        "yearly": yearly,
        "wiped": wiped,
    }


def main():
    print("Loading OHLC...")
    ohlc_bt = load_or_fetch(refresh=False)
    # map by pair name - backtest uses wsname as pair on Trade
    ohlc_map, _ = load_or_refresh(refresh=False)
    # Trade.pair is wsname from backtest_fade
    trades = get_rule_trades(ohlc_bt)
    # ensure pair field
    for t in trades:
        if not hasattr(t, "pair"):
            pass

    print(f"Trades={len(trades)} | MAE mean={statistics.mean(t.mae for t in trades)*100:.1f}%")
    print("Cross mark stress = daily HIGH (worst for shorts). Maint = 50% of initial margin.")
    print("=" * 100)

    print(f"\n{'Mode':<10} {'L':>3} {'Total':>10} {'MDD':>9} {'Y2025':>9} {'Y2026':>9} "
          f"{'PosLiq':>7} {'AcctLiq':>8} {'End$':>8} {'notes'}")
    print("-" * 100)

    rows = []
    for L in (1, 2, 3, 5, 10):
        for risk in (0.005,):
            iso = sim_isolated(trades, leverage=L, risk_pct=risk)
            crx = sim_cross(trades, ohlc_map, leverage=L, risk_pct=risk, mark_mode="high")
            for r in (iso, crx):
                note = ""
                if r["mode"] == "cross" and r.get("wiped"):
                    note = "WIPE"
                if r["mode"] == "isolated" and L == 1:
                    note = "ref"
                print(
                    f"{r['mode']:<10} {L:>3}x {pct(r['total']):>10} {pct(r['mdd']):>9} "
                    f"{pct(r.get('y2025')):>9} {pct(r.get('y2026')):>9} "
                    f"{r.get('liqs',0):>7} {r.get('account_liqs',0):>8} {r['end']:>8.1f} {note}"
                )
                rows.append(r)

    # also cross marked at close (less stressed)
    print("\n--- Cross marqué au CLOSE (moins stressé que high) ---")
    print(f"{'L':>3} {'Total':>10} {'MDD':>9} {'Y2025':>9} {'AcctLiq':>8} {'End$':>8}")
    for L in (1, 2, 3, 5, 10):
        r = sim_cross(trades, ohlc_map, leverage=L, risk_pct=0.005, mark_mode="close")
        print(f"{L:>3}x {pct(r['total']):>10} {pct(r['mdd']):>9} {pct(r.get('y2025')):>9} "
              f"{r['account_liqs']:>8} {r['end']:>8.1f} {'WIPE' if r.get('wiped') else ''}")

    print("\n--- Cross risk 1% mark=high ---")
    for L in (1, 2, 3, 5):
        r = sim_cross(trades, ohlc_map, leverage=L, risk_pct=0.01, mark_mode="high")
        print(f"{L}x total={pct(r['total'])} MDD={pct(r['mdd'])} Y25={pct(r.get('y2025'))} "
              f"acct_liq={r['account_liqs']} end={r['end']:.1f}")

    print("\n## Interprétation")
    print("  Isolated: wick d'UNE paire → liq locale, reste du compte survivant.")
    print("  Cross: les wicks sont absorbés par tout le wallet → moins de sorties précoces,")
    print("         MAIS plusieurs pumps corrélés en même temps peuvent WIPE le compte entier.")
    print("  Sur shitcoin fade multi-positions, cross + levier élevé = queue de distribution fatale.")


if __name__ == "__main__":
    main()
