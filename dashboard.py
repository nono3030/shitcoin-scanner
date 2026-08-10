#!/usr/bin/env python3
"""
Dashboard perfs + charts style TradingView (Lightweight Charts).

Live mode: state local (live/) merged with Bybit exchange (source de vérité).

  python dashboard.py              # génère + ouvre
  python dashboard.py --serve      # http://127.0.0.1:8765  (recommandé — refresh live)
  python dashboard.py --no-open
"""

from __future__ import annotations

import argparse
import json
import math
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from config import (
    BYBIT_ENV,
    COMPOUNDING,
    EQUITY_USD,
    EXECUTION_MODE,
    FEE_RT,
    LIVE_STATE,
    MAX_OPEN_POSITIONS,
    MIN_EQUITY_USD,
    OUT_DIR,
    PAPER_EQUITY_USD,
    PAPER_STATE,
    PROFILE_NAME,
    SIGNALS_FILE,
    active_rule,
    position_notional,
    profile_summary,
)
from kraken_data import Candle, load_or_refresh

DASH_HTML = OUT_DIR / "dashboard.html"
# Fly/Railway inject PORT; bind 0.0.0.0 in production so the proxy can reach us.
PORT = int(__import__("os").environ.get("PORT", "8765"))
BIND_HOST = __import__("os").environ.get("BIND_HOST", "127.0.0.1")
CHART_BARS = 120  # daily bars shown on chart


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

def _load_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _short_pnl(entry: float, last: float) -> float:
    if not entry:
        return 0.0
    return (entry - last) / entry


def _is_live() -> bool:
    return (EXECUTION_MODE or "").strip().lower() == "live"


def _symbol_to_pair(symbol: str) -> str:
    s = (symbol or "").upper()
    if s.endswith("USDT"):
        return f"{s[:-4]}/USD"
    if s.endswith("USD"):
        return f"{s[:-3]}/USD"
    return s


