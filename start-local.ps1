$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$env:PYTHONUTF8 = '1'
@'
from pathlib import Path
from dotenv import dotenv_values, set_key
import yaml

root = Path.cwd()
source = dotenv_values(root / '.env')
target = root / 'deploy/compose/.env'
if not target.exists():
    raise SystemExit('Missing deploy/compose/.env; complete initial setup first.')
for source_name, target_name in (
    ('OPENAI_API_KEY', 'NIUCAI_OPENAI_API_KEY'),
    ('OPENAI_API_BASE_URL', 'NIUCAI_OPENAI_API_BASE_URL'),
):
    if source.get(source_name) is not None:
        set_key(target, target_name, source[source_name], quote_mode='always')
if source.get('OPENAI_API_MODEL'):
    model_path = root / 'services/kernel/models.yaml'
    models = yaml.safe_load(model_path.read_text(encoding='utf-8'))
    for profile in models['roles'].values():
        profile['model'] = source['OPENAI_API_MODEL']
        profile.pop('reasoning', None)
    model_path.write_text(yaml.safe_dump(models, sort_keys=False), encoding='utf-8', newline='\n')
print('Local model configuration synchronized; credentials are not displayed.')
'@ | & "$PSScriptRoot/services/kernel/.venv/Scripts/python.exe" -
if ($LASTEXITCODE -ne 0) { throw 'Configuration synchronization failed' }
Push-Location "$PSScriptRoot/deploy/compose"
try {
    docker compose config --quiet
    if ($LASTEXITCODE -ne 0) { throw 'Compose configuration invalid' }
    docker compose up -d --build postgres api worker
    if ($LASTEXITCODE -ne 0) { throw 'Backend startup failed' }
    docker compose ps -a
} finally {
    Pop-Location
}
if (-not (Get-Process niucai-desktop -ErrorAction SilentlyContinue)) {
    $clientPath = @(
        "$PSScriptRoot/apps/desktop/src-tauri/target/release/niucai-desktop.exe",
        "$PSScriptRoot/apps/desktop/src-tauri/target/debug/niucai-desktop.exe"
    ) | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
    if ($clientPath) {
        Start-Process -FilePath $clientPath -WindowStyle Hidden
    } else {
        Write-Warning 'Backend started. Build the desktop client from apps/desktop before opening it.'
    }
}
