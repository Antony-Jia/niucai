param([switch]$SkipBuild, [switch]$MultiBot, [switch]$Codex,
      [ValidatePattern('^niucai-[a-z0-9-]+$')][string]$Project = 'niucai-remote-pi',
      [ValidateRange(1024,65535)][int]$ApiPort = 38180)
$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$demoDirectory = Join-Path $repoRoot $(if ($Project -eq 'niucai-remote-pi') { 'build-cache/remote-pi' } else { "build-cache/$Project" })
New-Item -ItemType Directory -Force -Path $demoDirectory | Out-Null
& icacls $demoDirectory /inheritance:r /grant:r "$($env:USERNAME):(OI)(CI)F" | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Cannot protect simulation credentials' }
$envPath = Join-Path $demoDirectory '.env'
$composePath = Join-Path $PSScriptRoot 'compose.remote-pi.yml'
function New-DemoSecret {
    return [Convert]::ToHexString([Security.Cryptography.RandomNumberGenerator]::GetBytes(24)).ToLowerInvariant()
}
if (!(Test-Path -LiteralPath $envPath)) {
    @(
        "REMOTE_API_TOKEN=$(New-DemoSecret)"
        "REMOTE_DB_PASSWORD=$(New-DemoSecret)"
        "REMOTE_API_PORT=$ApiPort"
        "REMOTE_NODE_A_ID=$([guid]::NewGuid())"
        "REMOTE_NODE_A_TOKEN=$(New-DemoSecret)"
        "REMOTE_NODE_B_ID=$([guid]::NewGuid())"
        "REMOTE_NODE_B_TOKEN=$(New-DemoSecret)"
    ) | Set-Content -LiteralPath $envPath -Encoding utf8
}
$values = @{}
Get-Content -LiteralPath $envPath | ForEach-Object {
    $parts = $_.Split('=', 2); $values[$parts[0]] = $parts[1]
}
$values['REMOTE_MULTI_BOT_ENABLED'] = if ($MultiBot) { 'true' } else { 'false' }
$values['REMOTE_WORKER_SLOTS'] = if ($MultiBot) { '3' } else { '1' }
$values.GetEnumerator() | Sort-Object Name | ForEach-Object { "$($_.Key)=$($_.Value)" } | Set-Content -LiteralPath $envPath -Encoding utf8
if (!$SkipBuild) {
    docker compose --env-file $envPath -p $Project -f $composePath $(if ($Codex) { '--profile', 'codex' }) build api node-a $(if ($Codex) { 'node-codex' })
    if ($LASTEXITCODE -ne 0) { throw 'Docker build failed' }
}
docker compose --env-file $envPath -p $Project -f $composePath up -d --wait api model worker
if ($LASTEXITCODE -ne 0) { throw 'Kernel startup failed' }
$headers = @{ Authorization = "Bearer $($values['REMOTE_API_TOKEN'])" }
$baseUrl = "http://127.0.0.1:$($values['REMOTE_API_PORT'])"
$nodes = Invoke-RestMethod -Uri "$baseUrl/api/remote/nodes" -Headers $headers
$suffixes = @('A', 'B')
if ($Codex) { $suffixes += 'C' }
foreach ($suffix in $suffixes) {
    if ($nodes.id -notcontains $values["REMOTE_NODE_${suffix}_ID"]) {
        $newNode = Invoke-RestMethod -Uri "$baseUrl/api/remote/nodes" -Headers $headers -Method Post -ContentType 'application/json' -Body (@{ name = "Docker node $suffix" } | ConvertTo-Json)
        $values["REMOTE_NODE_${suffix}_ID"] = $newNode.id
        $values["REMOTE_NODE_${suffix}_TOKEN"] = $newNode.token
    }
}
$values.GetEnumerator() | Sort-Object Name | ForEach-Object { "$($_.Key)=$($_.Value)" } | Set-Content -LiteralPath $envPath -Encoding utf8
docker compose --env-file $envPath -p $Project -f $composePath $(if ($Codex) { '--profile', 'codex' }) up -d --wait node-a node-b $(if ($Codex) { 'node-codex' })
if ($LASTEXITCODE -ne 0) { throw 'Node startup failed' }
Write-Host "Simulation Kernel: $baseUrl"
Write-Host "Local credentials saved in $envPath (not printed)."
