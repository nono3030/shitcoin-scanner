"""Kraken public market data helpers + OHLC cache."""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from config import CACHE_DIR, CACHE_FILE, HTTP_SLEEP, KRAKEN_PUBLIC

EXCLUDE_BASES = {
    "XXBT", "XBT", "BTC", "XETH", "ETH",
    "ZEUR", "ZGBP", "ZUSD", "ZCAD", "ZJPY", "ZAUD", "CHF",
    "USDT", "USDC", "DAI", "EUR", "GBP", "USD", "AUD", "CAD", "JPY",
    "EURQ", "USDQ", "EURR", "USDR", "PYUSD", "USDG", "RLUSD", "AUSD",
    "ETH2", "TBTC", "WBTC",
}


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
    def date(self) -> str:
        return datetime.fromtimestamp(self.ts, tz=timezone.utc).strftime("%Y-%m-%d")


def kraken_get(path: str, params: dict | None = None) -> dict:
    qs = urllib.parse.urlencode(params or {})
    url = f"{KRAKEN_PUBLIC}/{path}" + (f"?{qs}" if qs else "")
    req = urllib.request.Request(url, headers={"User-Agent": "fade-scanner/1.0"})
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
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
        out.append({"pair_key": key, "wsname": ws, "base": base, "altname": v.get("altname") or key})
    return sorted(out, key=lambda x: x["wsname"])


def fetch_ohlc_rows(pair_key: str, interval: int = 1440) -> list[list]:
    data = kraken_get("OHLC", {"pair": pair_key, "interval": interval})
    if data.get("error"):
        return []
    result = data.get("result") or {}
    sk = next((k for k in result if k != "last"), None)
    if not sk:
        return []
    return result[sk]


def rows_to_candles(rows: list[list]) -> list[Candle]:
    return [
        Candle(int(r[0]), float(r[1]), float(r[2]), float(r[3]),
               float(r[4]), float(r[5]), float(r[6]), int(r[7]))
        for r in rows
    ]


def download_universe(pairs: list[dict] | None = None) -> dict[str, list[Candle]]:
    pairs = pairs or load_usd_alt_pairs()
    ohlc: dict[str, list[Candle]] = {}
    for i, p in enumerate(pairs, 1):
        rows = fetch_ohlc_rows(p["pair_key"])
        candles = rows_to_candles(rows)
        if len(candles) >= 40:
            ohlc[p["wsname"]] = candles
        if i % 25 == 0 or i == len(pairs):
            print(f"  fetch {i}/{len(pairs)} kept={len(ohlc)}")
        time.sleep(HTTP_SLEEP)
    return ohlc


def save_cache(ohlc: dict[str, list[Candle]], pair_meta: dict[str, dict] | None = None) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "ohlc": {
            name: [[c.ts, c.o, c.h, c.l, c.c, c.vwap, c.volume, c.count] for c in series]
            for name, series in ohlc.items()
        },
        "pair_meta": pair_meta or {},
    }
    CACHE_FILE.write_text(json.dumps(payload), encoding="utf-8")


def load_cache() -> tuple[dict[str, list[Candle]], dict]:
    raw = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    ohlc_raw = raw.get("ohlc", raw)
    ohlc = {name: rows_to_candles(rows) for name, rows in ohlc_raw.items()}
    return ohlc, raw


def load_or_refresh(refresh: bool = False) -> tuple[dict[str, list[Candle]], list[dict]]:
    pairs = load_usd_alt_pairs()
    meta = {p["wsname"]: p for p in pairs}
    if CACHE_FILE.exists() and not refresh:
        print(f"Loading cache {CACHE_FILE}")
        ohlc, _ = load_cache()
        print(f"  {len(ohlc)} series")
        return ohlc, pairs
    # On Fly/cloud dashboard cold start: never download full universe unless forced.
    # Set KRAKEN_ALLOW_FULL_DOWNLOAD=1 (or refresh=True from run_daily --refresh).
    import os

    allow = os.environ.get("KRAKEN_ALLOW_FULL_DOWNLOAD", "").strip() in ("1", "true", "yes")
    if not refresh and not allow:
        print(
            f"No OHLC cache at {CACHE_FILE} — skipping full download "
            "(set KRAKEN_ALLOW_FULL_DOWNLOAD=1 or run run_daily.py --refresh)."
        )
        return {}, pairs
    print("Downloading full OHLC universe from Kraken...")
    ohlc = download_universe(pairs)
    save_cache(ohlc, meta)
    print(f"  cached {len(ohlc)} series")
    return ohlc, pairs


def refresh_pairs(ohlc: dict[str, list[Candle]], wsnames: list[str], pairs: list[dict]) -> dict[str, list[Candle]]:
    """Refresh only selected pairs (fast path for daily scan)."""
    by_ws = {p["wsname"]: p for p in pairs}
    updated = dict(ohlc)
    for ws in wsnames:
        p = by_ws.get(ws)
        if not p:
            continue
        rows = fetch_ohlc_rows(p["pair_key"])
        candles = rows_to_candles(rows)
        if len(candles) >= 40:
            updated[ws] = candles
        time.sleep(HTTP_SLEEP)
    return updated
