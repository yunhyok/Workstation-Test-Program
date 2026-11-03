#ifndef AppVersion
  #define AppVersion "1.3.0"
#endif
[Setup]
AppId={{AC051B97-5D45-4E44-97D6-577D57211AD0}
AppName=Workstation Test Program
AppVersion={#AppVersion}
AppPublisher=yunhyok
AppPublisherURL=https://github.com/yunhyok/Workstation-Test-Program
DefaultDirName={localappdata}\Programs\Workstation Test Program
DefaultGroupName=Workstation Test Program
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist\installer
OutputBaseFilename=Workstation-Test-Program-{#AppVersion}-Setup-x64
Compression=lzma2/fast
LZMANumBlockThreads=4
SolidCompression=yes
WizardStyle=modern
SetupIconFile=..\assets\workstation.ico
UninstallDisplayIcon={app}\WorkstationTestProgram.exe
CloseApplications=yes
RestartApplications=no
; CUDA libraries are shared by all experiments; no installer-time downloads.
[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked
[Files]
Source: "..\dist\WorkstationTestProgram\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
[Icons]
Name: "{group}\Workstation Test Program"; Filename: "{app}\WorkstationTestProgram.exe"; WorkingDir: "{app}"
Name: "{group}\Usage guide"; Filename: "{app}\_internal\README.md"
Name: "{autodesktop}\Workstation Test Program"; Filename: "{app}\WorkstationTestProgram.exe"; Tasks: desktopicon
[Run]
Filename: "{app}\WorkstationTestProgram.exe"; Description: "Launch Workstation Test Program"; Flags: nowait postinstall skipifsilent
