param([string]$Installer = "dist/installer/Workstation-Test-Program-1.0.0-Setup-x64.exe")
$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path -LiteralPath (Split-Path -Parent $PSScriptRoot)).Path
$installerPath = (Resolve-Path -LiteralPath (Join-Path $projectRoot $Installer)).Path
$smokeRoot = Join-Path $projectRoot ("output/install-smoke-" + (Get-Date -Format "yyyyMMdd-HHmmss"))
$installationRoot = [IO.Path]::GetFullPath((Join-Path $smokeRoot "app"))
if (-not $installationRoot.StartsWith($projectRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Smoke installation target must stay inside the project workspace."
}
$appRegistry = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\{AC051B97-5D45-4E44-97D6-577D57211AD0}_is1"
if (Test-Path -LiteralPath $appRegistry) { throw "The application is already installed; refusing to replace that installation in a smoke test." }
New-Item -ItemType Directory -Path $smokeRoot -Force | Out-Null
$studyRoot = Join-Path $smokeRoot "study"
New-Item -ItemType Directory -Path $studyRoot | Out-Null
Set-Content -LiteralPath (Join-Path $studyRoot "preserve.txt") -Value "Study evidence must survive uninstall."
$installArgs = @("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-", ('/DIR="' + $installationRoot + '"'), ('/LOG="' + (Join-Path $smokeRoot "install.log") + '"'))
$process = Start-Process -FilePath $installerPath -ArgumentList $installArgs -WindowStyle Hidden -PassThru -Wait
if ($process.ExitCode -ne 0) { throw "Silent installer failed: $($process.ExitCode)" }
$cliPath = Join-Path $installationRoot "WorkstationTest.exe"
$uninstaller = Join-Path $installationRoot "unins000.exe"
try {
    & $cliPath --version
    if ($LASTEXITCODE -ne 0) { throw "Installed CLI did not launch" }
    & $cliPath gui --smoke --root $studyRoot
    if ($LASTEXITCODE -ne 0) { throw "Installed GUI smoke failed" }
    & $cliPath validate --self-check
    if ($LASTEXITCODE -ne 0) { throw "Installed validation self-check failed" }
    & $cliPath agent --self-check
    if ($LASTEXITCODE -ne 0) { throw "Installed agent self-check failed" }
} finally {
    if (Test-Path -LiteralPath $uninstaller) {
        $resolvedUninstaller = (Resolve-Path -LiteralPath $uninstaller).Path
        if (-not $resolvedUninstaller.StartsWith($installationRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing to run an uninstaller outside the smoke installation."
        }
        $removeProcess = Start-Process -FilePath $resolvedUninstaller -ArgumentList "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART" -WindowStyle Hidden -PassThru -Wait
        if ($removeProcess.ExitCode -ne 0) { throw "Uninstall failed: $($removeProcess.ExitCode)" }
    }
}
if (-not (Test-Path -LiteralPath (Join-Path $studyRoot "preserve.txt"))) { throw "Uninstall deleted study evidence" }
if (Test-Path -LiteralPath $cliPath) { throw "Uninstall did not remove the installed CLI" }
Write-Output "PASS: install, GUI/CLI/self-checks, uninstall, preserved study data. Evidence: $smokeRoot"
