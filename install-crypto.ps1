# install-crypto.ps1
# Registers the crypto sleeve to run every four hours, around the clock.
#
#   .\install-crypto.ps1
#   .\install-crypto.ps1 -NoTest

param([switch]$NoTest)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$taskName = 'AutoTrader-Crypto'

$action = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$here\run-crypto.ps1`" -Unattended" `
    -WorkingDirectory $here

# Daily anchor at 00:05, repeating every four hours for a full day.
$start = [datetime]::Today.Add([timespan]::Parse('00:05'))
$trigger = New-ScheduledTaskTrigger -Daily -At $start
$trigger.Repetition = (New-ScheduledTaskTrigger -Once -At $start `
    -RepetitionInterval (New-TimeSpan -Hours 4) `
    -RepetitionDuration (New-TimeSpan -Days 1)).Repetition

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -WakeToRun `
    -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 20)

$principal = "$env:USERDOMAIN\$env:USERNAME"

Write-Host "Registering '$taskName' every 4h, first run $($start.ToString('yyyy-MM-dd HH:mm'))" -ForegroundColor Cyan

$registered = $false
foreach ($mode in @('S4U', 'Interactive')) {
    try {
        $p = New-ScheduledTaskPrincipal -UserId $principal -LogonType $mode -RunLevel Limited
        Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
            -Settings $settings -Principal $p -Force `
            -Description 'Alpaca PAPER crypto momentum sleeve: 4-hourly rebalance, no real money.' | Out-Null
        Write-Host "Registered ($mode)." -ForegroundColor Green
        $registered = $true
        break
    }
    catch {
        Write-Host "Registration as $mode failed: $($_.Exception.Message)" -ForegroundColor Yellow
    }
}
if (-not $registered) { Write-Host 'Run this from an elevated PowerShell.' -ForegroundColor Red; exit 1 }

if ($NoTest) { exit 0 }

Write-Host ''
Write-Host 'Test run...' -ForegroundColor Cyan
Start-ScheduledTask -TaskName $taskName
Start-Sleep -Seconds 20

$info = Get-ScheduledTaskInfo -TaskName $taskName
Write-Host ("Last run : {0}  result {1}" -f $info.LastRunTime, $info.LastTaskResult)
Write-Host ("Next run : {0}" -f $info.NextRunTime)

$log = Join-Path $here ("logs\crypto-{0}.log" -f (Get-Date).ToString('yyyy-MM-dd'))
if (Test-Path $log) {
    Write-Host ''
    Get-Content $log -Tail 12
}
