"""Shared config — Profil A risk 5% (full auto target)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT_DIR = ROOT / "out"
CACHE_DIR = ROOT / "cache"
PAPER_DIR = ROOT / "paper"
CACHE_FILE = CACHE_DIR / "ohlc_daily.json"
SIGNALS_FILE = OUT_DIR / "fade_signals_latest.json"
PAPER_LEDGER = PAPER_DIR / "ledger.jsonl"
PAPER_STATE = PAPER_DIR / "open_positions.json"
BOT_LOG = OUT_DIR / "bot_run.log"

# --- FADE-BLOWOFF-T3 ---
PUMP_LOOKBACK = 3
PUMP_MIN = 0.40
MIN_RSI = 70.0
MIN_VOL_SPIKE = 3.0
MIN_DIST_SMA20 = 0.20
MIN_AVG_VOL_USD_10D = 10_000.0
HOLD_DAYS = 3
FEE_RT = 0.0052
STRICT_VOL5 = False

# =============================================================================
# PROFIL A — risk 5%
#   CROSS · 2x · marge 5%/trade · max 3 pos
#   notional = 10% equity / trade · gross max 30%
#   Backtest ~23m: CAGR ~+124% | MDD ~-67% | Y2025 ~+75% | no wipe (sample)
# =============================================================================
PROFILE_NAME = "A_RISK5_L2_MAX3"

EQUITY_USD = 100.0
RISK_PCT_PER_TRADE = 0.05  # 5% margin per trade
MARGIN_MODE = "cross"
LEVERAGE = 2
MAX_OPEN_POSITIONS = 3
FULL_AUTO = True

# Execution mode: "paper" | "live"  (live = needs exchange keys later)
EXECUTION_MODE = "paper"

# Kill-switches
MAX_DAILY_LOSS_PCT = 0.20
MAX_DRAWDOWN_PCT = 0.70  # profile MDD ~67%, leave a bit of room
MIN_EQUITY_USD = 25.0

# Paper / dashboard aliases
PAPER_EQUITY_USD = EQUITY_USD
ASSUMED_ADVERSE_MOVE = 1.0 / LEVERAGE
MAX_NOTIONAL_PCT = RISK_PCT_PER_TRADE * LEVERAGE  # 10%

# Daily job
# Prefer running shortly after UTC daily candle close (~00:05–00:30 UTC)
# Windows Task Scheduler: see scripts/register_daily_task.ps1
REFRESH_OHLC_ON_RUN = True  # full refresh is slow; set False to use cache+targeted
HTTP_SLEEP = 0.35
KRAKEN_PUBLIC = "https://api.kraken.com/0/public"

# Live API (env only — never hardcode secrets)
# FADE_API_KEY / FADE_API_SECRET / FADE_EXCHANGE


@dataclass(frozen=True)
class FadeRule:
    name: str
    pump_lookback: int = PUMP_LOOKBACK
    pump_min: float = PUMP_MIN
    min_rsi: float = MIN_RSI
    min_vol_spike: float = MIN_VOL_SPIKE
    min_dist_sma20: float = MIN_DIST_SMA20
    min_avg_vol_usd_10d: float = MIN_AVG_VOL_USD_10D
    hold_days: int = HOLD_DAYS

    def describe(self) -> str:
        return (
            f"{self.name}: ret_{self.pump_lookback}d>={self.pump_min*100:.0f}% "
            f"RSI>={self.min_rsi:.0f} vol>={self.min_vol_spike}x "
            f"distSMA20>={self.min_dist_sma20*100:.0f}% "
            f"liq10d>=${self.min_avg_vol_usd_10d:,.0f} | "
            f"SHORT next open, exit close after {self.hold_days}d"
        )


def active_rule() -> FadeRule:
    vol = 5.0 if STRICT_VOL5 else MIN_VOL_SPIKE
    name = "FADE-BLOWOFF-T3-V5" if STRICT_VOL5 else "FADE-BLOWOFF-T3"
    return FadeRule(name=name, min_vol_spike=vol)


def margin_usd(equity_usd: float = EQUITY_USD) -> float:
    return max(0.0, equity_usd * RISK_PCT_PER_TRADE)


def position_notional(equity_usd: float = EQUITY_USD) -> float:
    """Notional = margin × leverage. A risk5% L2 → 10% equity."""
    return margin_usd(equity_usd) * LEVERAGE


def max_gross_exposure_usd(equity_usd: float = EQUITY_USD) -> float:
    return position_notional(equity_usd) * MAX_OPEN_POSITIONS


def profile_summary() -> str:
    eq = EQUITY_USD
    return (
        f"{PROFILE_NAME} | equity=${eq:.0f} | {MARGIN_MODE} {LEVERAGE}x | "
        f"risk {RISK_PCT_PER_TRADE*100:.0f}% (margin ${margin_usd(eq):.2f}, "
        f"notional ${position_notional(eq):.2f}) | max_pos={MAX_OPEN_POSITIONS} | "
        f"mode={EXECUTION_MODE} auto={FULL_AUTO}"
    )
