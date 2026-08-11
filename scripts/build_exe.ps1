$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$specPath = Join-Path $repoRoot "packaging\wechat-oracle.spec"
$outputDir = Join-Path $repoRoot "dist\WeChatOracle"
$exePath = Join-Path $outputDir "WeChatOracle.exe"

Push-Location $repoRoot
try {
    uv sync --group dev
    if ($LASTEXITCODE -ne 0) { throw "uv sync failed" }

    uv run pyinstaller --noconfirm --clean $specPath
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed" }
    if (-not (Test-Path -LiteralPath $exePath -PathType Leaf)) {
        throw "Expected executable was not created: $exePath"
    }

    New-Item -ItemType Directory -Force -Path (Join-Path $outputDir "data\personas") | Out-Null
    Copy-Item -LiteralPath (Join-Path $repoRoot "README.md") -Destination $outputDir -Force
    Copy-Item -LiteralPath (Join-Path $repoRoot "README.zh-CN.md") -Destination $outputDir -Force
    Copy-Item -LiteralPath (Join-Path $repoRoot "THIRD_PARTY_NOTICES.md") -Destination $outputDir -Force

    $sitePackages = Join-Path $repoRoot ".venv\Lib\site-packages"
    $wxLicense = Get-ChildItem -LiteralPath $sitePackages -Directory -Filter "wx4py-*.dist-info" |
        ForEach-Object { Join-Path $_.FullName "licenses\LICENSE" } |
        Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } |
        Select-Object -First 1
    if (-not $wxLicense) {
        throw "wx4py license file was not found under $sitePackages"
    }
    $licenseDir = Join-Path $outputDir "licenses"
    New-Item -ItemType Directory -Force -Path $licenseDir | Out-Null
    Copy-Item -LiteralPath $wxLicense -Destination (Join-Path $licenseDir "wx4py-LICENSE.txt") -Force

    Write-Host "Portable build ready: $exePath"
    Write-Host "Distribute the entire dist\WeChatOracle directory, not only the EXE."
}
finally {
    Pop-Location
}
