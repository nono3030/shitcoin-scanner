#!/usr/bin/env python3
"""
Shitcoin scanner (Kraken) + continuation vs fade backtest.

1) Build a mid/low-cap alt universe (excl. majors, stables, fiat)
2) Fetch daily OHLC (~720 days max via public API)
3) Live scan: best 2d / 3d performers
4) Backtest: after a multi-day pump, does price continue or fade?
"""

from __future__ import annotations

import json
import math
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BASE = "https://api.kraken.com/0/public"
SLEEP = 0.35  # polite rate-limit
OUT_DIR = Path(__file__).resolve().parent / "out"

# Exclude majors + stables + fiat / synthetic cash
EXCLUDE_BASES = {
    "XXBT", "XBT", "BTC", "XETH", "ETH", "XXDG",  # majors (DOGE kept optional below)
    "ZEUR", "ZGBP", "ZUSD", "ZCAD", "ZJPY", "ZAUD", "CHF",
    "USDT", "USDC", "DAI", "EUR", "GBP", "USD", "AUD", "CAD", "JPY",
    "EURQ", "USDQ", "EURR", "USDR", "PYUSD", "USDG", "RLUSD", "AUSD",
    "XBT", "ETH2", "TBTC", "WBTC",
}

# Extra quote noise
EXCLUDE_IF_CONTAINS = ("USDUSD", "EURUSD", "AUDUSD", "GBPUSD")

# Pump definition for backtest entries
PUMP_LOOKBACK = 3          # days (close[t] / close[t-3] - 1)
PUMP_MIN_RET = 0.15        # +15% over lookback to count as "pump"
MIN_AVG_VOL_USD = 5_000    # filter illiquid days (approx vol*close)
FORWARD_HORIZONS = (1, 2, 3, 5, 7)

# Live scan
TOP_N = 15
SCAN_WINDOWS = (2, 3)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def kraken_get(path: str, params: dict[str, Any] | None = None) -> dict:
    qs = urllib.parse.urlencode(params or {})
    url = f"{BASE}/{path}" + (f"?{qs}" if qs else "")
    req = urllib.request.Request(url, headers={"User-Agent": "shitcoin-scanner/1.0"})
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode())
            if data.get("error"):
                # soft errors on some pairs
                return data
            return data
        except urllib.error.HTTPError as e:
            if e.code in (429, 520, 521, 522, 523, 524) and attempt < 4:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise
        except Exception:
            if attempt < 4:
                time.sleep(1.0 * (attempt + 1))
                continue
            raise
    return {"error": ["max retries"], "result": {}}


# ---------------------------------------------------------------------------
# Universe
# ---------------------------------------------------------------------------

def load_usd_alt_pairs() -> list[dict]:
    data = kraken_get("AssetPairs")
    if data.get("error"):
        raise RuntimeError(data["error"])
    out = []
    for key, v in data["result"].items():
        ws = v.get("wsname") or ""
        quote = v.get("quote") or ""
        base = v.get("base") or ""
        status = v.get("status") or ""
        if status and status != "online":
            continue
        if not ws.endswith("/USD"):
            continue
        if quote not in ("ZUSD", "USD", "USDC", "USDT") and not str(quote).endswith("USD"):
            # keep pure USD quotes only
            if quote not in ("ZUSD", "USD"):
                continue
        if quote not in ("ZUSD", "USD"):
            continue
        if base in EXCLUDE_BASES:
            continue
        if any(x in key for x in EXCLUDE_IF_CONTAINS):
            continue
        # skip leveraged / weird
        if any(x in ws.upper() for x in ("UP", "DOWN", ".S", "3L", "3S")):
            continue
        out.append(
            {
                "pair_key": key,
                "wsname": ws,
                "base": base,
                "quote": quote,
                "altname": v.get("altname") or key,
            }
        )
    # de-dupe by wsname
    seen = set()
    uniq = []
    for p in out:
        if p["wsname"] in seen:
            continue
        seen.add(p["wsname"])
        uniq.append(p)
    return sorted(uniq, key=lambda x: x["wsname"])