def fetch_bybit_snapshot() -> dict[str, Any]:
    """Live snapshot from Bybit API (AI sub-account of the API keys)."""
    snap: dict[str, Any] = {
        "ok": False,
        "error": None,
        "equity_usdt": None,
        "positions": [],
        "orders": [],
        "api_note": None,
        "api_uid": None,
        "env": BYBIT_ENV,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        from broker_bybit import default_broker

        br = default_broker()
        snap["env"] = br.env
        snap["equity_usdt"] = float(br.get_equity_usdt())
        raw_pos = br.list_open_positions()
        # strip heavy raw
        snap["positions"] = [
            {
                "symbol": p.get("symbol"),
                "side": p.get("side"),
                "size": p.get("size"),
                "avgPrice": p.get("avgPrice"),
                "markPrice": p.get("markPrice"),
                "unrealisedPnl": p.get("unrealisedPnl"),
                "leverage": p.get("leverage"),
            }
            for p in raw_pos
        ]
        try:
            oh = br.private_get(
                "/v5/order/history",
                {"category": "linear", "settleCoin": "USDT", "limit": "20"},
            )
            rows = (oh.get("result") or {}).get("list") or []
            snap["orders"] = [
                {
                    "orderId": o.get("orderId"),
                    "symbol": o.get("symbol"),
                    "side": o.get("side"),
                    "orderType": o.get("orderType"),
                    "qty": o.get("qty"),
                    "avgPrice": o.get("avgPrice"),
                    "orderStatus": o.get("orderStatus"),
                    "createdTime": o.get("createdTime"),
                    "updatedTime": o.get("updatedTime"),
                    "reduceOnly": o.get("reduceOnly"),
                }
                for o in rows
            ]
        except Exception as e:
            snap["orders_error"] = str(e)

        try:
            meta = br.private_get("/v5/user/query-api")
            res = meta.get("result") or {}
            snap["api_note"] = res.get("note")
            snap["api_uid"] = res.get("userID") or res.get("uid")
            snap["api_is_master"] = res.get("isMaster")
            snap["api_parent_uid"] = res.get("parentUid")
        except Exception:
            pass

        snap["ok"] = True
    except Exception as e:
        snap["error"] = str(e)
    return snap


def enrich_positions(state: dict, ohlc: dict | None) -> list[dict]:
    rows = []
    for p in state.get("positions") or []:
        r = dict(p)
        r["u_pnl_pct"] = None
        r["u_pnl_usd"] = None
        r["last_px"] = None
        r["last_date"] = None
        r["progress"] = None
        r["exchange_synced"] = None
        r["pnl_source"] = "local"

        if r.get("status") == "open" and ohlc and r.get("pair") in ohlc and r.get("entry_px"):
            series = ohlc[r["pair"]]
            entry_date = r.get("entry_date") or ""
            bars = [c for c in series if c.date >= entry_date]
            if bars:
                last = bars[-1]
                r["last_px"] = last.c
                r["last_date"] = last.date
                r["bars_held"] = len(bars)
                gross = _short_pnl(float(r["entry_px"]), last.c)
                net = gross - FEE_RT / 2
                r["u_pnl_pct"] = net
                r["u_pnl_usd"] = net * float(r.get("notional_usd") or 0)
                r["pnl_source"] = "kraken_ohlc"
        elif r.get("status") == "closed":
            r["u_pnl_pct"] = r.get("realized_pnl_pct")
            r["u_pnl_usd"] = r.get("realized_pnl_usd")
            r["last_px"] = r.get("exit_px")
            r["last_date"] = r.get("exit_date")
            r["pnl_source"] = "closed"

        hd = max(1, int(r.get("hold_days") or 3))
        bh = int(r.get("bars_held") or 0)
        r["progress"] = min(1.0, bh / hd)
        rows.append(r)
    return rows


def merge_exchange_into_positions(
    positions: list[dict],
    exchange_positions: list[dict],
) -> tuple[list[dict], list[dict]]:
    """
    Overlay Bybit mark/avg/uPnL onto open book rows.
    Returns (merged_positions, orphan_exchange_rows).
    Exchange uPnL is source of truth for open trades when matched.
    """
    by_sym = {(p.get("symbol") or "").upper(): p for p in exchange_positions if p.get("symbol")}
    used: set[str] = set()
    out: list[dict] = []

    for r in positions:
        row = dict(r)
        sym = (row.get("bybit_symbol") or "").upper()
        if not sym and row.get("pair"):
            # best-effort map
            pair = row["pair"].upper().replace(" ", "")
            if "/" in pair:
                sym = pair.split("/")[0] + "USDT"
            row["bybit_symbol"] = sym

        if row.get("status") == "open" and sym and sym in by_sym:
            ep = by_sym[sym]
            used.add(sym)
            row["exchange_synced"] = True
            row["sync_status"] = "matched"
            row["exchange_size"] = ep.get("size")
            row["exchange_side"] = ep.get("side")
            row["exchange_avg"] = ep.get("avgPrice")
            row["exchange_mark"] = ep.get("markPrice")
            row["exchange_upnl"] = ep.get("unrealisedPnl")
            row["exchange_leverage"] = ep.get("leverage")
            if ep.get("markPrice"):
                row["last_px"] = ep["markPrice"]
            if ep.get("avgPrice") and not row.get("entry_px"):
                row["entry_px"] = ep["avgPrice"]
            # Prefer exchange entry avg for display consistency with Bybit app
            if ep.get("avgPrice"):
                row["entry_px_display"] = ep["avgPrice"]
            else:
                row["entry_px_display"] = row.get("entry_px")
            upnl = float(ep.get("unrealisedPnl") or 0)
            row["u_pnl_usd"] = upnl
            notion = float(row.get("notional_usd") or 0)
            if notion > 0:
                row["u_pnl_pct"] = upnl / notion
            elif row.get("entry_px") and ep.get("markPrice"):
                row["u_pnl_pct"] = _short_pnl(float(row["entry_px"]), float(ep["markPrice"]))
            row["pnl_source"] = "bybit_exchange"
        elif row.get("status") == "open":
            row["exchange_synced"] = False
            row["sync_status"] = "local_only"
            row["entry_px_display"] = row.get("entry_px")
            row["sync_warning"] = "open_in_book_not_on_exchange"
        else:
            row["entry_px_display"] = row.get("entry_px")
            if row.get("status") == "closed":
                row["sync_status"] = "closed"

        out.append(row)

    orphans: list[dict] = []
    for sym, ep in by_sym.items():
        if sym in used:
            continue
        pair = _symbol_to_pair(sym)
        size = float(ep.get("size") or 0)
        avg = float(ep.get("avgPrice") or 0) or None
        mark = float(ep.get("markPrice") or 0) or None
        upnl = float(ep.get("unrealisedPnl") or 0)
        notion = (size * (avg or mark or 0)) if size else None
        orphans.append(
            {
                "id": f"ex-{sym}",
                "pair": pair,
                "bybit_symbol": sym,
                "status": "open",
                "side": "short" if (ep.get("side") or "").lower() == "sell" else "long",
                "entry_px": avg,
                "entry_px_display": avg,
                "last_px": mark,
                "qty": size,
                "exchange_size": size,
                "exchange_side": ep.get("side"),
                "exchange_avg": avg,
                "exchange_mark": mark,
                "exchange_upnl": upnl,
                "exchange_leverage": ep.get("leverage"),
                "notional_usd": notion,
                "u_pnl_usd": upnl,
                "u_pnl_pct": (upnl / notion) if notion else None,
                "hold_days": None,
                "bars_held": None,
                "progress": None,
                "exchange_synced": True,
                "sync_status": "exchange_only",
                "sync_warning": "on_exchange_not_in_live_book",
                "pnl_source": "bybit_exchange",
            }
        )
        out.append(orphans[-1])

    return out, orphans


def stats_from_positions(
    positions: list[dict],
    cash_pnl: float,
    equity_start: float,
    *,
    live_equity: float | None = None,
    exchange_upnl: float | None = None,
) -> dict:
    open_p = [p for p in positions if p.get("status") == "open"]
    closed = [p for p in positions if p.get("status") == "closed"]
    pending = [p for p in positions if p.get("status") == "pending"]

    u_pnl = (
        float(exchange_upnl)
        if exchange_upnl is not None
        else sum(float(p.get("u_pnl_usd") or 0) for p in open_p)
    )
    realized = cash_pnl
    if live_equity is not None:
        equity = float(live_equity)
        total_pnl = equity - equity_start
        # implied realized ≈ equity - start - uPnL (fees/funding included in equity)
        implied_realized = equity - equity_start - u_pnl
        realized_display = implied_realized
    else:
        equity = equity_start + realized + u_pnl
        total_pnl = realized + u_pnl
        realized_display = realized

    closed_pnls = [p.get("realized_pnl_usd") or 0 for p in closed]
    wins = [x for x in closed_pnls if x > 0]
    losses = [x for x in closed_pnls if x <= 0]

    curve = []
    if closed:
        ordered = sorted(closed, key=lambda p: p.get("exit_date") or p.get("entry_date") or "")
        eq = equity_start
        curve.append({"t": "start", "eq": eq})
        for p in ordered:
            eq += p.get("realized_pnl_usd") or 0
            curve.append({
                "t": p.get("exit_date") or "?",
                "eq": eq,
                "pair": p.get("pair"),
                "pnl": p.get("realized_pnl_usd") or 0,
            })
    if live_equity is not None:
        if not curve:
            curve = [{"t": "start", "eq": equity_start}]
        curve.append({"t": "now", "eq": live_equity, "pair": "BYBIT", "pnl": 0})

    eq_for_size = live_equity if (live_equity and COMPOUNDING) else equity_start

    return {
        "equity_start": equity_start,
        "equity_now": equity,
        "live_equity": live_equity,
        "realized_pnl": realized_display,
        "book_cash_pnl": cash_pnl,
        "unrealized_pnl": u_pnl,
        "total_pnl": total_pnl,
        "total_pnl_pct": total_pnl / equity_start if equity_start else 0,
        "n_open": len(open_p),
        "n_closed": len(closed),
        "n_pending": len(pending),
        "win_rate": len(wins) / len(closed) if closed else None,
        "avg_win": sum(wins) / len(wins) if wins else None,
        "avg_loss": sum(losses) / len(losses) if losses else None,
        "best_trade": max(closed_pnls) if closed_pnls else None,
        "worst_trade": min(closed_pnls) if closed_pnls else None,
        "sum_closed": sum(closed_pnls) if closed_pnls else 0.0,
        "curve": curve,
        "notional_per_trade": position_notional(eq_for_size or equity_start),
        "max_open": MAX_OPEN_POSITIONS,
        "min_equity": MIN_EQUITY_USD,
    }


def load_backtest_highlights() -> dict | None:
    raw = _load_json(OUT_DIR / "fade_backtest_summary.json")
    if not raw:
        return None
    summaries = raw.get("summaries") or []
    best = next((s for s in summaries if s.get("rule") == "G_TIME_ONLY_BLOWOFF"), None)
    if not best and summaries:
        best = max(summaries, key=lambda x: x.get("expectancy") or -999)
    if not best:
        return None
    return {
        "rule": best.get("rule"),
        "n": best.get("n"),
        "win_rate": best.get("win_rate"),
        "expectancy": best.get("expectancy"),
        "med_net": best.get("med_net"),
        "profit_factor": best.get("profit_factor"),
        "by_year": best.get("by_year") or {},
        "description": best.get("description"),
    }


def candles_to_tv(series: list[Candle], n: int = CHART_BARS) -> list[dict]:
    """TradingView Lightweight Charts candle format (time = unix seconds UTC)."""
    out = []
    for c in series[-n:]:
        out.append({
            "time": c.ts,
            "open": c.o,
            "high": c.h,
            "low": c.l,
            "close": c.c,
        })
    return out


def volume_to_tv(series: list[Candle], n: int = CHART_BARS) -> list[dict]:
    out = []
    for c in series[-n:]:
        color = "rgba(61,214,140,0.45)" if c.c >= c.o else "rgba(240,113,120,0.45)"
        out.append({"time": c.ts, "value": c.volume, "color": color})
    return out


def date_to_ts(series: list[Candle], date: str | None) -> int | None:
    if not date:
        return None
    for c in series:
        if c.date == date:
            return c.ts
    # fallback: parse as noon UTC-ish via first bar on/after
    for c in series:
        if c.date >= date:
            return c.ts
    return None


def build_chart_bundle(
    ohlc: dict[str, list[Candle]] | None,
    positions: list[dict],
    signals: list[dict],
) -> dict[str, Any]:
    """
    charts[pair] = {
      candles, volumes,
      markers: [{time, position, color, shape, text}],
      lines: [{price, title, color}],
      trades: [...]
    }
    """
    if not ohlc:
        return {}

    pairs: set[str] = set()
    for p in positions:
        if p.get("pair"):
            pairs.add(p["pair"])
    for s in signals[:15]:
        if s.get("pair"):
            pairs.add(s["pair"])

    charts: dict[str, Any] = {}
    for pair in sorted(pairs):
        series = ohlc.get(pair)
        if not series or len(series) < 10:
            continue

        pair_trades = [p for p in positions if p.get("pair") == pair]
        markers = []
        price_lines = []

        for t in pair_trades:
            sig_ts = date_to_ts(series, t.get("signal_date"))
            ent_ts = date_to_ts(series, t.get("entry_date"))
            ext_ts = date_to_ts(series, t.get("exit_date"))

            if sig_ts:
                markers.append({
                    "time": sig_ts,
                    "position": "aboveBar",
                    "color": "#f0b429",
                    "shape": "circle",
                    "text": "SIG",
                })
            if ent_ts and t.get("entry_px"):
                markers.append({
                    "time": ent_ts,
                    "position": "aboveBar",
                    "color": "#f07178",
                    "shape": "arrowDown",
                    "text": f"SHORT {t.get('entry_px'):.4g}",
                })
                price_lines.append({
                    "price": float(t["entry_px"]),
                    "color": "#f07178",
                    "title": f"Entry {t.get('id', '')}",
                    "lineWidth": 1,
                    "lineStyle": 2,  # dashed
                    "axisLabelVisible": True,
                })
            if ext_ts and t.get("exit_px"):
                pnl = t.get("realized_pnl_pct")
                pnl_s = f" {pnl*100:+.1f}%" if pnl is not None else ""
                markers.append({
                    "time": ext_ts,
                    "position": "belowBar",
                    "color": "#3dd68c",
                    "shape": "arrowUp",
                    "text": f"EXIT{pnl_s}",
                })

        # signal-only markers from latest scan (if no position markers same day)
        for s in signals:
            if s.get("pair") != pair:
                continue
            sts = date_to_ts(series, s.get("signal_date"))
            if not sts:
                continue
            # avoid dup if already marked
            if any(m["time"] == sts and m.get("text") == "SIG" for m in markers):
                continue
            markers.append({
                "time": sts,
                "position": "aboveBar",
                "color": "#5b9dff",
                "shape": "circle",
                "text": "SCAN",
            })

        # sort markers by time (required by LWC)
        markers.sort(key=lambda m: m["time"])

        charts[pair] = {
            "candles": candles_to_tv(series),
            "volumes": volume_to_tv(series),
            "markers": markers,
            "priceLines": price_lines,
            "trades": [
                {
                    "id": t.get("id"),
                    "status": t.get("status"),
                    "signal_date": t.get("signal_date"),
                    "entry_date": t.get("entry_date"),
                    "exit_date": t.get("exit_date"),
                    "entry_px": t.get("entry_px"),
                    "exit_px": t.get("exit_px") or t.get("last_px"),
                    "pnl_pct": t.get("u_pnl_pct") if t.get("status") != "closed" else t.get("realized_pnl_pct"),
                    "pnl_usd": t.get("u_pnl_usd") if t.get("status") != "closed" else t.get("realized_pnl_usd"),
                    "hold_days": t.get("hold_days"),
                    "bars_held": t.get("bars_held"),
                }
                for t in pair_trades
            ],
            "last_close": series[-1].c,
            "last_date": series[-1].date,
        }
    return charts


def build_payload(use_ohlc: bool = True) -> dict:
    live = _is_live()
    if live:
        state = _load_json(
            LIVE_STATE,
            {
                "equity_start": EQUITY_USD,
                "cash_pnl": 0.0,
                "positions": [],
                "killed": False,
            },
        )
        book_label = "live"
        state_path = str(LIVE_STATE)
    else:
        state = _load_json(
            PAPER_STATE,
            {
                "equity_start": PAPER_EQUITY_USD,
                "cash_pnl": 0.0,
                "positions": [],
            },
        )
        book_label = "paper"
        state_path = str(PAPER_STATE)

    ohlc = None
    if use_ohlc:
        try:
            ohlc, _ = load_or_refresh(refresh=False)
        except Exception as e:
            print(f"warn: OHLC load failed ({e})")

    positions = enrich_positions(state or {}, ohlc)

    exchange: dict[str, Any] = {
        "ok": False,
        "skipped": True,
        "error": None,
        "equity_usdt": None,
        "positions": [],
        "orders": [],
    }
    orphans: list[dict] = []
    if live:
        exchange = fetch_bybit_snapshot()
        exchange["skipped"] = False
        if exchange.get("ok"):
            positions, orphans = merge_exchange_into_positions(
                positions, exchange.get("positions") or []
            )
            print(
                f"  bybit ok equity=${exchange.get('equity_usdt'):.4f} "
                f"ex_pos={len(exchange.get('positions') or [])} "
                f"orders={len(exchange.get('orders') or [])}"
            )
        else:
            print(f"  bybit ERROR: {exchange.get('error')}")

    ex_upnl = None
    live_eq = None
    if live and exchange.get("ok"):
        live_eq = float(exchange.get("equity_usdt") or 0)
        ex_upnl = sum(float(p.get("unrealisedPnl") or 0) for p in (exchange.get("positions") or []))

    equity_start = float((state or {}).get("equity_start") or (EQUITY_USD if live else PAPER_EQUITY_USD))
    stats = stats_from_positions(
        positions,
        cash_pnl=float((state or {}).get("cash_pnl") or 0),
        equity_start=equity_start,
        live_equity=live_eq,
        exchange_upnl=ex_upnl,
    )

    # sync health
    open_rows = [p for p in positions if p.get("status") == "open"]
    n_matched = sum(1 for p in open_rows if p.get("sync_status") == "matched")
    n_local_only = sum(1 for p in open_rows if p.get("sync_status") == "local_only")
    n_ex_only = sum(1 for p in open_rows if p.get("sync_status") == "exchange_only")
    stats["n_matched"] = n_matched
    stats["n_local_only"] = n_local_only
    stats["n_exchange_only"] = n_ex_only
    stats["sync_ok"] = bool(
        (not live)
        or (exchange.get("ok") and n_local_only == 0 and n_ex_only == 0)
    )

    signals_file = _load_json(SIGNALS_FILE, {}) or {}
    signals = signals_file.get("signals") or []
    charts = build_chart_bundle(ohlc, positions, signals)
    bt = load_backtest_highlights()

    open_pairs = [
        p["pair"] for p in positions if p.get("status") == "open" and p.get("pair") in charts
    ]
    default_pair = open_pairs[0] if open_pairs else (next(iter(charts), None))

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "live" if live else "paper",
        "book": book_label,
        "state_path": state_path,
        "profile": profile_summary(),
        "profile_name": PROFILE_NAME,
        "rule": active_rule().describe(),
        "killed": bool((state or {}).get("killed")),
        "kill_reason": (state or {}).get("kill_reason"),
        "stats": stats,
        "positions": positions,
        "exchange": {
            "ok": exchange.get("ok"),
            "error": exchange.get("error"),
            "equity_usdt": exchange.get("equity_usdt"),
            "env": exchange.get("env"),
            "api_note": exchange.get("api_note"),
            "api_uid": exchange.get("api_uid"),
            "api_is_master": exchange.get("api_is_master"),
            "api_parent_uid": exchange.get("api_parent_uid"),
            "fetched_at": exchange.get("fetched_at"),
            "positions": exchange.get("positions") or [],
            "orders": exchange.get("orders") or [],
            "n_positions": len(exchange.get("positions") or []),
            "n_orders": len(exchange.get("orders") or []),
            "orphans": orphans,
        },
        "signals": signals,
        "near_misses": (signals_file.get("near_misses") or [])[:10],
        "signals_generated_at": signals_file.get("generated_at"),
        "backtest": bt,
        "charts": charts,
        "default_pair": default_pair,
        "chart_pairs": list(charts.keys()),
    }


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

