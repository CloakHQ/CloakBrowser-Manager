; Inno Setup script for CloakBrowser Manager (Windows).
; Compiled by packaging/build_windows.ps1, which passes the version via /DAppVersion
; and points at the PyInstaller onedir output.

#ifndef AppVersion
  #define AppVersion "dev"
#endif
#ifndef SourceDir
  #define SourceDir "..\dist_native\CloakBrowser Manager"
#endif

#define AppName "CloakBrowser Manager"
#define AppExe "CloakBrowser Manager.exe"

[Setup]
AppId={{7C4D9E2A-3B1F-4C8E-9A6D-CB0AKMANAGER01}}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=CloakHQ
DefaultDirName={localappdata}\Programs\CloakBrowser Manager
DefaultGroupName=CloakBrowser Manager
DisableProgramGroupPage=yes
; Per-user install — no admin prompt, no code-signing cert needed for the test.
PrivilegesRequired=lowest
OutputDir=..\dist_native
OutputBaseFilename=CloakBrowser-Manager-{#AppVersion}-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
SetupIconFile=..\packaging\icon.ico
UninstallDisplayIcon={app}\{#AppExe}

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\CloakBrowser Manager"; Filename: "{app}\{#AppExe}"
Name: "{userdesktop}\CloakBrowser Manager"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional icons:"

[Run]
Filename: "{app}\{#AppExe}"; Description: "Launch CloakBrowser Manager"; Flags: nowait postinstall skipifsilent