# ---------------------------------------------------------------------------
# OHLC
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

    @property
    def dt(self) -> datetime:
        return datetime.fromtimestamp(self.ts, tz=timezone.utc)


def fetch_ohlc(pair_key: str, interval: int = 1440) -> list[Candle]:
    data = kraken_get("OHLC", {"pair": pair_key, "interval": interval})
    if data.get("error"):
        return []
    result = data.get("result") or {}
    series_key = next((k for k in result if k != "last"), None)
    if not series_key:
        return []
    candles = []
    for row in result[series_key]:
        # [time, open, high, low, close, vwap, volume, count]
        candles.append(
            Candle(
                ts=int(row[0]),
                o=float(row[1]),
                h=float(row[2]),
                l=float(row[3]),
                c=float(row[4]),
                vwap=float(row[5]),
                volume=float(row[6]),
                count=int(row[7]),
            )
        )
    # drop incomplete last candle if same-day partial is noisy — keep it for live scan close
    return candles


# ---------------------------------------------------------------------------
# Features
# ---------------------------------------------------------------------------

def pct(a: float, b: float) -> float:
    if b == 0:
        return float("nan")
    return a / b - 1.0


def sma(vals: list[float], n: int) -> float | None:
    if len(vals) < n:
        return None
    return sum(vals[-n:]) / n


def rsi(closes: list[float], n: int = 14) -> float | None:
    if len(closes) < n + 1:
        return None
    gains, losses = [], []
    for i in range(-n, 0):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_g = sum(gains) / n
    avg_l = sum(losses) / n
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return 100.0 - (100.0 / (1.0 + rs))


def realized_vol(closes: list[float], n: int = 14) -> float | None:
    if len(closes) < n + 1:
        return None
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(-n, 0) if closes[i - 1] > 0]
    if len(rets) < 2:
        return None
    return statistics.pstdev(rets) * math.sqrt(365)


def max_drawdown(closes: list[float]) -> float:
    peak = closes[0]
    mdd = 0.0
    for c in closes:
        peak = max(peak, c)
        if peak > 0:
            mdd = min(mdd, c / peak - 1.0)
    return mdd


def candle_features(candles: list[Candle], i: int) -> dict[str, float | None]:
    """Features at index i using history up to i inclusive."""
    closes = [c.c for c in candles[: i + 1]]
    vols = [c.volume for c in candles[: i + 1]]
    c = candles[i]
    out: dict[str, float | None] = {
        "close": c.c,
        "volume": c.volume,
        "ret_1d": pct(closes[-1], closes[-2]) if len(closes) >= 2 else None,
        "ret_2d": pct(closes[-1], closes[-3]) if len(closes) >= 3 else None,
        "ret_3d": pct(closes[-1], closes[-4]) if len(closes) >= 4 else None,
        "ret_5d": pct(closes[-1], closes[-6]) if len(closes) >= 6 else None,
        "ret_7d": pct(closes[-1], closes[-8]) if len(closes) >= 8 else None,
    }
    s7 = sma(closes, 7)
    s20 = sma(closes, 20)
    out["dist_sma7"] = (closes[-1] / s7 - 1.0) if s7 else None
    out["dist_sma20"] = (closes[-1] / s20 - 1.0) if s20 else None
    out["rsi14"] = rsi(closes, 14)
    out["vol_ann"] = realized_vol(closes, 14)
    # volume spike vs 20d avg
    if len(vols) >= 21:
        avg_v = sum(vols[-21:-1]) / 20
        out["vol_spike"] = (vols[-1] / avg_v) if avg_v > 0 else None
    else:
        out["vol_spike"] = None
    # extension: high of last 3 vs close path (intraday range pressure)
    if i >= 2:
        hh = max(candles[j].h for j in range(i - 2, i + 1))
        ll = min(candles[j].l for j in range(i - 2, i + 1))
        out["range_3d_pct"] = (hh / ll - 1.0) if ll > 0 else None
        out["close_in_range"] = (c.c - ll) / (hh - ll) if hh > ll else None
    else:
        out["range_3d_pct"] = None
        out["close_in_range"] = None
    # approx USD volume
    out["vol_usd"] = c.volume * c.c
    return out


