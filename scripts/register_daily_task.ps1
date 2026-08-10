# Register Windows Task Scheduler job for fade bot (Profil A risk 5%).
# Run ONCE in PowerShell (Admin optional if your user can create tasks):
#   cd C:\Users\ArnaudLavesque\shitcoin-scanner
#   powershell -ExecutionPolicy Bypass -File .\scripts\register_daily_task.ps1

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $Python) {
    # common install path
    $Python = "C:\Python314\python.exe"
}
if (-not (Test-Path $Python)) {
    Write-Error "Python not found. Edit this script to set `$Python path."
}

$TaskName = "FadeBot-Daily-A-Risk5"
$WorkDir = $Root
$LogDir = Join-Path $Root "out"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

# ~00:20 UTC ≈ adjust for your timezone.
# France été UTC+2 → 00:20 UTC = 02:20 local
# France hiver UTC+1 → 00:20 UTC = 01:20 local
# We schedule 02:30 local as a simple default (after daily UTC close).
$LocalTime = "02:30"

$Arg = "`"$Root\run_daily.py`""
# Use cache most days for speed; weekly full refresh optional separately
$Arg = "`"$Root\run_daily.py`" --no-refresh"

$Action = New-ScheduledTaskAction `
    -Execute $Python `
    -Argument $Arg `
    -WorkingDirectory $WorkDir

$Trigger = New-ScheduledTaskTrigger -Daily -At $LocalTime

$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2)

$Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Principal $Principal `
    -Force | Out-Null

Write-Host "OK — Task '$TaskName' registered daily at $LocalTime local"
Write-Host "  Python: $Python"
Write-Host "  WorkDir: $WorkDir"
Write-Host "  Command: python run_daily.py --no-refresh"
Write-Host ""
Write-Host "Test now:"
Write-Host "  python `"$Root\run_daily.py`" --no-refresh"
Write-Host "  schtasks /Run /TN `"$TaskName`""
Write-Host ""
Write-Host "Remove later:"
Write-Host "  Unregister-ScheduledTask -TaskName `"$TaskName`" -Confirm:`$false"
