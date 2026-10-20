# elevate-install.ps1
# Runs both task installers elevated so they can register S4U (run when logged out).
# Invoked by the assistant via Start-Process -Verb RunAs; logs everything.

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$logs = Join-Path $here 'logs'
New-Item -ItemType Directory -Force -Path $logs | Out-Null
$log = Join-Path $logs 'install-elevated.log'

Start-Transcript -Path $log -Force | Out-Null
try {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $isAdmin = (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
    Write-Host "elevated: $isAdmin  user: $($id.Name)"

    Write-Host ''
    Write-Host '--- install-auto.ps1 -NoTest ---'
    & (Join-Path $here 'install-auto.ps1') -NoTest

    Write-Host ''
    Write-Host '--- install-crypto.ps1 -NoTest ---'
    & (Join-Path $here 'install-crypto.ps1') -NoTest

    Write-Host ''
    Write-Host '--- resulting principals ---'
    foreach ($t in Get-ScheduledTask -TaskName 'AutoTrader-*') {
        $xml = [xml](Export-ScheduledTask -TaskName $t.TaskName)
        $lt = $xml.Task.Principals.Principal.LogonType
        Write-Host ("{0}: LogonType={1}  state={2}" -f $t.TaskName, $lt, $t.State)
    }
}
catch {
    Write-Host "ERROR: $($_.Exception.Message)"
}
finally {
    Stop-Transcript | Out-Null
}