def classify_signal(f: dict[str, float | None]) -> str:
    """
    Heuristic label for live scan (not the backtest itself):
    - CONTINUATION: strong pump but not fully exhausted
    - FADE: extreme extension / overbought / blow-off volume
    - MIXED: in between
    """
    ret3 = f.get("ret_3d")
    rsi_v = f.get("rsi14")
    dist20 = f.get("dist_sma20")
    vol_sp = f.get("vol_spike")
    cir = f.get("close_in_range")
    if ret3 is None:
        return "N/A"

    fade_pts = 0
    cont_pts = 0

    if rsi_v is not None:
        if rsi_v >= 80:
            fade_pts += 2
        elif rsi_v >= 70:
            fade_pts += 1
        elif 45 <= rsi_v <= 65:
            cont_pts += 1
        elif rsi_v < 40:
            cont_pts += 0  # not a pump context

    if dist20 is not None:
        if dist20 >= 0.40:
            fade_pts += 2
        elif dist20 >= 0.25:
            fade_pts += 1
        elif 0.05 <= dist20 <= 0.20:
            cont_pts += 1

    if vol_sp is not None:
        if vol_sp >= 5.0:
            fade_pts += 2  # climax volume often mean-reverts
        elif 1.5 <= vol_sp <= 3.5:
            cont_pts += 1

    if cir is not None:
        if cir >= 0.95:
            fade_pts += 1  # closed at top of range
        elif 0.55 <= cir <= 0.85:
            cont_pts += 1

    if ret3 >= 0.50:
        fade_pts += 1  # parabolic risk
    elif 0.15 <= ret3 <= 0.35:
        cont_pts += 1

    if fade_pts >= cont_pts + 2:
        return "FADE"
    if cont_pts >= fade_pts + 1:
        return "CONTINUATION"
    return "MIXED"


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------

def scan_live(pairs: list[dict], ohlc_map: dict[str, list[Candle]]) -> list[dict]:
    rows = []
    for p in pairs:
        candles = ohlc_map.get(p["pair_key"]) or []
        if len(candles) < 10:
            continue
        # use last closed-ish candle (last entry may be partial day)
        i = len(candles) - 1
        f = candle_features(candles, i)
        # liquidity filter on recent avg
        recent = candles[-10:]
        avg_usd = sum(c.volume * c.c for c in recent) / len(recent)
        if avg_usd < MIN_AVG_VOL_USD:
            continue
        row = {
            "wsname": p["wsname"],
            "pair_key": p["pair_key"],
            "date": candles[i].dt.strftime("%Y-%m-%d"),
            "close": f["close"],
            "ret_1d": f["ret_1d"],
            "ret_2d": f["ret_2d"],
            "ret_3d": f["ret_3d"],
            "ret_7d": f["ret_7d"],
            "rsi14": f["rsi14"],
            "dist_sma20": f["dist_sma20"],
            "vol_spike": f["vol_spike"],
            "close_in_range": f["close_in_range"],
            "avg_vol_usd_10d": avg_usd,
            "signal": classify_signal(f),
        }
        rows.append(row)
    rows.sort(key=lambda r: (r["ret_3d"] is not None, r["ret_3d"] or -999), reverse=True)
    return rows


# ---------------------------------------------------------------------------
# Backtest
# ---------------------------------------------------------------------------

