# Deploy sur Fly.io

Fly ne détectait rien car le repo n’avait **ni Dockerfile ni framework**.  
Ajoutés : `Dockerfile`, `requirements.txt`, `fly.toml`.

## ⚠️ Ne pas utiliser le wizard `fly launch plan propose`

L’erreur :
`Could not find a Dockerfile, nor detect a runtime`
arrive quand Fly analyse un **contexte vide** (UI web / plan propose sans checkout local).

**Le Dockerfile est à la racine du repo** (commit `5b6af6c`+).  
Deploy **depuis le clone local** avec `fly deploy --dockerfile Dockerfile`.

## 1. Prérequis

```powershell
# CLI: https://fly.io/docs/hands-on/install-flyctl/
fly auth login
cd C:\Users\arnau\shitcoin-scanner
git pull
dir Dockerfile   # DOIT exister — sinon mauvais dossier
```

## 2. Deploy sans wizard (recommandé)

```powershell
cd C:\Users\arnau\shitcoin-scanner
git pull

# une fois
fly apps create shitcoin-scanner
fly volumes create scanner_data --region fra --size 1 -a shitcoin-scanner
fly secrets set BYBIT_API_KEY="xxx" BYBIT_API_SECRET="yyy" -a shitcoin-scanner

# chaque release
fly deploy -a shitcoin-scanner --dockerfile Dockerfile --remote-only
fly apps open -a shitcoin-scanner
```

Ou script :

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\fly_deploy.ps1 -CreateApp
```

## 2b. Si tu avais déjà lancé un launch cassé

```powershell
cd C:\Users\arnau\shitcoin-scanner
git pull
# ignore le wizard ; force le Dockerfile
fly deploy -a shitcoin-scanner --dockerfile Dockerfile --remote-only
```

Si l’app n’existe pas encore : `fly apps create shitcoin-scanner` puis volume + secrets + deploy.

## 3. Cron daily (trading, post-close)

Fenêtre bot = **00:00–04:00 UTC**. Exemple 00:15 UTC :

```powershell
fly machine run . `
  --region cdg `
  --schedule "15 0 * * *" `
  --env DATA_ROOT=/data `
  --command "sh scripts/fly_daily.sh" `
  --volume scanner_data:/data `
  --restart no
```

(Adapte le nom du volume si différent : `fly volumes list`.)

## 4. Vérifs

```powershell
fly status
fly logs
fly ssh console -C "python broker_bybit.py ping"
```

## 5. Local vs Fly

| | Local | Fly |
|--|-------|-----|
| DATA_ROOT | repo | `/data` (volume) |
| BIND | 127.0.0.1 | 0.0.0.0 |
| PORT | 8765 | 8080 (Fly proxy) |
| Keys | `live/.env` | `fly secrets` |

## Notes

- Auto-stop machines OK pour le **dashboard** (se réveille à la visite).
- Le **cron** doit tourner sur une machine schedule (pas le web process).
- Premier `run_daily --refresh` peut prendre ~5–10 min (OHLC Kraken).
