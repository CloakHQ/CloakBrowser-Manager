; Inno Setup script for CloakBrowser Manager (Custom) — unofficial fork.
; Compiled by packaging/build_windows.ps1, which passes the version via /DAppVersion
; and points at the PyInstaller onedir output.
;
; NOTE: AppId is deliberately different from upstream's, so installing this fork
; side by side with the official CloakHQ build does not overwrite or uninstall it.

#ifndef AppVersion
  #define AppVersion "dev"
#endif
#ifndef SourceDir
  #define SourceDir "..\dist_native\CloakBrowser Manager (Custom)"
#endif

#define AppName "CloakBrowser Manager (Custom)"
#define AppExe "CloakBrowser Manager (Custom).exe"

[Setup]
AppId={{C5A742BE-CA41-4BC2-B7D4-B8CF06C5D21F}
AppName={#AppName}
AppVersion={#AppVersion}
; Not CloakHQ: this installer ships an unofficial fork, and the label says so.
AppPublisher=Unofficial fork of CloakHQ's CloakBrowser Manager
DefaultDirName={localappdata}\Programs\CloakBrowser Manager (Custom)
DefaultGroupName=CloakBrowser Manager (Custom)
DisableProgramGroupPage=yes
; Per-user install — no admin prompt, no code-signing cert needed for the test.
PrivilegesRequired=lowest
OutputDir=..\dist_native
OutputBaseFilename=CloakBrowser-Manager-Custom-{#AppVersion}-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
SetupIconFile=..\packaging\icon.ico
UninstallDisplayIcon={app}\{#AppExe}

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\CloakBrowser Manager (Custom)"; Filename: "{app}\{#AppExe}"
Name: "{userdesktop}\CloakBrowser Manager (Custom)"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional icons:"

[Run]
Filename: "{app}\{#AppExe}"; Description: "Launch CloakBrowser Manager (Custom)"; Flags: nowait postinstall skipifsilent