def backtest_pumps(ohlc_map: dict[str, list[Candle]], pair_meta: dict[str, dict]) -> list[dict]:
    """
    For each coin/day: if ret over PUMP_LOOKBACK >= PUMP_MIN_RET,
    record forward returns at horizons.
    Label outcome:
      CONTINUATION if fwd_3d > 0
      FADE if fwd_3d < 0
    """
    events = []
    for pair_key, candles in ohlc_map.items():
        if len(candles) < PUMP_LOOKBACK + max(FORWARD_HORIZONS) + 25:
            continue
        meta = pair_meta.get(pair_key, {})
        for i in range(PUMP_LOOKBACK + 20, len(candles) - max(FORWARD_HORIZONS) - 1):
            f = candle_features(candles, i)
            ret_lb = f.get(f"ret_{PUMP_LOOKBACK}d")
            if ret_lb is None or ret_lb < PUMP_MIN_RET:
                continue
            # liquidity on entry day
            if (f.get("vol_usd") or 0) < MIN_AVG_VOL_USD:
                continue
            entry = candles[i].c
            if entry <= 0:
                continue
            ev = {
                "wsname": meta.get("wsname", pair_key),
                "pair_key": pair_key,
                "date": candles[i].dt.strftime("%Y-%m-%d"),
                "entry_close": entry,
                "pump_ret": ret_lb,
                "rsi14": f.get("rsi14"),
                "dist_sma20": f.get("dist_sma20"),
                "vol_spike": f.get("vol_spike"),
                "close_in_range": f.get("close_in_range"),
                "signal_heuristic": classify_signal(f),
            }
            for h in FORWARD_HORIZONS:
                fut = candles[i + h].c
                ev[f"fwd_{h}d"] = fut / entry - 1.0
            # max adverse / favorable over next 3 days using highs/lows
            window = candles[i + 1 : i + 4]
            if window:
                mfe = max(c.h for c in window) / entry - 1.0
                mae = min(c.l for c in window) / entry - 1.0
                ev["mfe_3d"] = mfe
                ev["mae_3d"] = mae
            # outcome labels
            f3 = ev.get("fwd_3d")
            if f3 is not None:
                ev["outcome_3d"] = "CONTINUATION" if f3 > 0 else "FADE"
            events.append(ev)
    return events


