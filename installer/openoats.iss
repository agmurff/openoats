; OpenOats Meeting Assistant — Inno Setup script
; Builds a one-click .exe installer that drops the PyInstaller bundle into
; Program Files, registers Start menu + Add/Remove Programs, and creates
; an optional desktop shortcut.

#define MyAppName        "OpenOats Meeting Assistant"
#define MyAppShortName   "OpenOats"
#define MyAppVersion     "0.2.1"
#define MyAppPublisher   "Adam Murphy"
#define MyAppExeName     "OpenOats.exe"
#define MyAppId          "{{2003B55F-B4EF-451B-8F8A-BE591E7F0482}"

[Setup]
AppId={#MyAppId}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppShortName}
DefaultGroupName={#MyAppShortName}
DisableProgramGroupPage=yes
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\installer-out
OutputBaseFilename=OpenOats-Setup-{#MyAppVersion}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayName={#MyAppName}
UninstallDisplayIcon={app}\{#MyAppExeName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; \
  GroupDescription: "Additional shortcuts:"

[Files]
; Pull in the entire PyInstaller one-folder bundle.
Source: "..\dist\OpenOats\*"; DestDir: "{app}"; \
  Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}";        Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppShortName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppShortName}";     Filename: "{app}\{#MyAppExeName}"; \
  Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; \
  Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent
