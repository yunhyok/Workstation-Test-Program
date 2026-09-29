param([string]$Python = "python", [string]$ISCC = "iscc", [string]$Version = "1.2.0",
      [string]$EngineRoot = "")
$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $projectRoot
try {
    if ($EngineRoot) {
        & $Python scripts/bundle_runtime.py --engine-root $EngineRoot
        if ($LASTEXITCODE -ne 0) { throw "Engine runtime build failed" }
    }
    $runtime = Join-Path $projectRoot "build/engine-runtime"
    if (-not (Test-Path -LiteralPath (Join-Path $runtime "runtime-manifest.json"))) {
        throw "Standalone runtime is required. Pass -EngineRoot with the owner's engine checkout."
    }
    $runtimePython = Join-Path $runtime "python.exe"
    if (-not (Test-Path -LiteralPath $runtimePython)) { throw "Standalone runtime python.exe is missing: $runtimePython" }
    & $runtimePython scripts/bundle_runtime.py --verify-only $runtime
    if ($LASTEXITCODE -ne 0) { throw "Standalone runtime verification failed before packaging" }
    & $Python -m PyInstaller --noconfirm --log-level WARN packaging/workstation.spec
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed" }
    $packagedRuntime = Join-Path $projectRoot "dist/WorkstationTestProgram/engine-runtime"
    Copy-Item -LiteralPath $runtime -Destination $packagedRuntime -Recurse -Force
    & (Join-Path $packagedRuntime "python.exe") scripts/bundle_runtime.py --verify-only $packagedRuntime
    if ($LASTEXITCODE -ne 0) { throw "Packaged runtime verification failed" }
    & $ISCC /Q "/DAppVersion=$Version" packaging/setup.iss
    if ($LASTEXITCODE -ne 0) { throw "Installer build failed" }
    Get-ChildItem -LiteralPath dist/installer -Filter "*.exe" | Get-FileHash -Algorithm SHA256 | Format-Table
} finally {
    Pop-Location
}
