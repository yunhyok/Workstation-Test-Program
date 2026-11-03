param([Parameter(Mandatory=$true)][string]$Installer)
$ErrorActionPreference = 'Stop'
$Installer = (Resolve-Path -LiteralPath $Installer).Path
$root = Join-Path $env:RUNNER_TEMP ('installer-path-' + [Guid]::NewGuid().ToString('N'))
$target = Join-Path $root 'custom folder 한글'
New-Item -ItemType Directory -Path $target -Force | Out-Null
$marker = Join-Path $target 'preserved-user-setting.txt'
Set-Content -LiteralPath $marker -Value 'preserve-me' -NoNewline
function Invoke-Installer([string]$Path, [string]$Arguments) {
    $process = Start-Process -FilePath $Path -ArgumentList $Arguments -Wait -PassThru -WindowStyle Hidden
    if ($process.ExitCode -ne 0) { throw "Installer failed: $Path ($($process.ExitCode))" }
}
try {
    & (Join-Path $PSScriptRoot 'Test-InstallerWizard.ps1') -Setup $Installer -Mode '/ALLUSERS'
    Invoke-Installer $Installer "/CURRENTUSER /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /DIR=`"$target`" /LOG=`"$root\fresh.log`""
    $uninstaller = Join-Path $target 'unins000.exe'
    if (!(Test-Path -LiteralPath $uninstaller)) { throw 'Custom destination was not used.' }
    Invoke-Installer $Installer "/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /LOG=`"$root\reinstall.log`""
    if (!(Test-Path -LiteralPath $uninstaller)) { throw 'Reinstall did not preserve the custom destination.' }
    & (Join-Path $PSScriptRoot 'Test-InstallerWizard.ps1') -Setup $Installer -Mode '/CURRENTUSER' -ExpectedDirectory $target
    $log = Get-Content -LiteralPath (Join-Path $root 'reinstall.log') -Raw
    if (!$log.Contains($target)) { throw 'Reinstall log does not record the custom destination.' }
    if ((Get-Content -LiteralPath $marker -Raw) -cne 'preserve-me') { throw 'Reinstall changed user data.' }
    Invoke-Installer $uninstaller '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART'
    if ((Get-Content -LiteralPath $marker -Raw) -cne 'preserve-me') { throw 'Uninstall changed user data.' }
    Write-Host "PASS custom destination, previous path/mode, reinstall and data preservation: $Installer"
} finally {
    $uninstaller = Join-Path $target 'unins000.exe'
    if (Test-Path -LiteralPath $uninstaller) { Invoke-Installer $uninstaller '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART' }
}