def _pct(x: float | None, d: int = 2) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "—"
    return f"{x * 100:+.{d}f}%"


def _usd_plain(x: float | None, d: int = 2) -> str:
    if x is None:
        return "—"
    if abs(x) < 1e-12:
        return f"${0:.{d}f}"
    return f"${x:+,.{d}f}"


def _cls(x: float | None) -> str:
    if x is None:
        return "muted"
    if x > 0:
        return "pos"
    if x < 0:
        return "neg"
    return "muted"


def _px(x: float | None) -> str:
    if x is None:
        return "—"
    if x >= 1:
        return f"{x:.4g}"
    return f"{x:.6g}"


def _ms_utc(ms: str | int | None) -> str:
    if not ms:
        return "—"
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).strftime("%m-%d %H:%M")
    except Exception:
        return str(ms)


def render_html(data: dict, auto_refresh: bool = False) -> str:
    s = data["stats"]
    bt = data.get("backtest")
    ex = data.get("exchange") or {}
    mode = data.get("mode") or "paper"
    live = mode == "live"
    # auto-refresh more often in live serve mode
    refresh_meta = (
        '<meta http-equiv="refresh" content="30">' if auto_refresh and live
        else ('<meta http-equiv="refresh" content="60">' if auto_refresh else "")
    )
    gen = data["generated_at"].replace("T", " ")[:19] + " UTC"
    # Embed chart JSON safely
    charts_json = json.dumps(data.get("charts") or {}, separators=(",", ":"))
    default_pair = json.dumps(data.get("default_pair"))
    chart_pairs = data.get("chart_pairs") or []

    pair_options = "\n".join(
        f'<option value="{p}"{" selected" if p == data.get("default_pair") else ""}>{p}</option>'
        for p in chart_pairs
    )
    if not pair_options:
        pair_options = '<option value="">— aucun —</option>'

    curve = s.get("curve") or []
    spark = ""
    if len(curve) >= 2:
        vals = [c["eq"] for c in curve]
        mn, mx = min(vals), max(vals)
        span = (mx - mn) or 1.0
        w, h = 280, 64
        pts = []
        for i, v in enumerate(vals):
            x = i / (len(vals) - 1) * (w - 4) + 2
            y = h - 4 - ((v - mn) / span) * (h - 8)
            pts.append(f"{x:.1f},{y:.1f}")
        color = "#3dd68c" if vals[-1] >= vals[0] else "#f07178"
        spark = f'''<svg viewBox="0 0 {w} {h}" class="spark" preserveAspectRatio="none">
          <polyline fill="none" stroke="{color}" stroke-width="2.5" points="{" ".join(pts)}" />
        </svg>'''
    else:
        spark = '<div class="spark empty">Courbe dispo après 1er trade fermé</div>'

    def pos_rows(status: str) -> str:
        xs = [p for p in data["positions"] if p.get("status") == status]
        cols = 12 if live else 10
        if not xs:
            return f'<tr><td colspan="{cols}" class="muted">Aucune position {status}</td></tr>'
        if status == "open":
            xs = sorted(xs, key=lambda p: p.get("u_pnl_usd") or 0, reverse=True)
        else:
            xs = sorted(xs, key=lambda p: p.get("exit_date") or "", reverse=True)
        html = []
        for p in xs:
            pnl = p.get("u_pnl_usd") if status != "closed" else p.get("realized_pnl_usd")
            pnl_pct = p.get("u_pnl_pct") if status != "closed" else p.get("realized_pnl_pct")
            prog = int((p.get("progress") or 0) * 100) if p.get("progress") is not None else 0
            pair = p.get("pair") or ""
            sym = p.get("bybit_symbol") or "—"
            sync = p.get("sync_status") or ("—" if not live else "?")
            sync_cls = {
                "matched": "pos",
                "local_only": "neg",
                "exchange_only": "neg",
                "closed": "muted",
            }.get(sync, "muted")
            entry_show = p.get("entry_px_display") if status == "open" else p.get("entry_px")
            size_show = p.get("exchange_size") if p.get("exchange_size") is not None else p.get("qty")
            src = p.get("pnl_source") or ""
            hold = (
                f'{p.get("bars_held") or 0}/{p.get("hold_days") or 3}j'
                if p.get("hold_days") is not None
                else "—"
            )
            extra = ""
            if live:
                extra = f'''
              <td class="mono tiny">{sym}</td>
              <td class="mono tiny">{size_show if size_show is not None else "—"}</td>
              <td class="mono tiny {sync_cls}">{sync}</td>'''
            html.append(f'''
            <tr class="trade-row" data-pair="{pair}" onclick="window.focusPair('{pair}')" title="Voir le chart · PnL source={src}">
              <td><span class="tag {status}">{status}</span></td>
              <td class="pair">{pair}</td>
              {extra}
              <td class="mono">{p.get("entry_date") or "—"}</td>
              <td class="mono">{_px(entry_show)}</td>
              <td class="mono">{_px(p.get("last_px"))}</td>
              <td>
                <div class="bar"><div style="width:{prog}%"></div></div>
                <span class="muted tiny">{hold}</span>
              </td>
              <td class="mono">{_usd_plain(p.get("notional_usd"))}</td>
              <td class="mono {_cls(pnl_pct)}">{_pct(pnl_pct)}</td>
              <td class="mono {_cls(pnl)}">{_usd_plain(pnl)}</td>
              <td><button type="button" class="btn-chart" onclick="event.stopPropagation();window.focusPair('{pair}')">Chart</button></td>
            </tr>''')
        return "\n".join(html)

    # Bybit raw positions + orders tables
    ex_pos_rows = []
    for p in ex.get("positions") or []:
        upnl = p.get("unrealisedPnl")
        ex_pos_rows.append(f'''
        <tr>
          <td class="pair mono">{p.get("symbol")}</td>
          <td class="mono">{p.get("side")}</td>
          <td class="mono">{p.get("size")}</td>
          <td class="mono">{_px(p.get("avgPrice"))}</td>
          <td class="mono">{_px(p.get("markPrice"))}</td>
          <td class="mono {_cls(upnl)}">{_usd_plain(upnl)}</td>
          <td class="mono">{p.get("leverage") or "—"}x</td>
        </tr>''')
    if not ex_pos_rows:
        ex_pos_rows = ['<tr><td colspan="7" class="muted">Aucune position exchange</td></tr>']

    ex_ord_rows = []
    for o in (ex.get("orders") or [])[:15]:
        ex_ord_rows.append(f'''
        <tr>
          <td class="mono tiny">{_ms_utc(o.get("createdTime"))}</td>
          <td class="pair mono">{o.get("symbol")}</td>
          <td class="mono">{o.get("side")}</td>
          <td class="mono">{o.get("orderType")}</td>
          <td class="mono">{o.get("qty")}</td>
          <td class="mono">{_px(float(o["avgPrice"]) if o.get("avgPrice") not in (None,"") else None)}</td>
          <td><span class="tag open">{o.get("orderStatus")}</span></td>
          <td class="mono tiny">{(o.get("orderId") or "")[:8]}…</td>
        </tr>''')
    if not ex_ord_rows:
        ex_ord_rows = ['<tr><td colspan="8" class="muted">Aucun ordre récent</td></tr>']

    sig_rows = []
    for i, sig in enumerate(data.get("signals") or [], 1):
        pair = sig.get("pair") or ""
        sig_rows.append(f'''
        <tr class="trade-row" data-pair="{pair}" onclick="window.focusPair('{pair}')">
          <td>{i}</td>
          <td class="pair">{pair}</td>
          <td class="mono">{sig.get("signal_date")}</td>
          <td class="mono">{_px(sig.get("close"))}</td>
          <td class="mono pos">{_pct(sig.get("ret_3d"),1)}</td>
          <td class="mono">{f'{sig.get("rsi14"):.1f}' if sig.get("rsi14") is not None else "—"}</td>
          <td class="mono">{f'{sig.get("vol_spike"):.1f}x' if sig.get("vol_spike") is not None else "—"}</td>
          <td class="mono">{_pct(sig.get("dist_sma20"),1)}</td>
          <td><button type="button" class="btn-chart" onclick="event.stopPropagation();window.focusPair('{pair}')">Chart</button></td>
        </tr>''')
    if not sig_rows:
        sig_rows = ['<tr><td colspan="9" class="muted">Aucun signal</td></tr>']

    bt_html = '<p class="muted">Lance <code>python backtest_fade.py</code></p>'
    if bt:
        years = ""
        for y, v in (bt.get("by_year") or {}).items():
            years += f'''
            <div class="mini">
              <div class="mini-k">{y}</div>
              <div class="mini-v {_cls(v.get("mean_net"))}">{_pct(v.get("mean_net"),1)}</div>
              <div class="muted tiny">n={v.get("n")} · WR {_pct(v.get("win_rate"),0)}</div>
            </div>'''
        bt_html = f'''
        <div class="bt-name">{bt.get("rule")}</div>
        <div class="muted tiny" style="margin-bottom:8px">{bt.get("description") or ""}</div>
        <div class="kpis tight">
          <div class="kpi"><div class="k">Trades</div><div class="v">{bt.get("n")}</div></div>
          <div class="kpi"><div class="k">Win rate</div><div class="v">{_pct(bt.get("win_rate"))}</div></div>
          <div class="kpi"><div class="k">E[net]</div><div class="v {_cls(bt.get("expectancy"))}">{_pct(bt.get("expectancy"))}</div></div>
          <div class="kpi"><div class="k">PF</div><div class="v">{f'{bt.get("profit_factor"):.2f}' if bt.get("profit_factor") else "—"}</div></div>
        </div>
        <div class="year-grid">{years}</div>'''

    total_cls = _cls(s.get("total_pnl"))

    return f'''<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
{refresh_meta}
<title>Fade Dashboard</title>
<script src="https://unpkg.com/lightweight-charts@4.2.0/dist/lightweight-charts.standalone.production.js"></script>
<style>
  :root {{
    --bg: #0b0f14; --panel: #121820; --panel2: #18212c; --border: #243041;
    --text: #e7eef7; --muted: #8b9bb0; --pos: #3dd68c; --neg: #f07178;
    --accent: #5b9dff; --warn: #f0b429; --radius: 14px;
    --font: "Segoe UI", system-ui, -apple-system, sans-serif;
    --mono: "Cascadia Code", "SF Mono", Consolas, monospace;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 20px;
    font-family: var(--font); color: var(--text);
    background: radial-gradient(1200px 600px at 10% -10%, #152033 0%, var(--bg) 55%);
    min-height: 100vh;
  }}
  .wrap {{ max-width: 1280px; margin: 0 auto; }}
  header {{ display:flex; flex-wrap:wrap; justify-content:space-between; gap:12px; align-items:flex-end; margin-bottom:16px; }}
  h1 {{ margin:0; font-size:1.4rem; font-weight:700; letter-spacing:-0.02em; }}
  .sub {{ color:var(--muted); font-size:0.86rem; margin-top:4px; max-width:760px; line-height:1.4; }}
  .badge {{ display:inline-flex; align-items:center; gap:8px; background:var(--panel); border:1px solid var(--border);
    border-radius:999px; padding:8px 14px; font-size:0.82rem; color:var(--muted); }}
  .dot {{ width:8px; height:8px; border-radius:50%; background:var(--pos); box-shadow:0 0 10px var(--pos); }}
  .grid {{ display:grid; grid-template-columns:repeat(12,1fr); gap:12px; }}
  .card {{ background:linear-gradient(180deg,var(--panel),#0f141c); border:1px solid var(--border); border-radius:var(--radius); padding:14px 16px; }}
  .span-3 {{ grid-column:span 3; }} .span-4 {{ grid-column:span 4; }} .span-5 {{ grid-column:span 5; }}
  .span-7 {{ grid-column:span 7; }} .span-8 {{ grid-column:span 8; }} .span-12 {{ grid-column:span 12; }}
  @media (max-width:960px) {{
    .span-3,.span-4,.span-5,.span-7,.span-8 {{ grid-column:span 12; }}
    body {{ padding:12px; }}
  }}
  .label {{ color:var(--muted); font-size:0.75rem; text-transform:uppercase; letter-spacing:0.06em; }}
  .big {{ font-size:1.55rem; font-weight:700; margin-top:4px; font-variant-numeric:tabular-nums; }}
  .pos {{ color:var(--pos); }} .neg {{ color:var(--neg); }} .muted {{ color:var(--muted); }}
  .tiny {{ font-size:0.75rem; }} .mono {{ font-family:var(--mono); font-variant-numeric:tabular-nums; }}
  .kpis {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(100px,1fr)); gap:8px; }}
  .kpis.tight {{ margin-top:10px; }}
  .kpi {{ background:var(--panel2); border:1px solid var(--border); border-radius:10px; padding:8px 10px; }}
  .kpi .k {{ color:var(--muted); font-size:0.7rem; text-transform:uppercase; letter-spacing:0.04em; }}
  .kpi .v {{ font-size:1rem; font-weight:650; margin-top:3px; }}
  h2 {{ margin:0 0 10px; font-size:0.92rem; font-weight:650; display:flex; align-items:center; gap:8px; flex-wrap:wrap; }}
  h2 .count {{ background:var(--panel2); border:1px solid var(--border); border-radius:999px; padding:1px 8px; font-size:0.72rem; color:var(--muted); }}
  table {{ width:100%; border-collapse:collapse; font-size:0.84rem; }}
  th {{ text-align:left; color:var(--muted); font-weight:600; font-size:0.7rem; text-transform:uppercase;
    letter-spacing:0.05em; padding:8px; border-bottom:1px solid var(--border); }}
  td {{ padding:9px 8px; border-bottom:1px solid #1a2330; vertical-align:middle; }}
  tr.trade-row {{ cursor:pointer; }}
  tr.trade-row:hover td {{ background:rgba(91,157,255,0.06); }}
  tr.trade-row.active td {{ background:rgba(91,157,255,0.12); }}
  .pair {{ font-weight:650; }}
  .tag {{ display:inline-block; font-size:0.66rem; font-weight:700; text-transform:uppercase;
    letter-spacing:0.04em; padding:3px 7px; border-radius:6px; }}
  .tag.open {{ background:rgba(91,157,255,0.15); color:var(--accent); }}
  .tag.closed {{ background:rgba(139,155,176,0.15); color:var(--muted); }}
  .tag.pending {{ background:rgba(240,180,41,0.15); color:var(--warn); }}
  .bar {{ height:6px; background:#1c2736; border-radius:99px; overflow:hidden; width:72px; display:inline-block; vertical-align:middle; margin-right:6px; }}
  .bar > div {{ height:100%; background:linear-gradient(90deg,var(--accent),#8b7bff); }}
  .spark {{ width:100%; height:64px; display:block; }}
  .spark.empty {{ height:64px; display:flex; align-items:center; justify-content:center; color:var(--muted);
    font-size:0.85rem; background:var(--panel2); border-radius:10px; border:1px dashed var(--border); }}
  .year-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(90px,1fr)); gap:8px; margin-top:10px; }}
  .mini {{ background:var(--panel2); border:1px solid var(--border); border-radius:10px; padding:8px; }}
  .mini-k {{ color:var(--muted); font-size:0.72rem; }} .mini-v {{ font-size:1.05rem; font-weight:700; }}
  .bt-name {{ font-weight:700; }}
  footer {{ margin-top:16px; color:var(--muted); font-size:0.76rem; display:flex; flex-wrap:wrap; gap:10px 16px; }}
  code {{ background:var(--panel2); border:1px solid var(--border); border-radius:6px; padding:1px 6px; font-family:var(--mono); font-size:0.8em; }}
  .chart-toolbar {{ display:flex; flex-wrap:wrap; gap:10px; align-items:center; justify-content:space-between; margin-bottom:10px; }}
  .chart-toolbar select {{
    background:var(--panel2); color:var(--text); border:1px solid var(--border);
    border-radius:8px; padding:7px 10px; font-size:0.88rem; min-width:160px;
  }}
  .legend {{ display:flex; flex-wrap:wrap; gap:10px 14px; font-size:0.75rem; color:var(--muted); }}
  .legend i {{ display:inline-block; width:8px; height:8px; border-radius:50%; margin-right:4px; }}
  #tv-chart {{ width:100%; height:440px; border-radius:10px; overflow:hidden; background:#0d131a; border:1px solid var(--border); }}
  #tv-vol {{ width:100%; height:90px; border-radius:0 0 10px 10px; margin-top:-1px; }}
  .chart-meta {{ display:flex; flex-wrap:wrap; gap:8px 16px; margin-top:10px; font-size:0.82rem; color:var(--muted); }}
  .chart-meta strong {{ color:var(--text); font-weight:650; }}
  .btn-chart {{
    background:rgba(91,157,255,0.12); color:var(--accent); border:1px solid rgba(91,157,255,0.35);
    border-radius:7px; padding:4px 8px; font-size:0.72rem; font-weight:650; cursor:pointer;
  }}
  .btn-chart:hover {{ background:rgba(91,157,255,0.22); }}
  .trade-chips {{ display:flex; flex-wrap:wrap; gap:6px; margin-top:8px; }}
  .chip {{
    background:var(--panel2); border:1px solid var(--border); border-radius:999px;
    padding:4px 10px; font-size:0.75rem; cursor:pointer;
  }}
  .chip:hover, .chip.on {{ border-color:var(--accent); color:var(--accent); }}
  .hint {{ color:var(--muted); font-size:0.78rem; margin-top:6px; }}
  .mode-live {{ background:rgba(61,214,140,0.12); border-color:rgba(61,214,140,0.45); color:var(--pos); }}
  .mode-paper {{ background:rgba(240,180,41,0.12); border-color:rgba(240,180,41,0.45); color:var(--warn); }}
  .mode-err {{ background:rgba(240,113,120,0.12); border-color:rgba(240,113,120,0.45); color:var(--neg); }}
  .banner {{
    margin-bottom:12px; padding:10px 14px; border-radius:10px; border:1px solid var(--border);
    font-size:0.86rem; line-height:1.45;
  }}
  .banner.ok {{ background:rgba(61,214,140,0.08); border-color:rgba(61,214,140,0.35); }}
  .banner.warn {{ background:rgba(240,180,41,0.08); border-color:rgba(240,180,41,0.4); }}
  .banner.err {{ background:rgba(240,113,120,0.08); border-color:rgba(240,113,120,0.4); }}
  .tag.matched {{ background:rgba(61,214,140,0.15); color:var(--pos); }}
  .tag.filled {{ background:rgba(61,214,140,0.15); color:var(--pos); }}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div>
      <h1>Fade Dashboard {'· LIVE Bybit' if live else '· Paper'}</h1>
      <div class="sub">{data.get("profile") or data["rule"]}</div>
      <div class="sub" style="margin-top:2px">{data["rule"]}</div>
    </div>
    <div style="display:flex; flex-wrap:wrap; gap:8px; align-items:center;">
      <div class="badge {'mode-live' if live else 'mode-paper'}">{'LIVE' if live else 'PAPER'}</div>
      <div class="badge {'mode-live' if ex.get('ok') else ('mode-err' if live else '')}">
        <span class="dot" style="{'background:var(--pos)' if (ex.get('ok') or not live) else 'background:var(--neg)'}"></span>
        {'Bybit OK' if ex.get('ok') else ('Bybit OFF' if live else 'local')}
      </div>
      <div class="badge"><span class="dot"></span> Maj {gen}</div>
    </div>
  </header>

  {'' if not live else (f'''
  <div class="banner {'ok' if ex.get('ok') and s.get('sync_ok') else ('err' if not ex.get('ok') else 'warn')}">
    <strong>Source de vérité :</strong> equity + uPnL + positions/ordres = API Bybit
    (sub <code>{ex.get('api_note') or 'AI'}</code>
    uid=<code>{ex.get('api_uid') or '?'}</code> · env=<code>{ex.get('env')}</code>).
    Book local = hold days / signaux.
    Sync open: <strong class="{'pos' if s.get('sync_ok') else 'neg'}">{s.get('n_matched',0)} matched</strong>
    · {s.get('n_local_only',0)} local-only · {s.get('n_exchange_only',0)} exchange-only.
    {(' <strong class="neg">API: ' + str(ex.get('error')) + '</strong>') if ex.get('error') else ''}
    {(' <strong class="neg">KILL: ' + str(data.get('kill_reason')) + '</strong>') if data.get('killed') else ''}
  </div>
  ''')}

  <div class="grid">
    <div class="card span-3">
      <div class="label">{'Equity Bybit (live)' if live else 'Equity paper'}</div>
      <div class="big">{_usd_plain(s["equity_now"]).lstrip("+")}</div>
      <div class="muted tiny">start {_usd_plain(s["equity_start"]).lstrip("+")}{' · UTA sub-account' if live else ''}</div>
    </div>
    <div class="card span-3">
      <div class="label">P&amp;L total</div>
      <div class="big {total_cls}">{_usd_plain(s["total_pnl"])}</div>
      <div class="{total_cls} tiny">{_pct(s["total_pnl_pct"])} · réel {_usd_plain(s["realized_pnl"])} · latent {_usd_plain(s["unrealized_pnl"])}</div>
    </div>
    <div class="card span-3">
      <div class="label">Positions</div>
      <div class="big">{s["n_open"]}<span class="muted" style="font-size:1rem"> open</span></div>
      <div class="muted tiny">{'ex ' + str(ex.get('n_positions') or 0) + ' · ' if live else ''}{s.get("n_pending", 0)} pending · {s["n_closed"]} closed · max {s.get("max_open", "—")}</div>
    </div>
    <div class="card span-3">
      <div class="label">{'Sync book ↔ Bybit' if live else 'Win rate (closed)'}</div>
      <div class="big {'pos' if (not live or s.get('sync_ok')) else 'neg'}">{'OK' if (not live or s.get('sync_ok')) else 'DRIFT' if live else (_pct(s['win_rate'],0) if s.get('win_rate') is not None else '—')}</div>
      <div class="muted tiny">{'matched ' + str(s.get('n_matched',0)) + '/' + str(s.get('n_open',0)) if live else ('best ' + _usd_plain(s.get('best_trade')) + ' · worst ' + _usd_plain(s.get('worst_trade')))}</div>
    </div>

    <!-- CHART -->
    <div class="card span-12">
      <div class="chart-toolbar">
        <h2 style="margin:0">Chart trades <span class="count">TradingView LWC · daily</span></h2>
        <div style="display:flex; gap:10px; align-items:center; flex-wrap:wrap;">
          <label class="muted tiny" for="pair-select">Paire</label>
          <select id="pair-select" onchange="window.focusPair(this.value)">{pair_options}</select>
        </div>
      </div>
      <div class="legend">
        <span><i style="background:#f0b429"></i> Signal (SIG)</span>
        <span><i style="background:#f07178"></i> Short entry</span>
        <span><i style="background:#3dd68c"></i> Exit</span>
        <span><i style="background:#5b9dff"></i> Scan du jour</span>
        <span>Ligne pointillée rouge = prix d'entrée short</span>
      </div>
      <div id="tv-chart"></div>
      <div class="chart-meta" id="chart-meta">Sélectionne une paire…</div>
      <div class="trade-chips" id="trade-chips"></div>
      <div class="hint">Clique une ligne de trade / signal dans les tableaux pour focus le chart. Scroll = zoom, drag = pan.</div>
    </div>

    <div class="card span-5">
      <h2>Courbe equity <span class="count">closed</span></h2>
      {spark}
      <div class="kpis tight">
        <div class="kpi"><div class="k">Notional / trade</div><div class="v">{_usd_plain(s["notional_per_trade"]).lstrip("+")}</div></div>
        <div class="kpi"><div class="k">Avg win</div><div class="v pos">{_usd_plain(s["avg_win"])}</div></div>
        <div class="kpi"><div class="k">Avg loss</div><div class="v neg">{_usd_plain(s["avg_loss"])}</div></div>
      </div>
    </div>
    <div class="card span-7">
      <h2>Backtest référence</h2>
      {bt_html}
    </div>

    <div class="card span-12">
      <h2>Positions ouvertes (book + Bybit) <span class="count">{s["n_open"]}</span></h2>
      <div class="muted tiny" style="margin-bottom:8px">
        Open: uPnL = Bybit quand matched. Hold = daily bars Kraken. Sync matched = fiable 1:1 avec l'exchange.
      </div>
      <table>
        <thead><tr>
          <th>Statut</th><th>Pair</th>
          {('<th>Bybit</th><th>Size</th><th>Sync</th>' if live else '')}
          <th>Entry</th><th>Px in</th><th>Mark/Last</th>
          <th>Hold</th><th>Notional</th><th>P&amp;L %</th><th>P&amp;L $</th><th></th>
        </tr></thead>
        <tbody>{pos_rows("open")}</tbody>
      </table>
    </div>

    {'' if not live else f'''
    <div class="card span-12">
      <h2>Bybit raw positions <span class="count">{ex.get("n_positions") or 0}</span></h2>
      <div class="muted tiny" style="margin-bottom:8px">
        Snapshot API direct · uid={ex.get("api_uid") or "?"} · fetch {ex.get("fetched_at") or "—"}
      </div>
      <table>
        <thead><tr>
          <th>Symbol</th><th>Side</th><th>Size</th><th>Avg</th><th>Mark</th><th>uPnL $</th><th>Lev</th>
        </tr></thead>
        <tbody>{''.join(ex_pos_rows)}</tbody>
      </table>
    </div>

    <div class="card span-12">
      <h2>Bybit order history <span class="count">{ex.get("n_orders") or 0}</span></h2>
      <table>
        <thead><tr>
          <th>UTC</th><th>Symbol</th><th>Side</th><th>Type</th><th>Qty</th><th>Avg</th><th>Status</th><th>OrderId</th>
        </tr></thead>
        <tbody>{''.join(ex_ord_rows)}</tbody>
      </table>
    </div>
    '''}

    <div class="card span-12">
      <h2>Historique fermé (book) <span class="count">{s["n_closed"]}</span></h2>
      <table>
        <thead><tr>
          <th>Statut</th><th>Pair</th>
          {('<th>Bybit</th><th>Size</th><th>Sync</th>' if live else '')}
          <th>Entry</th><th>Px in</th><th>Exit</th>
          <th>Hold</th><th>Notional</th><th>P&amp;L %</th><th>P&amp;L $</th><th></th>
        </tr></thead>
        <tbody>{pos_rows("closed")}</tbody>
      </table>
    </div>

    <div class="card span-12">
      <h2>Signaux scanner <span class="count">{len(data.get("signals") or [])}</span></h2>
      <div class="muted tiny" style="margin-bottom:8px">Scan: {data.get("signals_generated_at") or "—"}</div>
      <table>
        <thead><tr>
          <th>#</th><th>Pair</th><th>Signal</th><th>Close</th><th>3d</th>
          <th>RSI</th><th>VolX</th><th>vsSMA20</th><th></th>
        </tr></thead>
        <tbody>{''.join(sig_rows)}</tbody>
      </table>
    </div>
  </div>

  <footer>
    <span>Serveur (refresh Bybit): <code>python dashboard.py --serve</code></span>
    <span>Status: <code>python live_book.py status</code></span>
    <span>Ping: <code>python broker_bybit.py ping</code></span>
    <span>Book: <code>{data.get("state_path") or "—"}</code></span>
  </footer>
</div>

<script>
const CHARTS = {charts_json};
let currentPair = {default_pair};
let chart = null;
let candleSeries = null;
let volumeSeries = null;

function fmtPct(x) {{
  if (x === null || x === undefined) return "—";
  return (x * 100).toFixed(2) + "%";
}}
function fmtUsd(x) {{
  if (x === null || x === undefined) return "—";
  const s = x >= 0 ? "+" : "";
  return s + "$" + x.toFixed(2);
}}

function highlightRows(pair) {{
  document.querySelectorAll("tr.trade-row").forEach(tr => {{
    tr.classList.toggle("active", tr.dataset.pair === pair);
  }});
  const sel = document.getElementById("pair-select");
  if (sel && pair) sel.value = pair;
}}

function renderTradeChips(pair) {{
  const el = document.getElementById("trade-chips");
  const meta = document.getElementById("chart-meta");
  const data = CHARTS[pair];
  if (!data) {{
    el.innerHTML = "";
    meta.innerHTML = "Pas de données OHLC pour cette paire.";
    return;
  }}
  meta.innerHTML = `<strong>${{pair}}</strong>
    <span>Last ${{data.last_close}} <span class="muted">(${{data.last_date}})</span></span>
    <span>${{data.candles.length}} bougies daily</span>
    <span>${{(data.trades || []).length}} trade(s) paper</span>`;

  el.innerHTML = (data.trades || []).map(t => {{
    const cls = (t.pnl_pct || 0) >= 0 ? "pos" : "neg";
    return `<span class="chip" title="id ${{t.id}}">
      ${{t.status}} · ${{t.entry_date || t.signal_date || "?"}}
      <span class="${{cls}}">${{fmtPct(t.pnl_pct)}} (${{fmtUsd(t.pnl_usd)}})</span>
    </span>`;
  }}).join("");
}}

function initChart() {{
  const el = document.getElementById("tv-chart");
  if (!el || typeof LightweightCharts === "undefined") {{
    el.innerHTML = '<div class="spark empty">Lib chart non chargée (réseau / CDN). Relance avec --serve.</div>';
    return;
  }}
  chart = LightweightCharts.createChart(el, {{
    layout: {{
      background: {{ type: "solid", color: "#0d131a" }},
      textColor: "#8b9bb0",
    }},
    grid: {{
      vertLines: {{ color: "#1a2330" }},
      horzLines: {{ color: "#1a2330" }},
    }},
    crosshair: {{ mode: LightweightCharts.CrosshairMode.Normal }},
    rightPriceScale: {{ borderColor: "#243041" }},
    timeScale: {{ borderColor: "#243041", timeVisible: false }},
    width: el.clientWidth,
    height: 440,
  }});

  candleSeries = chart.addCandlestickSeries({{
    upColor: "#3dd68c",
    downColor: "#f07178",
    borderUpColor: "#3dd68c",
    borderDownColor: "#f07178",
    wickUpColor: "#3dd68c",
    wickDownColor: "#f07178",
  }});

  volumeSeries = chart.addHistogramSeries({{
    priceFormat: {{ type: "volume" }},
    priceScaleId: "vol",
  }});
  chart.priceScale("vol").applyOptions({{
    scaleMargins: {{ top: 0.8, bottom: 0 }},
  }});
  chart.priceScale("right").applyOptions({{
    scaleMargins: {{ top: 0.08, bottom: 0.22 }},
  }});

  window.addEventListener("resize", () => {{
    if (chart && el) chart.applyOptions({{ width: el.clientWidth }});
  }});
}}

function loadPair(pair) {{
  if (!chart || !candleSeries) return;
  const data = CHARTS[pair];
  if (!data) {{
    renderTradeChips(pair);
    return;
  }}
  currentPair = pair;
  candleSeries.setData(data.candles);
  volumeSeries.setData(data.volumes || []);
  candleSeries.setMarkers(data.markers || []);

  // clear old price lines by recreating series is heavy; use createPriceLine each time after reset
  // Lightweight Charts: remove old lines by storing refs
  if (candleSeries._fadeLines) {{
    candleSeries._fadeLines.forEach(l => candleSeries.removePriceLine(l));
  }}
  candleSeries._fadeLines = [];
  (data.priceLines || []).forEach(pl => {{
    const line = candleSeries.createPriceLine({{
      price: pl.price,
      color: pl.color || "#f07178",
      lineWidth: pl.lineWidth || 1,
      lineStyle: pl.lineStyle !== undefined ? pl.lineStyle : 2,
      axisLabelVisible: true,
      title: pl.title || "Entry",
    }});
    candleSeries._fadeLines.push(line);
  }});

  chart.timeScale().fitContent();
  highlightRows(pair);
  renderTradeChips(pair);
}}

window.focusPair = function(pair) {{
  if (!pair || !CHARTS[pair]) {{
    if (pair) alert("Pas de chart pour " + pair);
    return;
  }}
  loadPair(pair);
  document.getElementById("tv-chart").scrollIntoView({{ behavior: "smooth", block: "center" }});
}};

// boot
(function() {{
  initChart();
  const start = currentPair || Object.keys(CHARTS)[0];
  if (start) loadPair(start);
  else {{
    document.getElementById("chart-meta").textContent = "Aucune paire à afficher — lance un scan / paper trade.";
  }}
}})();
</script>
</body>
</html>'''


