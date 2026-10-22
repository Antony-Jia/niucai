# Run the Kernel test suite in Linux containers: SQLite first, then a throwaway PostgreSQL.
# Uses the working tree (read-only mount) and the built Kernel image, which carries Node and the Pi runtime.
# Extra arguments go to pytest, e.g. .\container-test.ps1 -Database postgres -- tests/test_remote_pi.py -x
[CmdletBinding(PositionalBinding = $false)]
param(
    [ValidateSet('all','sqlite','postgres')]
    [string]$Database = 'all',
    [string]$Image = 'niucai-kernel:kernel-anywhere-v2',
    [string]$PostgresImage = 'pgvector/pgvector:pg17',
    [Parameter(ValueFromRemainingArguments)]
    [string[]]$PytestArgs = @()
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$kernel = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$project = 'niucai-ctest'
$network = "$project-net"
$postgres = "$project-pg"
$labels = @('--label', 'com.niucai.acceptance=ctest', '--label', "com.docker.compose.project=$project")

& docker image inspect $Image *> $null
if ($LASTEXITCODE -ne 0) { throw "Image $Image not found; build it with managed-kernel.ps1 -Action rebuild" }

function Invoke-Suite([string]$Name, [string[]]$Extra) {
    Write-Host "== $Name"
    # Copy out of the mount so the Linux venv and caches never touch the Windows tree.
    $script = 'set -e; mkdir /work; tar -C /src --exclude=.venv --exclude=__pycache__ --exclude=.pytest_cache -cf - . | tar -C /work -xf -; ' +
              'cd /work; uv sync --frozen --extra harness --extra browser -q; exec uv run --no-sync pytest -q -p no:cacheprovider -rs "$@"'
    & docker run --rm --name "$project-run" --user root @labels @Extra `
        -v "${kernel}:/src:ro" -v "${project}-uv:/root/.cache/uv" `
        -e UV_PROJECT_ENVIRONMENT=/opt/testenv -e UV_LINK_MODE=copy `
        $Image sh -c $script pytest @PytestArgs | Out-Host
    return $LASTEXITCODE
}

$failed = @()
if ($Database -in 'all','sqlite') {
    if ((Invoke-Suite 'SQLite' @()) -ne 0) { $failed += 'sqlite' }
}
if ($Database -in 'all','postgres') {
    try {
        & docker network create @labels $network | Out-Null
        & docker run -d --rm --name $postgres --network $network @labels `
            -e POSTGRES_PASSWORD=ctest -e POSTGRES_DB=ctest $PostgresImage | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'Cannot start test PostgreSQL' }
        $deadline = (Get-Date).AddSeconds(60)
        do {
            Start-Sleep 1
            & docker exec $postgres pg_isready -U postgres -d ctest *> $null
        } while ($LASTEXITCODE -ne 0 -and (Get-Date) -lt $deadline)
        if ($LASTEXITCODE -ne 0) { throw 'Test PostgreSQL did not become ready' }
        $url = "postgresql+psycopg://postgres:ctest@${postgres}:5432/ctest"
        if ((Invoke-Suite 'PostgreSQL' @('--network', $network, '-e', "NIUCAI_TEST_DATABASE_URL=$url")) -ne 0) { $failed += 'postgres' }
    } finally {
        & docker rm -f $postgres *> $null
        & docker network rm $network *> $null
    }
}
if ($failed) { Write-Output "FAILED: $($failed -join ', ')"; exit 1 }
Write-Output 'ALL PASSED'
