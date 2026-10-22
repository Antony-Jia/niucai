param(
    [ValidateSet('install','start','stop','rebuild','status','logs','diagnose','credential','configure-model','uninstall')]
    [string]$Action = 'status'
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$composePath = Join-Path $PSScriptRoot 'compose.managed.yml'
$privateDirectory = Join-Path $env:LOCALAPPDATA 'niucai\kernel-managed'
$credentialPath = Join-Path $privateDirectory 'secrets.clixml'
function New-Secret {
    $bytes = New-Object byte[] 32
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    return ([BitConverter]::ToString($bytes) -replace '-', '').ToLowerInvariant()
}
function Compose([string[]]$Arguments) {
    & docker compose --env-file NUL -p niucai-managed -f $composePath @Arguments
    if ($LASTEXITCODE -ne 0) { throw 'Managed Compose operation failed; inspect sanitized logs.' }
}
if ($Action -eq 'install' -and !(Test-Path -LiteralPath $credentialPath)) {
    New-Item -ItemType Directory -Force -Path $privateDirectory | Out-Null
    & icacls $privateDirectory /inheritance:r /grant:r "$($env:USERNAME):(OI)(CI)F" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Cannot protect managed credential directory' }
    @{api=(ConvertTo-SecureString (New-Secret) -AsPlainText -Force);
      postgres=(ConvertTo-SecureString (New-Secret) -AsPlainText -Force)} | Export-Clixml -LiteralPath $credentialPath
}
if ($Action -eq 'status' -and !(Test-Path -LiteralPath $credentialPath)) {
    & docker info --format '{{.OSType}}' 2>$null
    if ($LASTEXITCODE -ne 0) { Write-Output 'DOCKER_NOT_RUNNING' }
    Write-Output 'MANAGED_KERNEL_NOT_INSTALLED'
    exit 0
}
if (!(Test-Path -LiteralPath $credentialPath)) { throw 'Run install first' }
$secrets = Import-Clixml -LiteralPath $credentialPath
function Reveal($value) { return [System.Net.NetworkCredential]::new('', $value).Password }
if ($Action -eq 'credential') { Write-Output (Reveal $secrets.api); exit 0 }
if ($Action -eq 'configure-model') {
    if (!$env:NIUCAI_MANAGED_MODEL_KEY) { throw 'Model key is required through the private process environment' }
    $secrets.model = ConvertTo-SecureString $env:NIUCAI_MANAGED_MODEL_KEY -AsPlainText -Force
    $secrets | Export-Clixml -LiteralPath $credentialPath
    Write-Output 'MODEL_CREDENTIAL_SAVED'
    exit 0
}
$env:POSTGRES_PASSWORD = Reveal $secrets.postgres
$env:NIUCAI_API_TOKEN = Reveal $secrets.api
$env:NIUCAI_OPENAI_API_KEY = if ($secrets.model) { Reveal $secrets.model } else { '' }
try {
    $osType = & docker info --format '{{.OSType}}' 2>$null
    if ($LASTEXITCODE -ne 0) { throw 'DOCKER_NOT_RUNNING: start Docker Desktop' }
    if ($osType -ne 'linux') { throw 'DOCKER_NOT_LINUX: switch Docker Desktop to Linux containers' }
    # Refuse an existing project with the same name but without our ownership label.
    $containers = & docker ps -aq --filter label=com.docker.compose.project=niucai-managed
    foreach ($containerId in $containers) {
        $label = & docker inspect --format '{{index .Config.Labels "com.niucai.managed"}}' $containerId
        if ($label -ne 'kernel-v2') { throw 'Managed project name belongs to another installation' }
    }
    switch ($Action) {
        {$_ -in 'install','start','rebuild'} {
            $running = & docker ps -q --filter label=com.docker.compose.project=niucai-managed --filter label=com.docker.compose.service=api
            if (!$running) {
                $listener = Get-NetTCPConnection -LocalPort 8080 -State Listen -ErrorAction SilentlyContinue
                if ($listener) { throw 'PORT_CONFLICT: port 8080 is already in use' }
            }
            if ($Action -eq 'start') { Compose -Arguments @('up','-d','--wait') }
            else { Compose -Arguments @('up','-d','--build','--force-recreate','--wait') }
        }
        'stop' { Compose -Arguments @('stop') }
        'uninstall' { Compose -Arguments @('down'); Write-Output 'Containers removed; persistent volumes and credentials retained.' }
        'status' { Compose -Arguments @('ps','--format','json') }
        'logs' {
            $logs = Compose -Arguments @('logs','--no-color','--tail','100','api','worker','migrate')
            foreach ($line in $logs) {
                foreach ($secret in @($env:NIUCAI_API_TOKEN,$env:POSTGRES_PASSWORD,$env:NIUCAI_OPENAI_API_KEY)) {
                    if ($secret) { $line = $line.Replace($secret, '[REDACTED]') }
                }
                Write-Output $line
            }
        }
        'diagnose' {
            $headers = @{Authorization="Bearer $($env:NIUCAI_API_TOKEN)"}
            Invoke-RestMethod -Uri 'http://127.0.0.1:8080/api/kernel/diagnostics' -Headers $headers -TimeoutSec 10 | ConvertTo-Json -Depth 8
        }
    }
} finally {
    Remove-Item Env:POSTGRES_PASSWORD,Env:NIUCAI_API_TOKEN,Env:NIUCAI_OPENAI_API_KEY -ErrorAction SilentlyContinue
}