def write_dashboard(auto_refresh: bool = False, use_ohlc: bool = True) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    data = build_payload(use_ohlc=use_ohlc)
    html = render_html(data, auto_refresh=auto_refresh)
    DASH_HTML.write_text(html, encoding="utf-8")
    # strip heavy candles from json dump optional full
    (OUT_DIR / "dashboard_data.json").write_text(
        json.dumps({k: v for k, v in data.items() if k != "charts"}, indent=2, default=str),
        encoding="utf-8",
    )
    (OUT_DIR / "dashboard_charts.json").write_text(
        json.dumps(data.get("charts") or {}, default=str),
        encoding="utf-8",
    )
    n_charts = len(data.get("charts") or {})
    print(f"  charts ready: {n_charts} pairs")
    return DASH_HTML


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        if args:
            print(f"[dash] {args[0]}")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        # Fly/Railway health — never block on Kraken/Bybit
        if path in ("/health", "/healthz"):
            body = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if path in ("/", "/dashboard.html", "/index.html"):
            # Prefer cache; avoid multi-minute download on every request
            try:
                from config import CACHE_FILE

                use_ohlc = CACHE_FILE.exists()
            except Exception:
                use_ohlc = False
            try:
                write_dashboard(auto_refresh=True, use_ohlc=use_ohlc)
            except Exception as e:
                print(f"dashboard regen warn: {e}")
            if DASH_HTML.exists():
                body = DASH_HTML.read_bytes()
            else:
                body = b"<html><body><h1>Dashboard loading...</h1><p>OHLC cache warming.</p></body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if path in ("/data.json", "/api/live", "/live.json"):
            try:
                from config import CACHE_FILE

                use_ohlc = CACHE_FILE.exists() and path == "/data.json"
            except Exception:
                use_ohlc = False
            data = build_payload(use_ohlc=use_ohlc)
            # without full candles for lightness
            slim = {k: v for k, v in data.items() if k != "charts"}
            body = json.dumps(slim, default=str).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if path.startswith("/ohlc/"):
            pair = unquote(path[len("/ohlc/"):])
            data = build_payload(use_ohlc=True)
            chart = (data.get("charts") or {}).get(pair)
            if not chart:
                self.send_error(404, "pair not found")
                return
            body = json.dumps(chart, default=str).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)


