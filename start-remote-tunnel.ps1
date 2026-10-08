param(
    [string]$ServerAddress = '101.126.159.10',
    [string]$SshUser = 'root',
    [string]$ExpectedFingerprint = 'SHA256:Giz6fAj5+Jxg7N5AB37GtyIpPIiGz+DYCUrjAKX8MWE',
    [ValidatePattern('^[a-zA-Z0-9-]+$')][string]$TunnelName = 'remote',
    [ValidateRange(1,65535)][int]$LocalKernelPort = 18080,
    [ValidateRange(1,65535)][int]$RemoteKernelPort = 18080,
    [ValidateRange(1,65535)][int]$LocalDesktopPort = 16901,
    [ValidateRange(1,65535)][int]$RemoteDesktopPort = 16901
)

$ErrorActionPreference = 'Stop'
if ($LocalKernelPort -eq $LocalDesktopPort) { throw 'Kernel and desktop local ports must differ' }
$sshDirectory = 'C:\Program Files\Git\usr\bin'
$sshExecutable = Join-Path $sshDirectory 'ssh.exe'
$scanExecutable = Join-Path $sshDirectory 'ssh-keyscan.exe'
$logDirectory = Join-Path $PSScriptRoot 'local-logs'
New-Item -ItemType Directory -Force $logDirectory | Out-Null
$pidFile = Join-Path $logDirectory "$TunnelName-tunnel.pid"
if (Test-Path -LiteralPath $pidFile) {
    $recordedId = [int](Get-Content -LiteralPath $pidFile -Raw)
    if (Get-Process -Id $recordedId -ErrorAction SilentlyContinue) {
        throw "Tunnel name '$TunnelName' already has a live recorded process; stop or inspect it first."
    }
}

foreach ($port in @($LocalKernelPort, $LocalDesktopPort)) {
    $probe = [System.Net.Sockets.TcpClient]::new()
    try {
        $probe.Connect('127.0.0.1', $port)
        throw "Local port $port is already in use; inspect the existing listener before starting another tunnel."
    } catch [System.Net.Sockets.SocketException] {
        # No existing listener.
    } finally {
        $probe.Dispose()
    }
}

$keyLines = @(& $scanExecutable -T 6 -t ed25519 $ServerAddress 2> (Join-Path $logDirectory "$TunnelName-keyscan.log") |
    Where-Object { $_ -match '^\S+ ssh-ed25519 ' })
if ($LASTEXITCODE -ne 0 -or $keyLines.Count -ne 1) { throw 'Unable to read server host key' }
$keyBytes = [Convert]::FromBase64String(($keyLines[0] -split ' ')[2])
$sha = [System.Security.Cryptography.SHA256]::Create()
try {
    $fingerprint = 'SHA256:' + [Convert]::ToBase64String($sha.ComputeHash($keyBytes)).TrimEnd('=')
} finally {
    $sha.Dispose()
}
if ($fingerprint -ne $ExpectedFingerprint) { throw "Server host key mismatch: $fingerprint" }
$knownHosts = Join-Path $logDirectory "$TunnelName-known-hosts"
[System.IO.File]::WriteAllText($knownHosts, $keyLines[0] + "`n", [System.Text.Encoding]::ASCII)

$sshArguments = @(
    '-N', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
    '-o', "UserKnownHostsFile=$($knownHosts.Replace('\', '/'))",
    '-o', 'ConnectTimeout=8', '-o', 'ExitOnForwardFailure=yes',
    '-o', 'ServerAliveInterval=30', '-o', 'ServerAliveCountMax=3',
    '-i', "$($env:USERPROFILE.Replace('\', '/'))/.ssh/id_ed25519",
    '-i', "$($env:USERPROFILE.Replace('\', '/'))/.ssh/id_rsa",
    '-L', "127.0.0.1:${LocalKernelPort}:127.0.0.1:${RemoteKernelPort}",
    '-L', "127.0.0.1:${LocalDesktopPort}:127.0.0.1:${RemoteDesktopPort}",
    "$SshUser@$ServerAddress"
)
$tunnel = Start-Process -FilePath $sshExecutable -ArgumentList $sshArguments -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput (Join-Path $logDirectory "$TunnelName-tunnel.stdout.log") `
    -RedirectStandardError (Join-Path $logDirectory "$TunnelName-tunnel.stderr.log")
Set-Content -LiteralPath (Join-Path $logDirectory "$TunnelName-tunnel.pid") -Value $tunnel.Id -Encoding ascii
Start-Sleep -Seconds 2
$tunnel.Refresh()
if ($tunnel.HasExited) {
    $failure = Get-Content (Join-Path $logDirectory "$TunnelName-tunnel.stderr.log") -Raw
    throw "SSH tunnel exited: $failure"
}
Write-Output "SSH tunnel running, PID $($tunnel.Id), verified host $ServerAddress"
Write-Output "Kernel: http://127.0.0.1:$LocalKernelPort"
Write-Output "Desktop: http://127.0.0.1:$LocalDesktopPort/vnc.html"
