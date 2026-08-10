# Shitcoin Fade Scanner (Kraken)

Backtest-validé + scanner daily + paper book + job auto (`run_daily.py`).

**Profil actif (config.py)** : `A_RISK5_L2_MAX3` — cross 2x, risk 5% marge/trade, max 3 pos, equity $100 paper.

> Not financial advice. High drawdown profile (sample MDD ~-67%). Paper first.

Docs auto : [AUTOMATION.md](./AUTOMATION.md)

## Règle tradée : FADE-BLOWOFF-T3

```
IF  ret_3d >= +40%
AND RSI(14) >= 70
AND vol_spike >= 3× (vs avg 20j)
AND close/SMA20 - 1 >= +20%
AND avg_vol_usd_10d >= $10,000
THEN short next daily open
EXIT  close after 3 daily bars (time exit, no tight SL)
```

Variant stricte : dans `config.py` mettre `STRICT_VOL5 = True` (vol ≥ 5×).

## Setup

```powershell
cd C:\Users\ArnaudLavesque\shitcoin-scanner
# premier run (télécharge OHLC, ~6 min)
python backtest_fade.py --refresh
```

## Dashboard perfs (interface)

```powershell
python dashboard.py              # génère + ouvre le navigateur
python dashboard.py --serve      # live http://127.0.0.1:8765 (recommandé pour les charts)
```

Affiche :
- equity / P&amp;L paper, positions, signaux, backtest
- **chart style TradingView** (Lightweight Charts) : bougies daily, volume, marqueurs SIG / SHORT / EXIT, ligne d’entrée

Clique une ligne de trade ou le bouton **Chart** pour focus la paire. Zoom scroll, pan drag.

Fichier : `out/dashboard.html`

## Scanner daily

```powershell
python scan_fade_signals.py           # cache
python scan_fade_signals.py --refresh # full refresh
python scan_fade_signals.py --near    # near-misses
python scan_fade_signals.py --paper-open
```

Output : `out/fade_signals_latest.json`

## Paper book

```powershell
python paper_book.py status
python paper_book.py fill-opens   # pending -> open @ next open
python paper_book.py mark         # MTM
python paper_book.py close-due    # time exit
```

Config risk dans `config.py` :
- `PAPER_EQUITY_USD` (ex: 200)
- `RISK_PCT_PER_TRADE` (0.5%)
- `ASSUMED_ADVERSE_MOVE` (40%)
- `MAX_OPEN_POSITIONS` (3)

Notional ≈ `equity * risk_pct / adverse_move`, cap 10% equity.

## Backtest

```powershell
python backtest_fade.py
python robustness_g.py
```

## Roadmap bot live (suivant)

1. Kraken Futures (ou autre) pour shorts réels — spot Kraken ne short pas facilement les alts
2. API keys en env vars (jamais en repo)
3. Module `bot/` : scan → risk check → place order → manage time exit
4. Start tiny (`PAPER_EQUITY` scale) après 1–2 semaines paper

## Fichiers clés

| Fichier | Rôle |
|---------|------|
| `config.py` | règle + risk |
| `scan_fade_signals.py` | scan daily |
| `paper_book.py` | carnet paper |
| `backtest_fade.py` | backtest multi-règles |
| `cache/ohlc_daily.json` | OHLC cache |
| `out/` | outputs JSON |
| `paper/` | état paper |
