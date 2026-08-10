#!/usr/bin/env python3
"""
Backtest FADE (short after shitcoin pump) — règles claires, exécution réaliste.

Exécution (anti look-ahead):
  - Signal calculé sur la bougie daily close de J (données <= J)
  - Entrée short au OPEN de J+1
  - Sortie selon règles (horizon fixe et/ou TP/SL sur high/low intraday)

PnL short:
  pnl = (entry_price - exit_price) / entry_price - fees
  fees = 2 * FEE_RATE (entrée + sortie)

Usage:
  python backtest_fade.py
  python backtest_fade.py --refresh   # re-télécharge OHLC (lent)
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

BASE = "https://api.kraken.com/0/public"
SLEEP = 0.35
OUT_DIR = Path(__file__).resolve().parent / "out"
CACHE_DIR = Path(__file__).resolve().parent / "cache"
CACHE_FILE = CACHE_DIR / "ohlc_daily.json"

FEE_RATE = 0.0026  # ~0.26% taker Kraken round-trip side ≈ 0.26% * 2 total costs
# (conservative; maker would be lower)

EXCLUDE_BASES = {
    "XXBT", "XBT", "BTC", "XETH", "ETH",
    "ZEUR", "ZGBP", "ZUSD", "ZCAD", "ZJPY", "ZAUD", "CHF",
    "USDT", "USDC", "DAI", "EUR", "GBP", "USD", "AUD", "CAD", "JPY",
    "EURQ", "USDQ", "EURR", "USDR", "PYUSD", "USDG", "RLUSD", "AUSD",
    "ETH2", "TBTC", "WBTC",
}

MIN_AVG_VOL_USD = 10_000  # stricter liquidity for short realism


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class Candle:
    ts: int
    o: float
    h: float
    l: float
    c: float
    vwap: float
    volume: float
    count: int


@dataclass
class RuleSet:
    name: str
    description: str
    # entry filters (evaluated at signal day i)
    pump_lookback: int = 3
    pump_min: float = 0.25
    pump_max: float | None = None  # optional cap
    min_rsi: float | None = 70.0
    min_vol_spike: float | None = 3.0
    min_dist_sma20: float | None = 0.20
    # exit
    hold_days: int = 3
    take_profit: float | None = 0.15  # short TP: price down 15%
    stop_loss: float | None = 0.20    # short SL: price up 20%
    # risk
    min_vol_usd: float = MIN_AVG_VOL_USD


@dataclass
class Trade:
    rule: str
    pair: str
    signal_date: str
    entry_date: str
    exit_date: str
    entry_px: float
    exit_px: float
    pump_ret: float
    rsi: float | None
    vol_spike: float | None
    dist_sma20: float | None
    hold_days_actual: int
    exit_reason: str
    gross_pnl: float
    net_pnl: float
    mfe: float  # best excursion for short (price down)
    mae: float  # worst excursion for short (price up)


# ---------------------------------------------------------------------------
# HTTP + cache
# ---------------------------------------------------------------------------

def kraken_get(path: str, params: dict | None = None) -> dict:
    qs = urllib.parse.urlencode(params or {})
    url = f"{BASE}/{path}" + (f"?{qs}" if qs else "")
    req = urllib.request.Request(url, headers={"User-Agent": "fade-bt/1.0"})
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode())
            return data
        except Exception:
            if attempt < 4:
                time.sleep(1.2 * (attempt + 1))
                continue
            raise
    return {"error": ["fail"], "result": {}}


def load_usd_alt_pairs() -> list[dict]:
    data = kraken_get("AssetPairs")
    if data.get("error"):
        raise RuntimeError(data["error"])
    out, seen = [], set()
    for key, v in data["result"].items():
        ws = v.get("wsname") or ""
        quote = v.get("quote") or ""
        base = v.get("base") or ""
        status = v.get("status") or ""
        if status and status != "online":
            continue
        if not ws.endswith("/USD") or quote not in ("ZUSD", "USD"):
            continue
        if base in EXCLUDE_BASES:
            continue
        if ws in seen:
            continue
        seen.add(ws)
        out.append({"pair_key": key, "wsname": ws, "base": base})
    return sorted(out, key=lambda x: x["wsname"])


def fetch_ohlc(pair_key: str) -> list[list]:
    data = kraken_get("OHLC", {"pair": pair_key, "interval": 1440})
    if data.get("error"):
        return []
    result = data.get("result") or {}
    sk = next((k for k in result if k != "last"), None)
    if not sk:
        return []
    return result[sk]


def download_all(pairs: list[dict]) -> dict[str, list[Candle]]:
    ohlc: dict[str, list[Candle]] = {}
    for i, p in enumerate(pairs, 1):
        rows = fetch_ohlc(p["pair_key"])
        candles = []
        for row in rows:
            candles.append(
                Candle(
                    int(row[0]), float(row[1]), float(row[2]), float(row[3]),
                    float(row[4]), float(row[5]), float(row[6]), int(row[7]),
                )
            )
        if len(candles) >= 40:
            ohlc[p["wsname"]] = candles
        if i % 25 == 0 or i == len(pairs):
            print(f"  fetch {i}/{len(pairs)} kept={len(ohlc)}")
        time.sleep(SLEEP)
    return ohlc


def candles_to_json(ohlc: dict[str, list[Candle]]) -> dict:
    return {
        name: [[c.ts, c.o, c.h, c.l, c.c, c.vwap, c.volume, c.count] for c in series]
        for name, series in ohlc.items()
    }


def json_to_candles(raw: dict) -> dict[str, list[Candle]]:
    out = {}
    for name, rows in raw.items():
        out[name] = [
            Candle(int(r[0]), float(r[1]), float(r[2]), float(r[3]),
                   float(r[4]), float(r[5]), float(r[6]), int(r[7]))
            for r in rows
        ]
    return out


def load_or_fetch(refresh: bool) -> dict[str, list[Candle]]:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if CACHE_FILE.exists() and not refresh:
        print(f"Loading cache {CACHE_FILE} ...")
        raw = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        ohlc = json_to_candles(raw.get("ohlc", raw))
        print(f"  {len(ohlc)} series")
        return ohlc
    print("Downloading OHLC from Kraken (slow)...")
    pairs = load_usd_alt_pairs()
    print(f"  universe={len(pairs)}")
    ohlc = download_all(pairs)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "ohlc": candles_to_json(ohlc),
    }
    CACHE_FILE.write_text(json.dumps(payload), encoding="utf-8")
    print(f"  cached {len(ohlc)} series -> {CACHE_FILE}")
    return ohlc


# ---------------------------------------------------------------------------
# Features at signal day i (no future data)
# ---------------------------------------------------------------------------

def sma(xs: list[float], n: int) -> float | None:
    if len(xs) < n:
        return None
    return sum(xs[-n:]) / n


def rsi(closes: list[float], n: int = 14) -> float | None:
    if len(closes) < n + 1:
        return None
    gains, losses = [], []
    for i in range(-n, 0):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    ag, al = sum(gains) / n, sum(losses) / n
    if al == 0:
        return 100.0
    rs = ag / al
    return 100.0 - 100.0 / (1.0 + rs)


def features_at(candles: list[Candle], i: int) -> dict[str, float | None]:
    closes = [c.c for c in candles[: i + 1]]
    vols = [c.volume for c in candles[: i + 1]]
    c = candles[i]
    f: dict[str, float | None] = {"close": c.c, "vol_usd": c.volume * c.c}
    for lb in (2, 3, 5):
        f[f"ret_{lb}d"] = (closes[-1] / closes[-1 - lb] - 1.0) if len(closes) > lb else None
    s20 = sma(closes, 20)
    f["dist_sma20"] = (closes[-1] / s20 - 1.0) if s20 else None
    f["rsi14"] = rsi(closes, 14)
    if len(vols) >= 21:
        avg = sum(vols[-21:-1]) / 20.0
        f["vol_spike"] = (vols[-1] / avg) if avg > 0 else None
    else:
        f["vol_spike"] = None
    # 10d avg liquidity
    if i >= 9:
        f["avg_vol_usd_10d"] = sum(candles[j].volume * candles[j].c for j in range(i - 9, i + 1)) / 10.0
    else:
        f["avg_vol_usd_10d"] = f["vol_usd"]
    return f


def passes_entry(f: dict[str, float | None], rule: RuleSet) -> bool:
    ret = f.get(f"ret_{rule.pump_lookback}d")
    if ret is None or ret < rule.pump_min:
        return False
    if rule.pump_max is not None and ret >= rule.pump_max:
        return False
    liq = f.get("avg_vol_usd_10d") or 0.0
    if liq < rule.min_vol_usd:
        return False
    if rule.min_rsi is not None:
        r = f.get("rsi14")
        if r is None or r < rule.min_rsi:
            return False
    if rule.min_vol_spike is not None:
        v = f.get("vol_spike")
        if v is None or v < rule.min_vol_spike:
            return False
    if rule.min_dist_sma20 is not None:
        d = f.get("dist_sma20")
        if d is None or d < rule.min_dist_sma20:
            return False
    return True


# ---------------------------------------------------------------------------
# Simulate one short trade
# ---------------------------------------------------------------------------

def simulate_short(
    candles: list[Candle],
    signal_i: int,
    rule: RuleSet,
    pair: str,
    f: dict[str, float | None],
) -> Trade | None:
    """
    signal_i = day J close where signal fires
    entry = open of J+1
    path days = J+1 .. J+hold_days (inclusive of entry day as day 1 of hold)
    """
    entry_i = signal_i + 1
    last_i = entry_i + rule.hold_days - 1
    if last_i >= len(candles):
        return None
    # need full bars for path
    if entry_i >= len(candles):
        return None

    entry_px = candles[entry_i].o
    if entry_px <= 0:
        return None

    exit_px = None
    exit_i = None
    reason = None
    mfe = 0.0  # max favorable = max (entry - low) / entry
    mae = 0.0  # max adverse = max (high - entry) / entry

    for j in range(entry_i, last_i + 1):
        bar = candles[j]
        # path extremes for short
        fav = (entry_px - bar.l) / entry_px
        adv = (bar.h - entry_px) / entry_px
        mfe = max(mfe, fav)
        mae = max(mae, adv)

        # Intraday order: conservative for short —
        # if both SL and TP could hit same day, assume SL first (worse case)
        hit_sl = rule.stop_loss is not None and adv >= rule.stop_loss
        hit_tp = rule.take_profit is not None and fav >= rule.take_profit

        if hit_sl and hit_tp:
            exit_px = entry_px * (1.0 + rule.stop_loss)
            exit_i = j
            reason = "SL_same_day_priority"
            break
        if hit_sl:
            exit_px = entry_px * (1.0 + rule.stop_loss)
            exit_i = j
            reason = "stop_loss"
            break
        if hit_tp:
            exit_px = entry_px * (1.0 - rule.take_profit)
            exit_i = j
            reason = "take_profit"
            break

    if exit_px is None:
        exit_i = last_i
        exit_px = candles[exit_i].c
        reason = "time_exit"

    gross = (entry_px - exit_px) / entry_px
    fees = 2.0 * FEE_RATE
    net = gross - fees
    hold = exit_i - entry_i + 1

    def dts(idx: int) -> str:
        return datetime.fromtimestamp(candles[idx].ts, tz=timezone.utc).strftime("%Y-%m-%d")

    return Trade(
        rule=rule.name,
        pair=pair,
        signal_date=dts(signal_i),
        entry_date=dts(entry_i),
        exit_date=dts(exit_i),
        entry_px=entry_px,
        exit_px=exit_px,
        pump_ret=float(f.get(f"ret_{rule.pump_lookback}d") or 0.0),
        rsi=f.get("rsi14"),
        vol_spike=f.get("vol_spike"),
        dist_sma20=f.get("dist_sma20"),
        hold_days_actual=hold,
        exit_reason=reason or "time_exit",
        gross_pnl=gross,
        net_pnl=net,
        mfe=mfe,
        mae=mae,
    )


# ---------------------------------------------------------------------------
# Run backtest for one rule
# ---------------------------------------------------------------------------

def run_rule(ohlc: dict[str, list[Candle]], rule: RuleSet) -> list[Trade]:
    trades: list[Trade] = []
    # de-dupe: max 1 open trade per pair (no stacking)
    for pair, candles in ohlc.items():
        next_free_i = 0  # index in candles; cannot enter before this
        # signal day must leave room for entry + hold
        max_signal = len(candles) - rule.hold_days - 2
        for i in range(25, max_signal + 1):
            if i < next_free_i:
                continue
            f = features_at(candles, i)
            if not passes_entry(f, rule):
                continue
            tr = simulate_short(candles, i, rule, pair, f)
            if tr is None:
                continue
            trades.append(tr)
            # lock pair until after exit bar
            # exit_date maps to exit_i = entry_i + hold - 1; entry_i = i+1
            # next signal only after exit bar
            next_free_i = i + 1 + tr.hold_days_actual
    return trades


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

def pct(x: float | None) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "n/a"
    return f"{x * 100:+.2f}%"


def mean(xs: list[float]) -> float | None:
    return statistics.mean(xs) if xs else None


def med(xs: list[float]) -> float | None:
    return statistics.median(xs) if xs else None


def stdev(xs: list[float]) -> float | None:
    return statistics.pstdev(xs) if len(xs) >= 2 else None


def quantiles(xs: list[float]) -> tuple[float | None, float | None, float | None]:
    if len(xs) < 4:
        return None, med(xs), None
    q = statistics.quantiles(xs, n=4)
    return q[0], statistics.median(xs), q[2]


def max_drawdown_equity(pnls: list[float]) -> float:
    """Sequential equity curve by trade order (approx; not calendar)."""
    eq = 0.0
    peak = 0.0
    mdd = 0.0
    for p in pnls:
        eq += p
        peak = max(peak, eq)
        mdd = min(mdd, eq - peak)
    return mdd


def calendar_equity(trades: list[Trade]) -> tuple[list[tuple[str, float]], float]:
    """Sum net pnl by exit_date, build cumulative, return mdd on daily sums."""
    by_day: dict[str, float] = {}
    for t in trades:
        by_day[t.exit_date] = by_day.get(t.exit_date, 0.0) + t.net_pnl
    days = sorted(by_day)
    curve = []
    eq = 0.0
    peak = 0.0
    mdd = 0.0
    for d in days:
        eq += by_day[d]
        peak = max(peak, eq)
        mdd = min(mdd, eq - peak)
        curve.append((d, eq))
    return curve, mdd


def summarize(rule: RuleSet, trades: list[Trade]) -> dict[str, Any]:
    nets = [t.net_pnl for t in trades]
    gross = [t.gross_pnl for t in trades]
    wins = [p for p in nets if p > 0]
    losses = [p for p in nets if p <= 0]
    p25, p50, p75 = quantiles(nets)
    _, cal_mdd = calendar_equity(trades)

    # by exit reason
    by_reason: dict[str, list[float]] = {}
    for t in trades:
        by_reason.setdefault(t.exit_reason, []).append(t.net_pnl)

    # yearly
    by_year: dict[str, list[float]] = {}
    for t in trades:
        y = t.entry_date[:4]
        by_year.setdefault(y, []).append(t.net_pnl)

    # expectancy
    wr = len(wins) / len(nets) if nets else 0.0
    avg_w = mean(wins) or 0.0
    avg_l = mean(losses) or 0.0
    # profit factor
    sum_w = sum(wins) if wins else 0.0
    sum_l = abs(sum(losses)) if losses else 0.0
    pf = (sum_w / sum_l) if sum_l > 0 else None

    return {
        "rule": rule.name,
        "description": rule.description,
        "n": len(trades),
        "win_rate": wr,
        "mean_net": mean(nets),
        "med_net": med(nets),
        "stdev_net": stdev(nets),
        "p25": p25,
        "p75": p75,
        "mean_gross": mean(gross),
        "avg_win": avg_w if wins else None,
        "avg_loss": avg_l if losses else None,
        "expectancy": wr * avg_w + (1 - wr) * avg_l if nets else None,
        "profit_factor": pf,
        "sum_net_R": sum(nets) if nets else 0.0,  # unitless sum of returns (1 coin 1 unit each)
        "mean_mfe": mean([t.mfe for t in trades]),
        "mean_mae": mean([t.mae for t in trades]),
        "mean_hold": mean([float(t.hold_days_actual) for t in trades]),
        "trade_mdd_sum": max_drawdown_equity(nets),
        "calendar_mdd_sum": cal_mdd,
        "by_reason": {
            k: {"n": len(v), "mean_net": mean(v), "win_rate": sum(1 for x in v if x > 0) / len(v)}
            for k, v in sorted(by_reason.items())
        },
        "by_year": {
            y: {
                "n": len(v),
                "mean_net": mean(v),
                "win_rate": sum(1 for x in v if x > 0) / len(v),
                "sum_net": sum(v),
            }
            for y, v in sorted(by_year.items())
        },
        "params": asdict(rule),
    }


def print_summary(s: dict) -> None:
    print("\n" + "=" * 96)
    print(f"RULE: {s['rule']}")
    print(f"  {s['description']}")
    print("=" * 96)
    if s["n"] == 0:
        print("  No trades.")
        return
    print(f"  Trades              : {s['n']}")
    print(f"  Win rate            : {pct(s['win_rate'])}")
    print(f"  Mean net / trade    : {pct(s['mean_net'])}   (gross {pct(s['mean_gross'])})")
    print(f"  Median net          : {pct(s['med_net'])}")
    print(f"  P25 / P75           : {pct(s['p25'])} / {pct(s['p75'])}")
    print(f"  Stdev               : {pct(s['stdev_net'])}")
    print(f"  Avg win / avg loss  : {pct(s['avg_win'])} / {pct(s['avg_loss'])}")
    print(f"  Expectancy / trade  : {pct(s['expectancy'])}")
    print(f"  Profit factor       : {s['profit_factor']:.2f}" if s["profit_factor"] else "  Profit factor       : n/a")
    print(f"  Sum net (1u/trade)  : {pct(s['sum_net_R'])}")
    print(f"  Mean MFE / MAE      : {pct(s['mean_mfe'])} / {pct(s['mean_mae'])}")
    print(f"  Mean hold (days)    : {s['mean_hold']:.2f}" if s["mean_hold"] else "")
    print(f"  MDD (trade-seq sum) : {pct(s['trade_mdd_sum'])}")
    print(f"  MDD (calendar sum)  : {pct(s['calendar_mdd_sum'])}")
    print("  Exit reasons:")
    for k, v in s["by_reason"].items():
        print(f"    {k:<22} n={v['n']:<5} mean={pct(v['mean_net']):>9}  wr={pct(v['win_rate'])}")
    print("  By year:")
    for y, v in s["by_year"].items():
        print(
            f"    {y}  n={v['n']:<5} mean={pct(v['mean_net']):>9}  wr={pct(v['win_rate']):>8}  sum={pct(v['sum_net'])}"
        )


# ---------------------------------------------------------------------------
# Rule book (clear, fixed)
# ---------------------------------------------------------------------------

def rulebook() -> list[RuleSet]:
    """
    Toutes les règles partagent le même protocole d'exécution.
    Seuls les FILTRES d'entrée et la GESTION de sortie changent.
    """
    return [
        RuleSet(
            name="A_BASE_PUMP",
            description=(
                "ENTRY: ret_3d >= +25%, liq_10d >= $10k. "
                "EXIT: hold 3d, TP 15%, SL 20%. No RSI/vol filter."
            ),
            pump_min=0.25,
            min_rsi=None,
            min_vol_spike=None,
            min_dist_sma20=None,
            hold_days=3,
            take_profit=0.15,
            stop_loss=0.20,
        ),
        RuleSet(
            name="B_BLOWOFF",
            description=(
                "ENTRY: ret_3d >= +40%, RSI14 >= 70, vol_spike >= 3x, dist_sma20 >= +20%, liq>=$10k. "
                "EXIT: hold 3d, TP 15%, SL 20%."
            ),
            pump_min=0.40,
            min_rsi=70.0,
            min_vol_spike=3.0,
            min_dist_sma20=0.20,
            hold_days=3,
            take_profit=0.15,
            stop_loss=0.20,
        ),
        RuleSet(
            name="C_CLIMAX_VOL",
            description=(
                "ENTRY: ret_3d >= +25%, vol_spike >= 5x, liq>=$10k. "
                "EXIT: hold 3d, TP 15%, SL 20%."
            ),
            pump_min=0.25,
            min_rsi=None,
            min_vol_spike=5.0,
            min_dist_sma20=None,
            hold_days=3,
            take_profit=0.15,
            stop_loss=0.20,
        ),
        RuleSet(
            name="D_STRICT_FADE",
            description=(
                "ENTRY: ret_3d >= +40%, RSI >= 75, vol_spike >= 4x, dist_sma20 >= +25%, liq>=$10k. "
                "EXIT: hold 5d, TP 20%, SL 15% (tighter SL)."
            ),
            pump_min=0.40,
            min_rsi=75.0,
            min_vol_spike=4.0,
            min_dist_sma20=0.25,
            hold_days=5,
            take_profit=0.20,
            stop_loss=0.15,
        ),
        RuleSet(
            name="E_MODERATE_FAST",
            description=(
                "ENTRY: +25% <= ret_3d < +70%, RSI >= 70, vol_spike >= 2x, liq>=$10k. "
                "EXIT: hold 2d, TP 12%, SL 18%."
            ),
            pump_min=0.25,
            pump_max=0.70,
            min_rsi=70.0,
            min_vol_spike=2.0,
            min_dist_sma20=None,
            hold_days=2,
            take_profit=0.12,
            stop_loss=0.18,
        ),
        RuleSet(
            name="F_MEGA_ONLY",
            description=(
                "ENTRY: ret_3d >= +70%, vol_spike >= 3x, liq>=$10k. "
                "EXIT: hold 3d, TP 25%, SL 25%."
            ),
            pump_min=0.70,
            min_rsi=None,
            min_vol_spike=3.0,
            min_dist_sma20=None,
            hold_days=3,
            take_profit=0.25,
            stop_loss=0.25,
        ),
        RuleSet(
            name="G_TIME_ONLY_BLOWOFF",
            description=(
                "ENTRY: same as B_BLOWOFF. EXIT: time-only hold 3d (no TP/SL) — pure fade drift."
            ),
            pump_min=0.40,
            min_rsi=70.0,
            min_vol_spike=3.0,
            min_dist_sma20=0.20,
            hold_days=3,
            take_profit=None,
            stop_loss=None,
        ),
        RuleSet(
            name="H_BASE_TIGHT",
            description=(
                "ENTRY: ret_3d >= +25%, liq>=$25k (more liquid). "
                "EXIT: hold 3d, TP 15%, SL 20%."
            ),
            pump_min=0.25,
            min_rsi=None,
            min_vol_spike=None,
            min_dist_sma20=None,
            hold_days=3,
            take_profit=0.15,
            stop_loss=0.20,
            min_vol_usd=25_000,
        ),
    ]


# ---------------------------------------------------------------------------
# Robustness checks
# ---------------------------------------------------------------------------

def walk_forward_years(ohlc: dict[str, list[Candle]], rule: RuleSet) -> dict[str, Any]:
    """IS/OOS by year: train nothing (rules fixed), just report OOS stability."""
    trades = run_rule(ohlc, rule)
    by_y: dict[str, list[Trade]] = {}
    for t in trades:
        by_y.setdefault(t.entry_date[:4], []).append(t)
    out = {}
    for y, ts in sorted(by_y.items()):
        nets = [t.net_pnl for t in ts]
        out[y] = {
            "n": len(ts),
            "mean_net": mean(nets),
            "win_rate": sum(1 for x in nets if x > 0) / len(nets),
            "sum_net": sum(nets),
        }
    return out


def bootstrap_mean(nets: list[float], n_boot: int = 2000, seed: int = 42) -> dict[str, float]:
    """Simple bootstrap CI for mean net pnl."""
    import random
    rng = random.Random(seed)
    if not nets:
        return {}
    means = []
    n = len(nets)
    for _ in range(n_boot):
        sample = [nets[rng.randrange(n)] for _ in range(n)]
        means.append(sum(sample) / n)
    means.sort()
    return {
        "mean": statistics.mean(nets),
        "ci05": means[int(0.05 * n_boot)],
        "ci50": means[int(0.50 * n_boot)],
        "ci95": means[int(0.95 * n_boot)],
        "p_mean_gt_0": sum(1 for m in means if m > 0) / n_boot,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true", help="Re-download OHLC from Kraken")
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ohlc = load_or_fetch(refresh=args.refresh)

    print("\n" + "#" * 96)
    print("FADE BACKTEST — protocole commun")
    print("#" * 96)
    print(
        """