def summarize_backtest(events: list[dict]) -> dict:
    if not events:
        return {"n": 0}

    def safe_mean(xs):
        xs = [x for x in xs if x is not None and not math.isnan(x)]
        return statistics.mean(xs) if xs else None

    def safe_med(xs):
        xs = [x for x in xs if x is not None and not math.isnan(x)]
        return statistics.median(xs) if xs else None

    def hit_rate(xs, thresh=0.0):
        xs = [x for x in xs if x is not None]
        if not xs:
            return None
        return sum(1 for x in xs if x > thresh) / len(xs)

    summary: dict[str, Any] = {"n": len(events)}
    for h in FORWARD_HORIZONS:
        key = f"fwd_{h}d"
        vals = [e[key] for e in events if key in e]
        summary[f"mean_{key}"] = safe_mean(vals)
        summary[f"med_{key}"] = safe_med(vals)
        summary[f"hit_{key}"] = hit_rate(vals)
        summary[f"p25_{key}"] = statistics.quantiles(vals, n=4)[0] if len(vals) >= 4 else None
        summary[f"p75_{key}"] = statistics.quantiles(vals, n=4)[2] if len(vals) >= 4 else None

    cont = [e for e in events if e.get("outcome_3d") == "CONTINUATION"]
    fade = [e for e in events if e.get("outcome_3d") == "FADE"]
    summary["pct_continuation_3d"] = len(cont) / len(events) if events else None
    summary["pct_fade_3d"] = len(fade) / len(events) if events else None
    summary["mean_mfe_3d"] = safe_mean([e.get("mfe_3d") for e in events])
    summary["mean_mae_3d"] = safe_mean([e.get("mae_3d") for e in events])

    # stratify by heuristic signal
    by_sig: dict[str, list] = {}
    for e in events:
        by_sig.setdefault(e.get("signal_heuristic") or "N/A", []).append(e)
    summary["by_signal"] = {}
    for sig, xs in by_sig.items():
        summary["by_signal"][sig] = {
            "n": len(xs),
            "pct_continuation_3d": sum(1 for e in xs if e.get("outcome_3d") == "CONTINUATION") / len(xs),
            "mean_fwd_1d": safe_mean([e.get("fwd_1d") for e in xs]),
            "mean_fwd_3d": safe_mean([e.get("fwd_3d") for e in xs]),
            "med_fwd_3d": safe_med([e.get("fwd_3d") for e in xs]),
            "hit_fwd_3d": hit_rate([e.get("fwd_3d") for e in xs]),
        }

    # stratify by pump size buckets
    buckets = [
        ("15-25%", 0.15, 0.25),
        ("25-40%", 0.25, 0.40),
        ("40-70%", 0.40, 0.70),
        ("70%+", 0.70, 99.0),
    ]
    summary["by_pump_size"] = {}
    for name, lo, hi in buckets:
        xs = [e for e in events if lo <= e["pump_ret"] < hi]
        if not xs:
            continue
        summary["by_pump_size"][name] = {
            "n": len(xs),
            "pct_continuation_3d": sum(1 for e in xs if e.get("outcome_3d") == "CONTINUATION") / len(xs),
            "mean_fwd_3d": safe_mean([e.get("fwd_3d") for e in xs]),
            "med_fwd_3d": safe_med([e.get("fwd_3d") for e in xs]),
            "hit_fwd_3d": hit_rate([e.get("fwd_3d") for e in xs]),
        }

    # RSI buckets
    rsi_buckets = [("RSI<60", 0, 60), ("RSI 60-75", 60, 75), ("RSI 75-85", 75, 85), ("RSI>=85", 85, 200)]
    summary["by_rsi"] = {}
    for name, lo, hi in rsi_buckets:
        xs = [e for e in events if e.get("rsi14") is not None and lo <= e["rsi14"] < hi]
        if not xs:
            continue
        summary["by_rsi"][name] = {
            "n": len(xs),
            "pct_continuation_3d": sum(1 for e in xs if e.get("outcome_3d") == "CONTINUATION") / len(xs),
            "mean_fwd_3d": safe_mean([e.get("fwd_3d") for e in xs]),
            "med_fwd_3d": safe_med([e.get("fwd_3d") for e in xs]),
        }

    # vol spike buckets
    vol_buckets = [("vol x<2", 0, 2), ("vol x2-4", 2, 4), ("vol x4-8", 4, 8), ("vol x>=8", 8, 999)]
    summary["by_vol_spike"] = {}
    for name, lo, hi in vol_buckets:
        xs = [e for e in events if e.get("vol_spike") is not None and lo <= e["vol_spike"] < hi]
        if not xs:
            continue
        summary["by_vol_spike"][name] = {
            "n": len(xs),
            "pct_continuation_3d": sum(1 for e in xs if e.get("outcome_3d") == "CONTINUATION") / len(xs),
            "mean_fwd_3d": safe_mean([e.get("fwd_3d") for e in xs]),
            "med_fwd_3d": safe_med([e.get("fwd_3d") for e in xs]),
        }

    return summary


# ---------------------------------------------------------------------------
# Pretty print
# ---------------------------------------------------------------------------

def pct_str(x: float | None, digits: int = 1) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "  n/a"
    return f"{x * 100:+.{digits}f}%"


def num_str(x: float | None, digits: int = 2) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "n/a"
    return f"{x:.{digits}f}"


