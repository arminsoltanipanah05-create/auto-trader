# run-paper.ps1
# Daily Alpaca PAPER routine. Manual by default, unattended with -Unattended.
#
#   .\run-paper.ps1                 # connection + today's book + preview (sends nothing)
#   .\run-paper.ps1 -Send           # preview, then submit after you type SEND
#   .\run-paper.ps1 -Unattended     # used by Task Scheduler: gates on market hours,
#                                   # trades at most once per day, logs to logs\paper-YYYY-MM-DD.log
#
# There is deliberately no -Live switch. Going live is a separate, deliberate
# act: edit config.toml and pass --live yourself.

param(
    [switch]$Send,
    [switch]$Unattended,
    [switch]$Refresh
)

$ErrorActionPreference = 'Stop'
$here   = Split-Path -Parent $MyInvocation.MyCommand.Path
$py     = Join-Path $here 'python\.venv-torch\Scripts\python.exe'
$logs   = Join-Path $here 'logs'
$marker = Join-Path $logs 'last-trade.txt'
$today  = (Get-Date).ToString('yyyy-MM-dd')
$logFile = Join-Path $logs ("paper-{0}.log" -f $today)

New-Item -ItemType Directory -Force -Path $logs | Out-Null

function Log($msg) {
    $line = "{0}  {1}" -f (Get-Date).ToString('yyyy-MM-dd HH:mm:ss'), $msg
    Write-Host $line
    if ($Unattended) { Add-Content -Path $logFile -Value $line }
}

function Emit($text) {
    if ($Unattended) { Add-Content -Path $logFile -Value $text }
    else { Write-Host $text }
}

if (-not (Test-Path $py)) {
    Log "python venv missing at $py"
    exit 1
}

# Keys come from the User environment; read them from the registry so a task
# scheduler run (which may not inherit the session) still finds them.
if (-not $env:APCA_API_KEY_ID) {
    $env:APCA_API_KEY_ID = [Environment]::GetEnvironmentVariable('APCA_API_KEY_ID', 'User')
}
if (-not $env:APCA_API_SECRET_KEY) {
    $env:APCA_API_SECRET_KEY = [Environment]::GetEnvironmentVariable('APCA_API_SECRET_KEY', 'User')
}
if (-not $env:APCA_API_KEY_ID -or -not $env:APCA_API_SECRET_KEY) {
    Log 'Alpaca PAPER keys not found. Run: setx APCA_API_KEY_ID "PK..." and setx APCA_API_SECRET_KEY "..."'
    exit 1
}

# ---------------------------------------------------------------- unattended gate
if ($Unattended) {
    if (Test-Path $marker) {
        $last = (Get-Content $marker -Raw).Trim()
        if ($last -eq $today) { Log "already traded today ($last) - exiting"; exit 0 }
    }

    try {
        $headers = @{
            'APCA-API-KEY-ID'     = $env:APCA_API_KEY_ID
            'APCA-API-SECRET-KEY' = $env:APCA_API_SECRET_KEY
        }
        $clock = Invoke-RestMethod -Uri 'https://paper-api.alpaca.markets/v2/clock' `
                                   -Headers $headers -TimeoutSec 20
    }
    catch {
        Log "clock check failed: $($_.Exception.Message)"
        exit 1
    }

    if (-not $clock.is_open) {
        Log "market closed (next open $($clock.next_open)) - exiting"
        exit 0
    }

    $mins = ([datetime]$clock.next_close - (Get-Date)).TotalMinutes
    if ($mins -gt 45) { Log ("open, {0:N0} min to close - too early, a later run will take it" -f $mins); exit 0 }
    if ($mins -lt 3)  { Log ("open, only {0:N1} min to close - too late, skipping" -f $mins); exit 0 }
    Log ("open, {0:N0} min to close - trading" -f $mins)
    $Send = $true
}

# ------------------------------------------------------------------ connection
Log '=== 1/4 connection ==='
$conn = & (Join-Path $here 'check-alpaca.ps1') 2>&1 | Out-String
Emit $conn
if ($LASTEXITCODE -ne 0) {
    Log 'connection failed - stopping, nothing was traded'
    exit 1
}

# ------------------------------------------------------------------- today's book
Log '=== 2/4 today''s book ==='
Push-Location (Join-Path $here 'python')
try {
    $pyArgs = @('predict_live.py', '--strategy', 'tsmom')
    if ($Refresh) { $pyArgs += '--refresh' }
    $book = & $py @pyArgs 2>&1 | Out-String
    Emit $book
    if ($LASTEXITCODE -ne 0) { Log 'predict_live failed - stopping'; exit 1 }

    # ---------------------------------------------------------------- orders
    Log '=== 3/4 orders ==='
    if ($Send) {
        if (-not $Unattended) {
            Write-Host 'About to submit to the PAPER account.' -ForegroundColor Yellow
            if ((Read-Host 'Type SEND to continue') -ne 'SEND') {
                Log 'cancelled by user'
                exit 0
            }
        }
        $orders = & $py 'execute.py' '--send' 2>&1 | Out-String
        Emit $orders
        if ($LASTEXITCODE -ne 0) {
            Log 'execute failed - book may be incomplete, no marker written (a later run will retry)'
            exit 1
        }
    }
    else {
        $preview = & $py 'execute.py' 2>&1 | Out-String
        Emit $preview
    }

    # ----------------------------------------------------------------- status
    Log '=== 4/4 status ==='
    $status = & $py 'status.py' 2>&1 | Out-String
    Emit $status

    if ($Send -and $Unattended) {
        Set-Content -Path $marker -Value $today
        Log "done - marker set for $today"
    }
}
finally {
    Pop-Location
}