PROTOCOLE (identique pour toutes les règles):
  1. Univers     : paires */USD Kraken, hors BTC/ETH/stables
  2. Timeframe   : daily OHLC
  3. Signal      : close de J (features calculées sans données futures)
  4. Entrée      : SHORT au OPEN de J+1
  5. Taille      : 1 unité notionnelle par trade (pas de compound)
  6. Coûts       : 2 × {fee:.2f}% = {rt:.2f}% round-trip (taker-ish)
  7. SL/TP       : sur high/low journaliers; si SL et TP même jour → SL prioritaire (conservateur)
  8. Pas de stack: 1 position max par paire (prochain signal après sortie)
  9. Liquidité   : filtre vol USD moyen 10j (seuil par règle)
""".format(fee=FEE_RATE * 100, rt=2 * FEE_RATE * 100)
    )

    rules = rulebook()
    all_summaries = []
    all_trades: dict[str, list[dict]] = {}

    for rule in rules:
        print(f"\nRunning {rule.name} ...")
        trades = run_rule(ohlc, rule)
        s = summarize(rule, trades)
        print_summary(s)
        all_summaries.append(s)
        all_trades[rule.name] = [asdict(t) for t in trades]

        if trades:
            boot = bootstrap_mean([t.net_pnl for t in trades])
            print(
                f"  Bootstrap mean net 95% band: [{pct(boot['ci05'])} , {pct(boot['ci95'])}]  "
                f"P(mean>0)={boot['p_mean_gt_0']*100:.1f}%"
            )

    # Leaderboard
    print("\n" + "=" * 96)
    print("LEADERBOARD (trié par expectancy nette / trade)")
    print("=" * 96)
    ranked = sorted(all_summaries, key=lambda x: (x.get("expectancy") is not None, x.get("expectancy") or -999), reverse=True)
    print(f"  {'Rule':<22} {'n':>5} {'WR':>8} {'E[net]':>9} {'Med':>9} {'PF':>6} {'Sum':>10} {'P(m>0)':>8}")
    for s in ranked:
        if s["n"] == 0:
            continue
        boot = bootstrap_mean([t["net_pnl"] for t in all_trades[s["rule"]]])
        pf = f"{s['profit_factor']:.2f}" if s["profit_factor"] else "n/a"
        print(
            f"  {s['rule']:<22} {s['n']:>5} {pct(s['win_rate']):>8} {pct(s['expectancy']):>9} "
            f"{pct(s['med_net']):>9} {pf:>6} {pct(s['sum_net_R']):>10} {boot['p_mean_gt_0']*100:>6.1f}%"
        )

    # Pick best with n>=100 for reliability note
    reliable = [s for s in ranked if s["n"] >= 100]
    best = reliable[0] if reliable else (ranked[0] if ranked else None)

    print("\n" + "=" * 96)
    print("RÈGLE RECOMMANDÉE (meilleure expectancy, n>=100 si possible)")
    print("=" * 96)
    if best:
        print(f"  → {best['rule']}")
        print(f"  {best['description']}")
        print(f"  n={best['n']}  WR={pct(best['win_rate'])}  E[net]={pct(best['expectancy'])}  "
              f"PF={best['profit_factor']:.2f}" if best["profit_factor"] else "")
        # full rule card
        p = best["params"]
        print("\n  RULE CARD (copy-paste):")
        print(f"    IF ret_3d >= {p['pump_min']*100:.0f}%"
              + (f" AND ret_3d < {p['pump_max']*100:.0f}%" if p.get("pump_max") else "")
              + (f" AND RSI14 >= {p['min_rsi']:.0f}" if p.get("min_rsi") is not None else "")
              + (f" AND vol_spike >= {p['min_vol_spike']}x" if p.get("min_vol_spike") is not None else "")
              + (f" AND close >= SMA20 * {1+p['min_dist_sma20']:.2f}" if p.get("min_dist_sma20") is not None else "")
              + f" AND avg_vol_usd_10d >= ${p['min_vol_usd']:,.0f}")
        print(f"    THEN short next open")
        print(f"    EXIT: hold<={p['hold_days']}d"
              + (f" OR TP={p['take_profit']*100:.0f}% down" if p.get("take_profit") else "")
              + (f" OR SL={p['stop_loss']*100:.0f}% up" if p.get("stop_loss") else ""))
        print(f"    COST assumption: {2*FEE_RATE*100:.2f}% round-trip")
    else:
        print("  No trades generated.")

    # Save
    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "protocol": {
            "signal": "close_J",
            "entry": "open_J+1_short",
            "fee_rt": 2 * FEE_RATE,
            "sl_tp_same_day": "SL_priority",
            "position_sizing": "1_unit_no_compound",
            "no_stack_per_pair": True,
        },
        "summaries": all_summaries,
        "leaderboard": [s["rule"] for s in ranked],
        "best": best["rule"] if best else None,
    }
    (OUT_DIR / "fade_backtest_summary.json").write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    # save trades for best only (size)
    if best:
        (OUT_DIR / "fade_trades_best.json").write_text(
            json.dumps(all_trades[best["rule"]], indent=2), encoding="utf-8"
        )
    (OUT_DIR / "fade_trades_all_counts.json").write_text(
        json.dumps({k: len(v) for k, v in all_trades.items()}, indent=2), encoding="utf-8"
    )
    print(f"\nSaved → {OUT_DIR / 'fade_backtest_summary.json'}")
    print("Done.")


if __name__ == "__main__":
    main()