def print_scan(rows: list[dict], n: int = TOP_N) -> None:
    print("\n" + "=" * 100)
    print(f"LIVE SCAN — Top {n} shitcoins by 3-day return (Kraken USD, daily)")
    print("=" * 100)
    header = (
        f"{'#':>2} {'Pair':<14} {'Close':>12} {'1d':>8} {'2d':>8} {'3d':>8} {'7d':>8} "
        f"{'RSI':>6} {'vsSMA20':>8} {'VolX':>6} {'Sig':<13} {'Liq10d$':>10}"
    )
    print(header)
    print("-" * len(header))
    for i, r in enumerate(rows[:n], 1):
        print(
            f"{i:>2} {r['wsname']:<14} {r['close']:>12.6g} "
            f"{pct_str(r['ret_1d']):>8} {pct_str(r['ret_2d']):>8} {pct_str(r['ret_3d']):>8} {pct_str(r['ret_7d']):>8} "
            f"{num_str(r['rsi14'],1):>6} {pct_str(r['dist_sma20']):>8} {num_str(r['vol_spike'],1):>6} "
            f"{r['signal']:<13} {r['avg_vol_usd_10d']:>10,.0f}"
        )
    print("\nSignal legend: CONTINUATION = momentum may keep running | FADE = mean-reversion risk | MIXED = unclear")


