param([Parameter(Mandatory=$true)][string]$Destination)
$ErrorActionPreference = 'Stop'
$privateDirectory = Join-Path $env:LOCALAPPDATA 'niucai\kernel-managed'
$secrets = Import-Clixml -LiteralPath (Join-Path $privateDirectory 'secrets.clixml')
$token = [System.Net.NetworkCredential]::new('', $secrets.api).Password
$headers = @{Authorization="Bearer $token"}
$identity = Invoke-RestMethod -Uri 'http://127.0.0.1:8080/api/kernel/identity' -Headers $headers
$drain = Invoke-RestMethod -Uri 'http://127.0.0.1:8080/api/kernel/admission/drain' -Headers $headers -Method Post
if (!$drain.backup_ready) { throw 'Kernel is draining; finish/cancel tasks and reconcile LOST/process exits, then retry backup.' }
if (Test-Path -LiteralPath $Destination) { throw 'Use a new backup directory' }
New-Item -ItemType Directory -Path $Destination | Out-Null
$backupDirectory = (Resolve-Path -LiteralPath $Destination).Path
& icacls $backupDirectory /inheritance:r /grant:r "$($env:USERNAME):(OI)(CI)F" | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Cannot protect backup directory' }
function Container([string]$Service) {
    $ids = @(& docker ps -aq --filter label=com.docker.compose.project=niucai-managed --filter "label=com.docker.compose.service=$Service")
    if ($ids.Count -ne 1) { throw "Expected exactly one managed $Service container" }
    $label = & docker inspect --format '{{index .Config.Labels "com.niucai.managed"}}' $ids[0]
    if ($label -ne 'kernel-v2') { throw 'Managed project ownership mismatch' }
    return $ids[0]
}
$api = Container 'api'
$worker = Container 'worker'
$postgres = Container 'postgres'
& docker stop $worker $api | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Cannot stop writers; do not copy live state' }
& docker exec $postgres pg_dump -U niucai -d niucai -Fc -f /tmp/kernel-backup.dump
if ($LASTEXITCODE -ne 0) { throw 'Database backup failed; Kernel remains drained/stopped' }
& docker cp "${postgres}:/tmp/kernel-backup.dump" (Join-Path $backupDirectory 'postgres.dump')
if ($LASTEXITCODE -ne 0) { throw 'Database backup copy failed' }
foreach ($volume in @('workspace','pi_sessions')) {
    $name = "niucai-managed_$volume"
    $label = & docker volume inspect --format '{{index .Labels "com.niucai.managed"}}' $name
    if ($label -ne 'kernel-v2') { throw 'Volume ownership mismatch' }
    & docker run --rm --label com.niucai.managed=backup -v "${name}:/source:ro" -v "${backupDirectory}:/backup" --entrypoint python niucai-kernel:kernel-anywhere-v2 -c "import tarfile; t=tarfile.open('/backup/$volume.tar','w');t.add('/source',arcname='.');t.close()"
    if ($LASTEXITCODE -ne 0) { throw 'Volume backup failed' }
}
$files = @{}
foreach ($name in @('postgres.dump','workspace.tar','pi_sessions.tar')) {
    $files[$name] = (Get-FileHash -LiteralPath (Join-Path $backupDirectory $name) -Algorithm SHA256).Hash.ToLowerInvariant()
}
@{kernel_instance_id=$identity.kernel_instance_id; protocol_version=$identity.protocol_version;
  migration_head='f2d21013d006'; files=$files; state='cold-backup-source-stopped';
  credentials='Provision via secure storage at destination; Windows DPAPI is not portable';
  node_data='Not included; separately back up each node volume'} | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $backupDirectory 'manifest.json') -Encoding utf8
Write-Output "Cold backup saved to $backupDirectory. Source writers remain stopped and admission remains drained."
