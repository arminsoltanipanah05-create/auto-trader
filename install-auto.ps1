# install-auto.ps1
# Registers the daily paper-trading job in Windows Task Scheduler.
#
#   .\install-auto.ps1                 # install and immediately test-run it
#   .\install-auto.ps1 -NoTest         # install only
#   .\install-auto.ps1 -Time 21:15     # change the start time (local clock)
#
# The trigger repeats every 15 minutes for two hours and the script itself
# decides whether to trade. That absorbs daylight-saving drift between Europe
# and New York, and a missed run gets picked up by a later one.

param(
    [string]$Time = '21:15',
    [switch]$NoTest
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$taskName = 'AutoTrader-Paper'

$action = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$here\run-paper.ps1`" -Unattended" `
    -WorkingDirectory $here

$start = [datetime]::Today.Add([timespan]::Parse($Time))

$trigger = New-ScheduledTaskTrigger -Daily -At $start
$repetition = (New-ScheduledTaskTrigger -Once -At $start `
    -RepetitionInterval (New-TimeSpan -Minutes 15) `
    -RepetitionDuration (New-TimeSpan -Hours 2)).Repetition
$trigger.Repetition = $repetition

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -WakeToRun `
    -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30)

$principal = "$env:USERDOMAIN\$env:USERNAME"

Write-Host "Registering '$taskName' for $principal, first run $($start.ToString('yyyy-MM-dd HH:mm'))" -ForegroundColor Cyan

$registered = $false
foreach ($mode in @('S4U', 'Interactive')) {
    try {
        $principalObj = New-ScheduledTaskPrincipal -UserId $principal -LogonType $mode -RunLevel Limited
        Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
            -Settings $settings -Principal $principalObj -Force `
            -Description 'Alpaca PAPER trading bot: daily TSMOM rebalance, no real money.' | Out-Null
        if ($mode -eq 'S4U') {
            Write-Host 'Registered (runs whether or not you are logged on).' -ForegroundColor Green
        }
        else {
            Write-Host 'Registered (runs only while you are logged on).' -ForegroundColor Green
        }
        $registered = $true
        break
    }
    catch {
        Write-Host "Registration as $mode failed: $($_.Exception.Message)" -ForegroundColor Yellow
    }
}

if (-not $registered) {
    Write-Host 'Could not register the task. Run this script from an elevated PowerShell window.' -ForegroundColor Red
    exit 1
}

if ($NoTest) { exit 0 }

Write-Host ''
Write-Host 'Test run...' -ForegroundColor Cyan
Start-ScheduledTask -TaskName $taskName
Start-Sleep -Seconds 25

$info = Get-ScheduledTaskInfo -TaskName $taskName
Write-Host ("Last run  : {0}" -f $info.LastRunTime)
Write-Host ("Result    : {0}" -f $info.LastTaskResult)
Write-Host ("Next run  : {0}" -f $info.NextRunTime)

$log = Join-Path $here ("logs\paper-{0}.log" -f (Get-Date).ToString('yyyy-MM-dd'))
if (Test-Path $log) {
    Write-Host ''
    Write-Host "--- $log ---" -ForegroundColor Cyan
    Get-Content $log -Tail 25
}
else {
    Write-Host ''
    Write-Host 'No log yet. If the task says it ran, the script failed before logging.' -ForegroundColor Yellow
}
