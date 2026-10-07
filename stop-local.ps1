$ErrorActionPreference = 'Stop'
Push-Location "$PSScriptRoot/deploy/compose"
try {
    docker compose stop api worker postgres
    if ($LASTEXITCODE -ne 0) { throw 'Stopping backend failed' }
} finally {
    Pop-Location
}
