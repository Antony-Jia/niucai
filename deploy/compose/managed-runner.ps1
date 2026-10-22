param([Parameter(Mandatory=$true)][guid]$NodeId,
      [ValidateSet('install','start','stop','logs','rebuild','uninstall')][string]$Action = 'install')
$ErrorActionPreference = 'Stop'
$nodeDirectory = Join-Path $env:LOCALAPPDATA "niucai\runner-$NodeId"
$secretPath = Join-Path $nodeDirectory 'secrets.clixml'
$composePath = Join-Path $nodeDirectory 'compose.yml'
$sourcePath = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path.Replace('\','/')
if ($Action -eq 'install') {
    if (!$env:NIUCAI_INSTALL_NODE_TOKEN -or !$env:NIUCAI_INSTALL_MODEL_KEY) { throw 'Node token and model key required' }
    New-Item -ItemType Directory -Force -Path $nodeDirectory | Out-Null
    & icacls $nodeDirectory /inheritance:r /grant:r "$($env:USERNAME):(OI)(CI)F" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Cannot protect Runner credentials' }
    $values = @{}
    foreach ($key in @('NIUCAI_INSTALL_NODE_TOKEN','NIUCAI_INSTALL_MODEL_KEY','NIUCAI_INSTALL_PROVIDER','NIUCAI_INSTALL_MODEL','NIUCAI_INSTALL_BASE_URL','NIUCAI_INSTALL_KERNEL_ID')) {
        $value = [Environment]::GetEnvironmentVariable($key)
        if (!$value) { $value = '-' }
        $values[$key] = ConvertTo-SecureString $value -AsPlainText -Force
    }
    $values | Export-Clixml -LiteralPath $secretPath
    @'
services:
  runner:
    image: niucai-pi-node:kernel-anywhere-v2
    build:
      context: ${NIUCAI_SOURCE_PATH}
      dockerfile: services/node-runner/Dockerfile
    labels: {com.niucai.managed: "runner-v2"}
    environment:
      NIUCAI_KERNEL_URL: http://api:8080
      NIUCAI_NODE_ID: ${NIUCAI_NODE_ID}
      NIUCAI_NODE_TOKEN: ${NIUCAI_NODE_TOKEN}
      NIUCAI_KERNEL_INSTANCE_ID: ${NIUCAI_KERNEL_INSTANCE_ID}
      NIUCAI_PI_PROVIDER: ${NIUCAI_PI_PROVIDER}
      NIUCAI_PI_MODEL: ${NIUCAI_PI_MODEL}
      NIUCAI_PI_BASE_URL: ${NIUCAI_PI_BASE_URL}
      NIUCAI_PI_PROVIDER_KEY: ${NIUCAI_PI_PROVIDER_KEY}
      OPENROUTER_API_KEY: ${NIUCAI_PI_PROVIDER_KEY}
      OPENAI_API_KEY: ${NIUCAI_PI_PROVIDER_KEY}
      ANTHROPIC_API_KEY: ${NIUCAI_PI_PROVIDER_KEY}
    networks: [managed]
    volumes: [node-data:/data]
    read_only: true
    tmpfs: [/tmp]
    cap_drop: [ALL]
    cap_add: [SETUID, SETGID, KILL, CHOWN, DAC_OVERRIDE]
    security_opt: [no-new-privileges:true]
    init: true
    restart: unless-stopped
networks:
  managed:
    external: true
    name: niucai-managed
volumes:
  node-data:
    labels: {com.niucai.managed: "runner-v2"}
'@ | Set-Content -LiteralPath $composePath -Encoding utf8
}
if (!(Test-Path -LiteralPath $secretPath)) { throw 'Runner not installed' }
$values = Import-Clixml -LiteralPath $secretPath
function Reveal($key) { return [System.Net.NetworkCredential]::new('', $values[$key]).Password }
$env:NIUCAI_SOURCE_PATH = $sourcePath
$env:NIUCAI_NODE_ID = "$NodeId"
$env:NIUCAI_NODE_TOKEN = Reveal 'NIUCAI_INSTALL_NODE_TOKEN'
$env:NIUCAI_KERNEL_INSTANCE_ID = Reveal 'NIUCAI_INSTALL_KERNEL_ID'
$env:NIUCAI_PI_PROVIDER = Reveal 'NIUCAI_INSTALL_PROVIDER'
$env:NIUCAI_PI_MODEL = Reveal 'NIUCAI_INSTALL_MODEL'
$env:NIUCAI_PI_BASE_URL = Reveal 'NIUCAI_INSTALL_BASE_URL'
if ($env:NIUCAI_PI_BASE_URL -eq '-') { $env:NIUCAI_PI_BASE_URL = '' }
$env:NIUCAI_PI_PROVIDER_KEY = Reveal 'NIUCAI_INSTALL_MODEL_KEY'
try {
    $networkLabel = & docker network inspect --format '{{index .Labels "com.niucai.managed"}}' niucai-managed
    if ($LASTEXITCODE -ne 0 -or $networkLabel -ne 'kernel-v2') { throw 'Managed Kernel network not available' }
    $project = "niucai-node-$NodeId"
    foreach ($containerId in (& docker ps -aq --filter "label=com.docker.compose.project=$project")) {
        $label = & docker inspect --format '{{index .Config.Labels "com.niucai.managed"}}' $containerId
        if ($label -ne 'runner-v2') { throw 'Runner project ownership mismatch' }
    }
    $argsForCompose = switch ($Action) {
        {$_ -in 'install','rebuild'} { @('up','-d','--build','--force-recreate') }
        'start' { @('up','-d') }
        'stop' { @('stop') }
        'logs' { @('logs','--no-color','--tail','100') }
        'uninstall' { @('down') }
    }
    $output = & docker compose --env-file NUL -p $project -f $composePath @argsForCompose
    if ($LASTEXITCODE -ne 0) { throw 'Managed Runner operation failed' }
    foreach ($line in $output) {
        $line = $line.Replace($env:NIUCAI_NODE_TOKEN, '[REDACTED]').Replace($env:NIUCAI_PI_PROVIDER_KEY, '[REDACTED]')
        Write-Output $line
    }
    Write-Output "RUNNER_CONFIGURED: $NodeId; wait for a real heartbeat and execute a test package."
} finally {
    foreach ($key in @('NIUCAI_NODE_TOKEN','NIUCAI_PI_PROVIDER_KEY')) { Remove-Item "Env:$key" -ErrorAction SilentlyContinue }
}