def print_summary(s: dict) -> None:
    print("\n" + "=" * 100)
    print(
        f"BACKTEST — After +{PUMP_MIN_RET*100:.0f}% pump over {PUMP_LOOKBACK}d, what next? "
        f"(n={s.get('n', 0)} events, Kraken daily OHLC)"
    )
    print("=" * 100)
    if not s.get("n"):
        print("No events.")
        return
    print(f"Continuation rate (fwd 3d > 0): {pct_str(s.get('pct_continuation_3d'))}")
    print(f"Fade rate         (fwd 3d < 0): {pct_str(s.get('pct_fade_3d'))}")
    print(f"Mean MFE 3d (best high):        {pct_str(s.get('mean_mfe_3d'))}")
    print(f"Mean MAE 3d (worst low):        {pct_str(s.get('mean_mae_3d'))}")
    print("\nForward returns (all pump events):")
    for h in FORWARD_HORIZONS:
        print(
            f"  +{h}d  mean={pct_str(s.get(f'mean_fwd_{h}d'))}  "
            f"med={pct_str(s.get(f'med_fwd_{h}d'))}  "
            f"hit={pct_str(s.get(f'hit_fwd_{h}d'))}  "
            f"p25={pct_str(s.get(f'p25_fwd_{h}d'))}  p75={pct_str(s.get(f'p75_fwd_{h}d'))}"
        )

    def print_group(title: str, block: dict) -> None:
        print(f"\n{title}")
        print(f"  {'bucket':<14} {'n':>5} {'cont%':>8} {'mean+3d':>9} {'med+3d':>9} {'hit+3d':>8}")
        for name, v in block.items():
            print(
                f"  {name:<14} {v['n']:>5} {pct_str(v.get('pct_continuation_3d')):>8} "
                f"{pct_str(v.get('mean_fwd_3d')):>9} {pct_str(v.get('med_fwd_3d')):>9} "
                f"{pct_str(v.get('hit_fwd_3d')):>8}"
            )

    if s.get("by_signal"):
        print("\nBy heuristic signal (does the live classifier help?):")
        print(f"  {'signal':<14} {'n':>5} {'cont%':>8} {'mean+1d':>9} {'mean+3d':>9} {'med+3d':>9} {'hit+3d':>8}")
        for name, v in s["by_signal"].items():
            print(
                f"  {name:<14} {v['n']:>5} {pct_str(v.get('pct_continuation_3d')):>8} "
                f"{pct_str(v.get('mean_fwd_1d')):>9} {pct_str(v.get('mean_fwd_3d')):>9} "
                f"{pct_str(v.get('med_fwd_3d')):>9} {pct_str(v.get('hit_fwd_3d')):>8}"
            )
    if s.get("by_pump_size"):
        print_group("By pump size:", s["by_pump_size"])
    if s.get("by_rsi"):
        print_group("By RSI at entry:", s["by_rsi"])
    if s.get("by_vol_spike"):
        print_group("By volume spike:", s["by_vol_spike"])


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print("Loading Kraken USD pairs...")
    pairs = load_usd_alt_pairs()
    print(f"  Universe candidates: {len(pairs)} USD alts")

    # Fetch OHLC for all (can take a few minutes). Optionally subsample for speed.
    # For a solid backtest we want breadth; rate-limited ~3 req/s.
    ohlc_map: dict[str, list[Candle]] = {}
    pair_meta: dict[str, dict] = {}
    errors = 0
    for i, p in enumerate(pairs, 1):
        pair_meta[p["pair_key"]] = p
        try:
            candles = fetch_ohlc(p["pair_key"], interval=1440)
            if len(candles) >= 30:
                ohlc_map[p["pair_key"]] = candles
        except Exception as e:
            errors += 1
            if errors <= 5:
                print(f"  warn {p['wsname']}: {e}")
        if i % 25 == 0 or i == len(pairs):
            print(f"  fetched {i}/{len(pairs)} — kept {len(ohlc_map)} series")
        time.sleep(SLEEP)

    print(f"\nOHLC series ready: {len(ohlc_map)} (errors soft-skipped ~{errors})")

    # Live scan
    scan_rows = scan_live(pairs, ohlc_map)
    print_scan(scan_rows, TOP_N)

    # Also show top by 2d
    by2 = sorted(scan_rows, key=lambda r: (r["ret_2d"] is not None, r["ret_2d"] or -999), reverse=True)
    print("\n" + "-" * 100)
    print(f"Top {TOP_N} by 2-day return:")
    for i, r in enumerate(by2[:TOP_N], 1):
        print(
            f"  {i:>2}. {r['wsname']:<14} 2d={pct_str(r['ret_2d'])}  3d={pct_str(r['ret_3d'])}  "
            f"RSI={num_str(r['rsi14'],1)}  sig={r['signal']}"
        )

    # Backtest
    print("\nRunning pump continuation/fade backtest...")
    events = backtest_pumps(ohlc_map, pair_meta)
    summary = summarize_backtest(events)
    print_summary(summary)

    # Live top names: historical conditional stats when similar
    top_names = {r["wsname"] for r in scan_rows[:TOP_N]}
    print("\n" + "=" * 100)
    print("PER-COIN history: when THIS coin pumped +15% / 3d, what happened next? (if enough events)")
    print("=" * 100)
    for name in [r["wsname"] for r in scan_rows[:TOP_N]]:
        xs = [e for e in events if e["wsname"] == name]
        if len(xs) < 5:
            print(f"  {name:<14} n={len(xs):<4} (insufficient history)")
            continue
        cont = sum(1 for e in xs if e.get("outcome_3d") == "CONTINUATION") / len(xs)
        mean3 = statistics.mean([e["fwd_3d"] for e in xs if "fwd_3d" in e])
        med3 = statistics.median([e["fwd_3d"] for e in xs if "fwd_3d" in e])
        print(f"  {name:<14} n={len(xs):<4} cont%={pct_str(cont)}  mean+3d={pct_str(mean3)}  med+3d={pct_str(med3)}")

    # Save artifacts
    def dump(path: Path, obj: Any) -> None:
        path.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")

    dump(OUT_DIR / "scan_live.json", scan_rows[:50])
    dump(OUT_DIR / "backtest_summary.json", summary)
    # trim events for size
    dump(OUT_DIR / "backtest_events_sample.json", events[:500])
    dump(
        OUT_DIR / "meta.json",
        {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "n_pairs_universe": len(pairs),
            "n_ohlc": len(ohlc_map),
            "n_events": len(events),
            "pump_lookback": PUMP_LOOKBACK,
            "pump_min_ret": PUMP_MIN_RET,
            "min_avg_vol_usd": MIN_AVG_VOL_USD,
        },
    )
    print(f"\nSaved JSON under {OUT_DIR}")
    print("Done.")


if __name__ == "__main__":
    main()
