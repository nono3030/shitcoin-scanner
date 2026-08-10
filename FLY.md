# Deploy sur Fly.io

Fly ne détectait rien car le repo n’avait **ni Dockerfile ni framework**.  
Ajoutés : `Dockerfile`, `requirements.txt`, `fly.toml`.

## 1. Prérequis

```powershell
# CLI: https://fly.io/docs/hands-on/install-flyctl/
fly auth login
cd C:\Users\arnau\shitcoin-scanner
```

## 2. Premier deploy (dashboard)

```powershell
# Si l’app existe déjà partiellement, skip launch et:
fly volumes create scanner_data --region cdg --size 1

# Secrets Bybit (ne jamais commit)
fly secrets set BYBIT_API_KEY="xxx" BYBIT_API_SECRET="yyy"

fly deploy
fly apps open
```

Si `fly launch` redemande un plan :

```powershell
fly launch --no-deploy --copy-config --name shitcoin-scanner --region cdg
fly volumes create scanner_data --region cdg --size 1
fly secrets set BYBIT_API_KEY="..." BYBIT_API_SECRET="..."
fly deploy
```

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
