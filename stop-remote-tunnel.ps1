$ErrorActionPreference = 'Stop'
$pidFile = Join-Path $PSScriptRoot 'local-logs/remote-tunnel.pid'
if (-not (Test-Path -LiteralPath $pidFile)) { throw 'No recorded remote tunnel PID' }
$tunnelProcessId = [int](Get-Content -LiteralPath $pidFile -Raw)
$process = Get-CimInstance Win32_Process -Filter "ProcessId=$tunnelProcessId"
if (-not $process) {
    Write-Output 'Recorded tunnel has already exited'
    return
}
$expectedKnownHosts = (Join-Path $PSScriptRoot 'local-logs/remote-known-hosts').Replace('\', '/')
if ($process.Name -ne 'ssh.exe' -or
    $process.CommandLine -notmatch '127\.0\.0\.1:18080:127\.0\.0\.1:18080' -or
    $process.CommandLine -notmatch [regex]::Escape($expectedKnownHosts)) {
    throw 'Recorded PID is not the expected niucai SSH tunnel; refusing to stop it'
}
Stop-Process -Id $tunnelProcessId
Write-Output "Stopped niucai remote tunnel PID $tunnelProcessId"
