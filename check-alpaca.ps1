# check-alpaca.ps1
# Verifies Alpaca paper credentials without printing them.
# Usage:
#   $env:APCA_API_KEY_ID     = "..."
#   $env:APCA_API_SECRET_KEY = "..."
#   .\check-alpaca.ps1

$ErrorActionPreference = 'Stop'

$base = 'https://paper-api.alpaca.markets'

if (-not $env:APCA_API_KEY_ID -or -not $env:APCA_API_SECRET_KEY) {
    Write-Host 'MISSING: set APCA_API_KEY_ID and APCA_API_SECRET_KEY first.' -ForegroundColor Red
    exit 1
}

# Show only a fingerprint, never the key itself.
$id = $env:APCA_API_KEY_ID
Write-Host ("Key id  : {0}...{1}  (length {2})" -f $id.Substring(0,[Math]::Min(4,$id.Length)), $id.Substring($id.Length-2), $id.Length)

$headers = @{
    'APCA-API-KEY-ID'     = $env:APCA_API_KEY_ID
    'APCA-API-SECRET-KEY' = $env:APCA_API_SECRET_KEY
}

function Probe($path, $label) {
    try {
        $r = Invoke-RestMethod -Uri "$base$path" -Headers $headers -Method Get -TimeoutSec 20
        Write-Host ("OK    {0}" -f $label) -ForegroundColor Green
        return $r
    }
    catch {
        $code = $null
        if ($_.Exception.Response) { $code = [int]$_.Exception.Response.StatusCode }
        Write-Host ("FAIL  {0} -> HTTP {1}" -f $label, $code) -ForegroundColor Red
        switch ($code) {
            401 { Write-Host '      Key malformed: check for trailing space or newline.' }
            403 { Write-Host '      Key rejected. Live keys on the paper host? Or keys regenerated?' }
            404 { Write-Host '      Wrong host or path.' }
            default { Write-Host ('      ' + $_.Exception.Message) }
        }
        return $null
    }
}

$acct  = Probe '/v2/account' 'account'
$clock = Probe '/v2/clock'   'clock'

if ($acct) {
    Write-Host ''
    Write-Host ("Account : {0}" -f $acct.account_number)
    Write-Host ("Status  : {0}" -f $acct.status)
    Write-Host ("Equity  : {0} {1}" -f $acct.currency, $acct.equity)
    Write-Host ("Buying  : {0} {1}" -f $acct.currency, $acct.buying_power)
    Write-Host ("Blocked : {0}" -f $acct.trading_blocked)
    if ($acct.status -ne 'ACTIVE') {
        Write-Host 'WARNING: account is not ACTIVE. Orders will be rejected.' -ForegroundColor Yellow
    }
    if ($acct.trading_blocked -eq $true) {
        Write-Host 'WARNING: trading_blocked is true.' -ForegroundColor Yellow
    }
}

if ($clock) {
    Write-Host ("Market  : open={0}  next_open={1}" -f $clock.is_open, $clock.next_open)
}

Write-Host ''
if ($acct -and $clock) {
    Write-Host 'PAPER CONNECTION OK' -ForegroundColor Green
    exit 0
} else {
    Write-Host 'CONNECTION NOT READY' -ForegroundColor Red
    exit 1
}