def serve(open_browser: bool = True, host: str | None = None, port: int | None = None) -> None:
    """
    Bind HTTP immediately so Fly/Railway health checks pass.
    First HTML is generated without blocking on full Kraken OHLC download;
    charts hydrate on request / background warm if cache missing.
    """
    import threading

    host = host or BIND_HOST
    port = int(port or PORT)

    # Fast path: never block bind on full universe download
    try:
        from config import CACHE_FILE

        has_cache = CACHE_FILE.exists()
    except Exception:
        has_cache = False
    write_dashboard(auto_refresh=True, use_ohlc=has_cache)

    httpd = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{port}/"
    print(f"Dashboard + charts → {url} (bind={host})")
    print("Ctrl+C pour arrêter.")

    if not has_cache:
        def _warm() -> None:
            try:
                print("Background: warming OHLC cache (first boot)...")
                write_dashboard(auto_refresh=True, use_ohlc=True)
                print("Background: OHLC cache ready.")
            except Exception as e:
                print(f"Background OHLC warm failed: {e}")

        threading.Thread(target=_warm, daemon=True).start()

    if open_browser and host in ("127.0.0.1", "localhost"):
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        httpd.server_close()


def main() -> None:
    ap = argparse.ArgumentParser(description="Fade dashboard + TradingView-style charts")
    ap.add_argument("--serve", action="store_true")
    ap.add_argument("--no-open", action="store_true")
    ap.add_argument("--no-ohlc", action="store_true")
    ap.add_argument("--host", default=None, help="Bind host (default BIND_HOST or 127.0.0.1)")
    ap.add_argument("--port", type=int, default=None, help="Port (default PORT env or 8765)")
    args = ap.parse_args()

    if args.serve:
        serve(open_browser=not args.no_open, host=args.host, port=args.port)
        return

    path = write_dashboard(auto_refresh=False, use_ohlc=not args.no_ohlc)
    print(f"Dashboard écrit → {path}")
    if not args.no_open:
        # file:// works but CDN needs network; charts still embed data
        webbrowser.open(path.resolve().as_uri())


if __name__ == "__main__":
    main()
