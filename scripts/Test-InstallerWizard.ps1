param([Parameter(Mandatory)][string]$Setup, [string]$ExpectedDirectory, [string]$Mode='/ALLUSERS', [string]$Language='english')
$ErrorActionPreference='Stop'
# Isolated test runner. Read the actual native edit control, including Delphi VCL classes.
Add-Type -TypeDefinition @"
using System;
using System.Text;
using System.Collections.Generic;
using System.Runtime.InteropServices;
public static class SetupWindow {
 public delegate bool Visit(IntPtr w, IntPtr state);
 [DllImport("user32")] static extern bool EnumWindows(Visit f, IntPtr state);
 [DllImport("user32")] static extern bool EnumChildWindows(IntPtr w, Visit f, IntPtr state);
 [DllImport("user32")] public static extern bool IsWindowVisible(IntPtr w);
 [DllImport("user32")] public static extern bool IsWindowEnabled(IntPtr w);
 [DllImport("user32",CharSet=CharSet.Unicode)] static extern int GetClassName(IntPtr w,StringBuilder value,int length);
 [DllImport("user32",CharSet=CharSet.Unicode)] static extern IntPtr SendMessageTimeout(IntPtr w,int msg,IntPtr wp,StringBuilder data,int flags,int timeout,out IntPtr result);
 [DllImport("user32",CharSet=CharSet.Unicode, EntryPoint="SendMessageTimeoutW")] static extern IntPtr SetText(IntPtr w,int msg,IntPtr wp,string data,int flags,int timeout,out IntPtr result);
 [DllImport("user32")] public static extern int GetWindowLong(IntPtr w,int field);
 [DllImport("user32")] public static extern bool PostMessage(IntPtr w,int m,IntPtr a,IntPtr b);
 public static string Class(IntPtr w) {var b=new StringBuilder(256);GetClassName(w,b,b.Capacity);return b.ToString();}
 public static string Text(IntPtr w) {var b=new StringBuilder(1024);IntPtr r;SendMessageTimeout(w,13,(IntPtr)b.Capacity,b,2,1000,out r);return b.ToString();}
 public static bool Change(IntPtr w,string s) {IntPtr r;return SetText(w,12,IntPtr.Zero,s,2,1000,out r)!=IntPtr.Zero && r!=IntPtr.Zero && Text(w)==s;}
 public static IntPtr[] Visible(IntPtr parent) {var list=new List<IntPtr>();Visit add=(w,x)=>{if(IsWindowVisible(w))list.Add(w);return true;};if(parent==IntPtr.Zero)EnumWindows(add,IntPtr.Zero);else EnumChildWindows(parent,add,IntPtr.Zero);return list.ToArray();}
}
"@
$proc=Start-Process -FilePath (Resolve-Path -LiteralPath $Setup).Path -ArgumentList @('/SP-',$Mode,"/LANG=$Language") -PassThru
try {
 $end=(Get-Date).AddSeconds(60);$last=''
 while ((Get-Date) -lt $end) {
  Start-Sleep -Milliseconds 550
  foreach ($w in [SetupWindow]::Visible([IntPtr]::Zero)) {
   if ([SetupWindow]::Class($w) -ne 'TWizardForm') {continue}
   $controls=[SetupWindow]::Visible($w)
   $last=($controls | ForEach-Object {"$([SetupWindow]::Class($_))=$([SetupWindow]::Text($_))"}) -join ' / '
   foreach ($c in $controls) {
    $path=[SetupWindow]::Text($c)
    if ($path -match '^[A-Za-z]:\\' -and [SetupWindow]::Class($c) -match 'Edit') {
     if ($ExpectedDirectory -and $path.TrimEnd('\') -ine $ExpectedDirectory.TrimEnd('\')) {throw "Displayed $path, expected $ExpectedDirectory"}
     if ($last -notmatch 'Browse|찾아보기') {throw "No Browse button: $last"}
     if (![SetupWindow]::IsWindowEnabled($c) -or ([SetupWindow]::GetWindowLong($c,-16) -band 0x800)) {throw 'Directory editor disabled or read-only'}
     if (![SetupWindow]::Change($c,'D:\Installer verification\Changed folder')) {throw 'Could not change directory'}
     Write-Output "Editable directory with Browse: $path";return
    }
   }
   foreach ($c in $controls) {
    $caption=([SetupWindow]::Text($c)).Replace('&','')
    if ([SetupWindow]::IsWindowEnabled($c) -and $caption -match '^(I accept|동의합니다|Next|다음)' -and [SetupWindow]::Class($c) -match 'Button') {
     [void][SetupWindow]::PostMessage($c,0xF5,[IntPtr]::Zero,[IntPtr]::Zero)
    }
   }
  }
  if ($proc.HasExited) {throw "Exited before directory page: $($proc.ExitCode)"}
 }
 throw "No directory editor. $last"
} finally {if (!$proc.HasExited) { & taskkill.exe /PID $proc.Id /T /F | Out-Null }}
