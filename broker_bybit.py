#!/usr/bin/env python3
"""
Bybit linear USDT perps broker (V5 REST, HMAC).

Keys: BYBIT_API_KEY / BYBIT_API_SECRET from process env or live/.env.
Never logs secrets in clear text.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any

from config import (
    BYBIT_ACCOUNT_TYPE,
    BYBIT_BASE_URLS,
    BYBIT_CATEGORY,
    BYBIT_ENV,
    BYBIT_RECV_WINDOW,
    BYBIT_SETTLE_COIN,
    LEVERAGE,
    LIVE_DIR,
    ROOT,
)

_ENV_LOADED = False


def _mask(s: str | None, keep: int = 4) -> str:
    if not s:
        return "<empty>"
    if len(s) <= keep * 2:
        return "***"
    return f"{s[:keep]}…{s[-keep:]}"


def _load_dotenv_file(path: Path) -> None:
    """Minimal KEY=VALUE loader (no python-dotenv dependency)."""
    if not path.is_file():
        return
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip("'").strip('"')
        if key and key not in os.environ:
            os.environ[key] = val


def ensure_env_loaded() -> None:
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    # Prefer live/.env, then repo root .env (both gitignored)
    _load_dotenv_file(LIVE_DIR / ".env")
    _load_dotenv_file(ROOT / ".env")
    _ENV_LOADED = True


def get_credentials() -> tuple[str, str]:
    ensure_env_loaded()
    key = (os.environ.get("BYBIT_API_KEY") or "").strip()
    secret = (os.environ.get("BYBIT_API_SECRET") or "").strip()
    if not key or not secret:
        raise RuntimeError(
            "Missing BYBIT_API_KEY / BYBIT_API_SECRET. "
            "Set them in the environment or in live/.env (see live/.env.example)."
        )
    return key, secret


def kraken_to_bybit_symbol(pair: str) -> str:
    """Map Kraken-style PAIR/USD → Bybit linear PAIRUSDT.

    Examples: SOL/USD → SOLUSDT, DOGE/USD → DOGEUSDT, XRPUSD → XRPUSDT
    """
    p = (pair or "").strip().upper().replace(" ", "")
    if not p:
        raise ValueError("empty pair")
    if p.endswith("USDT"):
        return p
    if "/" in p:
        base, quote = p.split("/", 1)
    elif p.endswith("USD"):
        base, quote = p[:-3], "USD"
    else:
        base, quote = p, "USD"
    base = base.replace("XBT", "BTC").replace("XXBT", "BTC")
    if quote not in ("USD", "USDT", "ZUSD"):
        # still map alts quoted USD → USDT perps
        pass
    return f"{base}USDT"


class BybitBroker:
    """Thin HMAC client for Bybit V5 linear USDT perps."""

    def __init__(
        self,
        env: str | None = None,
        category: str | None = None,
        api_key: str | None = None,
        api_secret: str | None = None,
    ) -> None:
        self.env = (env or BYBIT_ENV).lower()
        if self.env not in BYBIT_BASE_URLS:
            raise ValueError(f"Unknown BYBIT_ENV={self.env!r}")
        self.base_url = BYBIT_BASE_URLS[self.env].rstrip("/")
        self.category = category or BYBIT_CATEGORY
        if api_key and api_secret:
            self.api_key = api_key
            self.api_secret = api_secret
        else:
            self.api_key, self.api_secret = get_credentials()
        self.recv_window = BYBIT_RECV_WINDOW

    # --- signing / HTTP -------------------------------------------------

    def _sign(self, payload: str) -> str:
        return hmac.new(
            self.api_secret.encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _headers(self, payload: str) -> dict[str, str]:
        ts = str(int(time.time() * 1000))
        sign_payload = f"{ts}{self.api_key}{self.recv_window}{payload}"
        return {
            "Content-Type": "application/json",
            "X-BAPI-API-KEY": self.api_key,
            "X-BAPI-TIMESTAMP": ts,
            "X-BAPI-SIGN": self._sign(sign_payload),
            "X-BAPI-RECV-WINDOW": self.recv_window,
            "User-Agent": "fade-scanner-bybit/1.0",
        }

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        auth: bool = True,
    ) -> dict[str, Any]:
        method = method.upper()
        params = {k: v for k, v in (params or {}).items() if v is not None}
        query = ""
        if params:
            # Bybit requires sorted query for GET signature
            items = sorted((str(k), str(v)) for k, v in params.items())
            query = urllib.parse.urlencode(items)
        body_str = ""
        if body is not None:
            body_str = json.dumps(body, separators=(",", ":"))

        if method == "GET":
            sign_payload = query
            url = f"{self.base_url}{path}" + (f"?{query}" if query else "")
            data = None
        else:
            sign_payload = body_str
            url = f"{self.base_url}{path}"
            data = body_str.encode("utf-8")

        headers = (
            self._headers(sign_payload)
            if auth
            else {"Content-Type": "application/json", "User-Agent": "fade-scanner-bybit/1.0"}
        )
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace") if e.fp else ""
            raise RuntimeError(f"Bybit HTTP {e.code} {path}: {err_body[:500]}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"Bybit network error {path}: {e}") from e

        try:
            out = json.loads(raw)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Bybit non-JSON response {path}: {raw[:300]}") from e

        if out.get("retCode", 0) != 0:
            # Never echo credentials; retMsg is safe
            raise RuntimeError(
                f"Bybit API error retCode={out.get('retCode')} "
                f"retMsg={out.get('retMsg')!r} path={path}"
            )
        return out

    def public_get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._request("GET", path, params=params, auth=False)

    def private_get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._request("GET", path, params=params, auth=True)

    def private_post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", path, body=body, auth=True)

    # --- market helpers -------------------------------------------------

    def get_ticker_price(self, symbol: str) -> float:
        out = self.public_get(
            "/v5/market/tickers",
            {"category": self.category, "symbol": symbol},
        )
        rows = (out.get("result") or {}).get("list") or []
        if not rows:
            raise RuntimeError(f"No ticker for {symbol}")
        last = rows[0].get("lastPrice") or rows[0].get("markPrice")
        if last is None:
            raise RuntimeError(f"No price for {symbol}")
        return float(last)

    def get_instrument(self, symbol: str) -> dict[str, Any]:
        out = self.public_get(
            "/v5/market/instruments-info",
            {"category": self.category, "symbol": symbol},
        )
        rows = (out.get("result") or {}).get("list") or []
        if not rows:
            raise RuntimeError(f"Instrument not found on Bybit: {symbol}")
        return rows[0]

    def _qty_step(self, instrument: dict[str, Any]) -> tuple[Decimal, Decimal]:
        lot = instrument.get("lotSizeFilter") or {}
        qty_step = Decimal(str(lot.get("qtyStep") or "0.001"))
        min_qty = Decimal(str(lot.get("minOrderQty") or qty_step))
        return qty_step, min_qty

    def notional_to_qty(self, symbol: str, notional_usd: float) -> str:
        """Convert USD notional → base qty string rounded down to qtyStep."""
        if notional_usd <= 0:
            raise ValueError("notional_usd must be > 0")
        instrument = self.get_instrument(symbol)
        price = self.get_ticker_price(symbol)
        if price <= 0:
            raise RuntimeError(f"Invalid price for {symbol}")
        qty_step, min_qty = self._qty_step(instrument)
        raw = Decimal(str(notional_usd)) / Decimal(str(price))
        # floor to step
        steps = (raw / qty_step).to_integral_value(rounding=ROUND_DOWN)
        qty = steps * qty_step
        if qty < min_qty:
            raise RuntimeError(
                f"Qty {qty} < minOrderQty {min_qty} for {symbol} "
                f"(notional=${notional_usd:.2f} @ {price})"
            )
        # normalize string without scientific notation
        q = format(qty, "f").rstrip("0").rstrip(".") if "." in format(qty, "f") else format(qty, "f")
        return q

    # --- account / positions --------------------------------------------

    def get_equity_usdt(self) -> float:
        """Total equity in USDT (prefer coin equity, fallback totalEquity)."""
        out = self.private_get(
            "/v5/account/wallet-balance",
            {"accountType": BYBIT_ACCOUNT_TYPE, "coin": BYBIT_SETTLE_COIN},
        )
        rows = (out.get("result") or {}).get("list") or []
        if not rows:
            return 0.0
        acc = rows[0]
        coins = acc.get("coin") or []
        for c in coins:
            if (c.get("coin") or "").upper() == BYBIT_SETTLE_COIN:
                for field in ("equity", "walletBalance", "usdValue"):
                    v = c.get(field)
                    if v is not None and str(v) != "":
                        return float(v)
        te = acc.get("totalEquity")
        return float(te) if te not in (None, "") else 0.0

    def list_open_positions(self) -> list[dict[str, Any]]:
        out = self.private_get(
            "/v5/position/list",
            {
                "category": self.category,
                "settleCoin": BYBIT_SETTLE_COIN,
            },
        )
        rows = (out.get("result") or {}).get("list") or []
        open_pos: list[dict[str, Any]] = []
        for r in rows:
            size = float(r.get("size") or 0)
            if size == 0:
                continue
            open_pos.append(
                {
                    "symbol": r.get("symbol"),
                    "side": r.get("side"),  # Buy = long, Sell = short
                    "size": size,
                    "avgPrice": float(r.get("avgPrice") or 0) or None,
                    "markPrice": float(r.get("markPrice") or 0) or None,
                    "unrealisedPnl": float(r.get("unrealisedPnl") or 0),
                    "leverage": r.get("leverage"),
                    "positionIdx": r.get("positionIdx"),
                    "raw": r,
                }
            )
        return open_pos

    def get_position_size(self, symbol: str) -> float:
        """Absolute open size for symbol (0 if flat)."""
        out = self.private_get(
            "/v5/position/list",
            {"category": self.category, "symbol": symbol},
        )
        rows = (out.get("result") or {}).get("list") or []
        total = 0.0
        for r in rows:
            total += abs(float(r.get("size") or 0))
        return total

    def set_leverage(self, symbol: str, leverage: int | float | None = None) -> dict[str, Any]:
        lev = str(int(leverage if leverage is not None else LEVERAGE))
        body = {
            "category": self.category,
            "symbol": symbol,
            "buyLeverage": lev,
            "sellLeverage": lev,
        }
        try:
            return self.private_post("/v5/position/set-leverage", body)
        except RuntimeError as e:
            msg = str(e).lower()
            # 110043 = leverage not modified (already set) — treat as OK
            if "110043" in msg or "not modified" in msg:
                return {"retCode": 0, "retMsg": "leverage unchanged", "result": {}}
            raise

    def open_short(self, symbol: str, notional_usd: float) -> dict[str, Any]:
        """Market Sell short for ~notional_usd USD."""
        symbol = symbol.upper()
        self.set_leverage(symbol)
        qty = self.notional_to_qty(symbol, notional_usd)
        body = {
            "category": self.category,
            "symbol": symbol,
            "side": "Sell",
            "orderType": "Market",
            "qty": qty,
            "timeInForce": "IOC",
            "reduceOnly": False,
        }
        out = self.private_post("/v5/order/create", body)
        result = out.get("result") or {}
        return {
            "symbol": symbol,
            "side": "Sell",
            "qty": qty,
            "notional_usd": notional_usd,
            "orderId": result.get("orderId"),
            "orderLinkId": result.get("orderLinkId"),
            "raw": out,
        }

    def close_short(self, symbol: str, qty: float | str | None = None) -> dict[str, Any]:
        """Reduce-only market Buy to cover short. qty=None → full position size."""
        symbol = symbol.upper()
        if qty is None:
            size = self.get_position_size(symbol)
            if size <= 0:
                raise RuntimeError(f"No open position to close on {symbol}")
            qty_str = format(Decimal(str(size)), "f").rstrip("0").rstrip(".")
            if not qty_str:
                qty_str = str(size)
        else:
            qty_str = str(qty)
        body = {
            "category": self.category,
            "symbol": symbol,
            "side": "Buy",
            "orderType": "Market",
            "qty": qty_str,
            "timeInForce": "IOC",
            "reduceOnly": True,
        }
        out = self.private_post("/v5/order/create", body)
        result = out.get("result") or {}
        return {
            "symbol": symbol,
            "side": "Buy",
            "qty": qty_str,
            "reduceOnly": True,
            "orderId": result.get("orderId"),
            "orderLinkId": result.get("orderLinkId"),
            "raw": out,
        }

    def ping(self) -> dict[str, Any]:
        """Connectivity + auth check (no secrets logged)."""
        # public
        self.public_get("/v5/market/time")
        # private
        equity = self.get_equity_usdt()
        positions = self.list_open_positions()
        return {
            "ok": True,
            "env": self.env,
            "base_url": self.base_url,
            "category": self.category,
            "api_key": _mask(self.api_key),
            "equity_usdt": equity,
            "open_positions": len(positions),
        }


def default_broker() -> BybitBroker:
    return BybitBroker()


def main() -> int:
    ap = argparse.ArgumentParser(description="Bybit linear broker helpers")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("ping", help="Auth + equity smoke test")
    sub.add_parser("equity", help="Print USDT equity")
    sub.add_parser("positions", help="List open linear positions")

    p_map = sub.add_parser("map", help="Map Kraken pair → Bybit symbol")
    p_map.add_argument("pair", help="e.g. SOL/USD")

    p_lev = sub.add_parser("set-leverage", help="Set leverage on symbol")
    p_lev.add_argument("symbol", help="e.g. SOLUSDT")
    p_lev.add_argument("--leverage", type=int, default=None)

    p_open = sub.add_parser("open-short", help="Market short by notional USD")
    p_open.add_argument("symbol")
    p_open.add_argument("notional", type=float)

    p_close = sub.add_parser("close-short", help="Reduce-only cover short")
    p_close.add_argument("symbol")
    p_close.add_argument("--qty", default=None)

    args = ap.parse_args()

    if args.cmd == "map":
        print(kraken_to_bybit_symbol(args.pair))
        return 0

    try:
        br = default_broker()
    except RuntimeError as e:
        print(f"ERROR: {e}")
        return 2

    try:
        if args.cmd == "ping":
            info = br.ping()
            print(json.dumps(info, indent=2))
        elif args.cmd == "equity":
            print(f"{br.get_equity_usdt():.4f}")
        elif args.cmd == "positions":
            pos = br.list_open_positions()
            # strip raw for cleaner CLI
            clean = [{k: v for k, v in p.items() if k != "raw"} for p in pos]
            print(json.dumps(clean, indent=2))
        elif args.cmd == "set-leverage":
            out = br.set_leverage(args.symbol.upper(), args.leverage)
            print(json.dumps({"retCode": out.get("retCode"), "retMsg": out.get("retMsg")}, indent=2))
        elif args.cmd == "open-short":
            out = br.open_short(args.symbol.upper(), args.notional)
            safe = {k: v for k, v in out.items() if k != "raw"}
            print(json.dumps(safe, indent=2))
        elif args.cmd == "close-short":
            out = br.close_short(args.symbol.upper(), qty=args.qty)
            safe = {k: v for k, v in out.items() if k != "raw"}
            print(json.dumps(safe, indent=2))
        return 0
    except Exception as e:
        print(f"ERROR: {e}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
