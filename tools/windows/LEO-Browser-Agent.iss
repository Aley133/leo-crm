#define MyAppName "LEO Browser Agent"
#define MyAppVersion "0.1.0"
#define MyAppPublisher "LEO CRM"
#define MyAppExeName "LEO-Browser-Agent.exe"

[Setup]
AppId={{8B7A3C41-13A7-4B63-8A6E-7B6E8DF61C01}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\LEO Browser Agent
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\..\dist
OutputBaseFilename=LEO-Browser-Agent-Setup
Compression=lzma
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\{#MyAppExeName}

[Files]
Source: "..\..\dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\LEO Browser Agent"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\LEO Browser Agent"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon
Name: "{userstartup}\LEO Browser Agent"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"

[Tasks]
Name: "desktopicon"; Description: "Создать ярлык на рабочем столе"; GroupDescription: "Дополнительные ярлыки:"; Flags: checkedonce

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Запустить LEO Browser Agent"; Flags: nowait postinstall skipifsilent
