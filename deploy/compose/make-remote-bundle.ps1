# 生成交给 Hermes / 远程服务器的源码包。
# 只打包 Git 已提交的内容（git archive），因此 .env、secrets、build-cache 等机器本地文件不会进入；
# 有未提交修改时给出警告并列出，修改不会进入包内。输出到被 gitignore 的 niucai-delivery/。
param(
    [string]$Revision = 'HEAD',
    [string]$OutputDirectory = (Join-Path $PSScriptRoot '../../niucai-delivery'),
    [switch]$AllowDirty
)
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
Push-Location $root
try {
    $commit = (git rev-parse --verify "$Revision^{commit}").Trim()
    $short = $commit.Substring(0, 12)
    $dirty = git status --porcelain --untracked-files=no
    if ($dirty -and -not $AllowDirty) {
        Write-Warning '存在未提交修改，它们不会进入源码包。先提交，或加 -AllowDirty 明确接受：'
        $dirty | ForEach-Object { Write-Warning "  $_" }
        throw '拒绝在工作区不干净时生成交付包。'
    }
    New-Item -ItemType Directory -Force $OutputDirectory | Out-Null
    $out = (Resolve-Path $OutputDirectory).Path
    $zip = Join-Path $out "niucai-remote-$short.zip"
    git archive --format=zip --prefix=niucai/ -o $zip $commit
    $hash = (Get-FileHash -Algorithm SHA256 $zip).Hash.ToLower()
    "$hash  $(Split-Path $zip -Leaf)" | Set-Content -Encoding ascii "$zip.sha256"
    # 安全自检：包内不得出现本机密钥/环境文件。
    $names = git archive --format=tar $commit | tar -tf -
    $leaks = $names | Where-Object { $_ -match '(^|/)(\.env|secrets/|local-client/|local-logs/|.*\.db)$' -and $_ -notmatch '\.env\.example$' }
    if ($leaks) { Remove-Item $zip, "$zip.sha256"; throw "源码包含疑似本机文件：$($leaks -join ', ')" }
    Write-Host "已生成 $zip"
    Write-Host "SHA256 $hash"
    Write-Host "提交 $commit"
    Write-Host '传输后在服务器：sha256sum -c ' -NoNewline; Write-Host (Split-Path "$zip.sha256" -Leaf)
} finally { Pop-Location }
