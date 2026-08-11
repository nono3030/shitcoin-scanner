# Automatisation — Profil A risk 5%

## Profil

| | |
|--|--|
| Equity | $100 |
| Mode | Cross · **2x** |
| Risk | **5% marge / trade** → notional **10%** ($10) |
| Max pos | **3** |
| Hold | 3 daily bars time-exit |
| Signal | FADE-BLOWOFF-T3 |
| Exec | `live` Bybit (`config.EXECUTION_MODE`) |
| DCA soft | **ON** — add short +10%/+20% adverse, max **2×** first leg, block if account DD ≥ **35%** |

Backtest sample ~23m (single) : CAGR ~**+124%**, MDD ~**-67%**.  
Portfolio sim DCA soft : upside ↑, MDD ~**-57%** (stress high ~-70%).

---

## Process auto (le plus simple)

```
chaque jour (après close UTC)
  → run_daily.py
       1. scan signaux
       2. ouvre shorts paper (si slots libres)
       3. fill @ open
       4. close si hold >= 3j
       5. regen dashboard
```

### Lancer à la main

```powershell
cd C:\Users\ArnaudLavesque\shitcoin-scanner

# test rapide (cache)
python run_daily.py --no-refresh

# scan only
python run_daily.py --dry-run --no-refresh

# full refresh OHLC (~6 min) — 1× / semaine recommandé
python run_daily.py --refresh
```

### Planifier Windows (recommandé)

```powershell
cd C:\Users\ArnaudLavesque\shitcoin-scanner
powershell -ExecutionPolicy Bypass -File .\scripts\register_daily_task.ps1
```

- Tâche : `FadeBot-Daily-A-Risk5`
- Heure défaut : **02:30 locale** (après close daily UTC)
- Log : `out\bot_run.log`
- Dashboard : `out\dashboard.html` ou `python dashboard.py --serve`

### Voir les perfs

```powershell
python dashboard.py --serve
python paper_book.py status
```

### Reset paper (nouveau départ $100)

```powershell
python paper_book.py reset --force
# puis édite paper/open_positions.json equity_start si besoin, ou relance run_daily
python run_daily.py --no-refresh
```

---

## Passage LIVE (étape 2)

1. Choisir venue **perps** (Kraken Futures / Bybit…) — spot ne short pas facilement  
2. Mettre clés en env : `FADE_API_KEY`, `FADE_API_SECRET`  
3. Brancher un module `broker.py` (place short, reduce-only close)  
4. `EXECUTION_MODE = "live"` dans `config.py`  
5. Garder kill-switches : daily -20%, DD -70%, equity min $25  

**Ne passe pas live avant 1–2 semaines paper stable.**

---

## Checklist ops

| Quand | Action |
|-------|--------|
| Quotidien | Task Scheduler → `run_daily.py` |
| Hebdo | `python run_daily.py --refresh` |
| Après run | Check `out/bot_run.log` + dashboard |
| Signal douteux | `FULL_AUTO=False` pour pause |
