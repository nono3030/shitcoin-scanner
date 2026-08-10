# Deploy shitcoin-scanner to Fly.io from THIS repo root (has Dockerfile).
# Usage:
#   cd C:\Users\arnau\shitcoin-scanner
#   powershell -ExecutionPolicy Bypass -File .\scripts\fly_deploy.ps1
# Optional: -App name -Region fra -SkipSecrets

param(
    [string]$App = "shitcoin-scanner",
    [string]$Region = "fra",
    [switch]$SkipSecrets,
    [switch]$CreateApp
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

if (-not (Test-Path "$Root\Dockerfile")) {
    Write-Error "Dockerfile not found in $Root — wrong directory or git pull missing."
}
if (-not (Get-Command fly -ErrorAction SilentlyContinue) -and -not (Get-Command flyctl -ErrorAction SilentlyContinue)) {
    Write-Error "flyctl not in PATH. Install: https://fly.io/docs/hands-on/install-flyctl/"
}
$Fly = if (Get-Command fly -ErrorAction SilentlyContinue) { "fly" } else { "flyctl" }

Write-Host "Root: $Root"
Write-Host "Using: $Fly | app=$App region=$Region"
Get-Item Dockerfile, fly.toml, requirements.txt | Format-Table Name, Length

if ($CreateApp) {
    & $Fly apps create $App 2>$null
}

# Volume (ignore error if exists)
& $Fly volumes create scanner_data --region $Region --size 1 -a $App 2>$null

if (-not $SkipSecrets) {
    Write-Host ""
    Write-Host "Set secrets if not already done:"
    Write-Host "  $Fly secrets set BYBIT_API_KEY=... BYBIT_API_SECRET=... -a $App"
    Write-Host ""
}

Write-Host "Deploying with explicit Dockerfile..."
& $Fly deploy -a $App --dockerfile Dockerfile --remote-only
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "OK. Open: $Fly apps open -a $App"
& $Fly status -a $App
