# 把 Windows Runner 打成单文件 exe，作为 Tauri sidecar 放到 apps/desktop/src-tauri/binaries/。
# 用法（仓库根目录）：.\services\node-runner\build-windows.ps1
# 之后 `npm run desktop:build:runner`（apps/desktop）会把它打进安装包。
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$out = Join-Path $here "..\..\apps\desktop\src-tauri\binaries"
$work = Join-Path $here "..\..\build-cache\node-runner"
New-Item -ItemType Directory -Force $out, $work | Out-Null

# Tauri externalBin 约定：文件名后缀为目标三元组。
$name = "niucai-runner-x86_64-pc-windows-msvc"
uv run --no-project --with pyinstaller --with httpx pyinstaller `
    --onefile --console --name $name `
    --hidden-import board_mcp --hidden-import hermes_bridge --hidden-import win_job `
    --add-data "$(Join-Path $here 'board-extension.ts');." `
    --distpath $out --workpath $work --specpath $work `
    --paths $here (Join-Path $here "runner.py")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
Write-Host "built $(Join-Path $out "$name.exe")"
