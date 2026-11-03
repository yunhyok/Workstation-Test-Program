# Run on an isolated Windows test runner. Stops at the destination page before copying files.
param([Parameter(Mandatory)][string]$Setup, [string]$ExpectedDirectory, [string]$Mode='/ALLUSERS', [string]$Language='english')
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient,UIAutomationTypes
$exe = (Resolve-Path -LiteralPath $Setup).Path
$proc = Start-Process -FilePath $exe -ArgumentList @('/SP-', $Mode, "/LANG=$Language") -PassThru
$root = [Windows.Automation.AutomationElement]::RootElement
$all = [Windows.Automation.Condition]::TrueCondition
$children = [Windows.Automation.TreeScope]::Children
$desc = [Windows.Automation.TreeScope]::Descendants
try {
  $end = (Get-Date).AddSeconds(60)
  $last = ''
  while ((Get-Date) -lt $end) {
    Start-Sleep -Milliseconds 500
    foreach ($window in $root.FindAll($children,$all)) {
      if ($window.Current.ClassName -ne 'TWizardForm') {continue}
      $controls=@($window.FindAll($desc,$all) | Where-Object { !$_.Current.IsOffscreen })
      $last = ($controls | ForEach-Object { $_.Current.Name }) -join ' / '
      foreach ($el in $controls) {
        if ($el.Current.ControlType -ne [Windows.Automation.ControlType]::Edit) {continue}
        $value = $el.GetCurrentPattern([Windows.Automation.ValuePattern]::Pattern)
        $path = $value.Current.Value
        if ($path -notmatch '^[A-Za-z]:\\') {continue}
        if ($ExpectedDirectory -and $path.TrimEnd('\') -ine $ExpectedDirectory.TrimEnd('\')) {throw "Displayed path '$path', expected '$ExpectedDirectory'"}
        if ($value.Current.IsReadOnly) {throw 'Installation directory is read-only'}
        $value.SetValue('D:\Installer verification\Selected folder')
        if ($value.Current.Value -ne 'D:\Installer verification\Selected folder') {throw 'Destination editor did not retain the selected path'}
        $browse = @($controls | Where-Object { $_.Current.ControlType -eq [Windows.Automation.ControlType]::Button -and $_.Current.Name.Replace('&','') -match 'Browse|찾아보기' })
        if ($browse.Count -eq 0) { throw 'Destination Browse button is missing.' }
        Write-Output "Editable destination page and Browse: $path"
        return
      }
      # The license agreement and welcome pages may precede the destination.
      foreach ($el in $controls) {
        if ($el.Current.ControlType -eq [Windows.Automation.ControlType]::RadioButton -and $el.Current.Name -match '^I accept|^동의합니다') {
          $el.GetCurrentPattern([Windows.Automation.SelectionItemPattern]::Pattern).Select()
        }
      }
      $next=$controls | Where-Object {$_.Current.ControlType -eq [Windows.Automation.ControlType]::Button -and $_.Current.Name.Replace('&','') -match '^(Next|다음)' -and $_.Current.IsEnabled} | Select-Object -First 1
      if ($next) {$next.GetCurrentPattern([Windows.Automation.InvokePattern]::Pattern).Invoke()}
    }
    if ($proc.HasExited) {throw "Installer exited before Select Destination ($($proc.ExitCode))"}
  }
  throw "No editable destination page. Visible controls: $last"
} finally {
  if (!$proc.HasExited) { & taskkill.exe /PID $proc.Id /T /F | Out-Null }
}
