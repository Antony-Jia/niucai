param(
    [Parameter(Mandatory=$true)][string]$SshHost,
    [ValidateRange(1024,65535)][int]$RemotePort = 18080,
    [ValidateRange(1024,65535)][int]$LocalPort = 8080,
    [string]$RemoteSocket,
    [switch]$Diagnose,
    [switch]$Once
)
$ErrorActionPreference = 'Stop'
if ($SshHost -notmatch '^[a-zA-Z0-9][a-zA-Z0-9._@-]*$') { throw 'Use an SSH config alias or user@host' }
if ($RemoteSocket -and $RemoteSocket -notmatch '^/[a-zA-Z0-9_./-]+/kernel\.sock$') { throw 'Use an absolute private kernel.sock path' }
if ($RemoteSocket -match '/\.\.?/') { throw 'Tunnel socket path cannot contain parent traversal' }
if ($Diagnose) {
    # Runs in the VPS host namespace; the package relay uses this same host loopback target.
    if ($RemoteSocket) {
        & ssh -o BatchMode=yes -o StrictHostKeyChecking=yes $SshHost "curl --fail --silent --max-time 10 --unix-socket $RemoteSocket http://localhost/healthz"
    } else {
        & ssh -o BatchMode=yes -o StrictHostKeyChecking=yes $SshHost "curl --fail --silent --max-time 10 http://127.0.0.1:$RemotePort/healthz"
    }
    if ($LASTEXITCODE -ne 0) { throw 'TUNNEL_UNREACHABLE: remote loopback cannot reach Kernel' }
    exit 0
}
do {
    Write-Output "Connecting SSH reverse tunnel: VPS loopback $RemotePort -> local loopback $LocalPort"
    if ($RemoteSocket) {
        $socketDirectory = $RemoteSocket.Substring(0, $RemoteSocket.LastIndexOf('/'))
        # sshd may leave the socket inode after a disconnect. Only remove the selected
        # socket inside a caller-owned 0700 directory; never delete a directory or files.
        $remoteCheck = 'test -d SOCKET_DIR && test -O SOCKET_DIR && test "$(stat -c %a SOCKET_DIR)" = 700 && { test ! -e SOCKET_PATH || { test -S SOCKET_PATH && rm -- SOCKET_PATH; }; }'
        $remoteCheck = $remoteCheck.Replace('SOCKET_DIR', $socketDirectory).Replace('SOCKET_PATH', $RemoteSocket)
        & ssh -T -o BatchMode=yes -o StrictHostKeyChecking=yes $SshHost $remoteCheck
        if ($LASTEXITCODE -ne 0) { throw 'Tunnel directory must be caller-owned 0700 and contain only a socket at the chosen path' }
    }
    $forward = if ($RemoteSocket) { "${RemoteSocket}:127.0.0.1:${LocalPort}" } else { "127.0.0.1:${RemotePort}:127.0.0.1:${LocalPort}" }
    & ssh -N -T -o BatchMode=yes -o StrictHostKeyChecking=yes -o StreamLocalBindUnlink=yes -o ExitOnForwardFailure=yes -o ServerAliveInterval=10 -o ServerAliveCountMax=3 -R $forward $SshHost
    if ($Once) { exit $LASTEXITCODE }
    Write-Output 'TUNNEL_DISCONNECTED: reconnecting in 5 seconds'
    Start-Sleep -Seconds 5
} while ($true)
