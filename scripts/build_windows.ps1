param([string]$Python = "python", [string]$ISCC = "iscc", [string]$Version = "1.0.1")
$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $projectRoot
try {
    & $Python -m PyInstaller --noconfirm --log-level WARN packaging/workstation.spec
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed" }
    & $ISCC /Q "/DAppVersion=$Version" packaging/setup.iss
    if ($LASTEXITCODE -ne 0) { throw "Installer build failed" }
    Get-ChildItem -LiteralPath dist/installer -Filter "*.exe" | Get-FileHash -Algorithm SHA256 | Format-Table
} finally {
    Pop-Location
}
