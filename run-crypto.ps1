# run-crypto.ps1
# Crypto sleeve: 24/7 momentum on Alpaca spot, long/flat.
#
#   .\run-crypto.ps1              # preview only
#   .\run-crypto.ps1 -Send        # trade now
#   .\run-crypto.ps1 -Unattended  # used by Task Scheduler, logs to logs\crypto-YYYY-MM-DD.log
#
# Unlike the ETF runner there is no market-hours gate: crypto does not close.
# No once-a-day marker either — it re-checks every four hours and acts on the
# drift between the current book and the target.

param(
    [switch]$Send,
    [switch]$Unattended
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$py = Join-Path $here 'python\.venv-torch\Scripts\python.exe'
$logs = Join-Path $here 'logs'
$kill = Join-Path $here 'python\output\KILL_SWITCH'
$logFile = Join-Path $logs ("crypto-{0}.log" -f (Get-Date).ToString('yyyy-MM-dd'))

New-Item -ItemType Directory -Force -Path $logs | Out-Null

function Log($msg) {
    $line = "{0}  {1}" -f (Get-Date).ToString('yyyy-MM-dd HH:mm:ss'), $msg
    Write-Host $line
    if ($Unattended) { Add-Content -Path $logFile -Value $line }
}

if (-not (Test-Path $py)) { Log "python venv missing at $py"; exit 1 }

if (-not $env:APCA_API_KEY_ID) {
    $env:APCA_API_KEY_ID = [Environment]::GetEnvironmentVariable('APCA_API_KEY_ID', 'User')
}
if (-not $env:APCA_API_SECRET_KEY) {
    $env:APCA_API_SECRET_KEY = [Environment]::GetEnvironmentVariable('APCA_API_SECRET_KEY', 'User')
}
if (-not $env:APCA_API_KEY_ID -or -not $env:APCA_API_SECRET_KEY) {
    Log 'Alpaca keys not found in the User environment.'
    exit 1
}

if (Test-Path $kill) {
    Log "kill switch present ($kill) - not trading"
    exit 0
}

Push-Location (Join-Path $here 'python')
try {
    $mode = if ($Send -or $Unattended) { '--send' } else { '--dry-run' }
    Log "crypto rebalance ($mode)"
    $out = & $py 'crypto_bot.py' $mode 2>&1 | Out-String
    if ($Unattended) { Add-Content -Path $logFile -Value $out } else { Write-Host $out }

    if ($LASTEXITCODE -ne 0) {
        Log "crypto_bot failed (exit $LASTEXITCODE) - legs may be incomplete, next run retries"
        exit 1
    }
    Log 'crypto rebalance complete'
}
finally {
    Pop-Location
}
