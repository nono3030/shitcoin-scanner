"""Feature engineering for fade signals (no look-ahead)."""

from __future__ import annotations

from kraken_data import Candle


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
    f: dict[str, float | None] = {
        "close": c.c,
        "open": c.o,
        "high": c.h,
        "low": c.l,
        "volume": c.volume,
        "vol_usd": c.volume * c.c,
        "ts": float(c.ts),
    }
    for lb in (1, 2, 3, 5, 7):
        f[f"ret_{lb}d"] = (closes[-1] / closes[-1 - lb] - 1.0) if len(closes) > lb else None
    s20 = sma(closes, 20)
    f["sma20"] = s20
    f["dist_sma20"] = (closes[-1] / s20 - 1.0) if s20 else None
    f["rsi14"] = rsi(closes, 14)
    if len(vols) >= 21:
        avg = sum(vols[-21:-1]) / 20.0
        f["vol_spike"] = (vols[-1] / avg) if avg > 0 else None
        f["avg_vol_20d"] = avg
    else:
        f["vol_spike"] = None
        f["avg_vol_20d"] = None
    if i >= 9:
        f["avg_vol_usd_10d"] = sum(
            candles[j].volume * candles[j].c for j in range(i - 9, i + 1)
        ) / 10.0
    else:
        f["avg_vol_usd_10d"] = f["vol_usd"]
    return f
