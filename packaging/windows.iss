; release.py passes the version read from pyproject.toml and the application.
#ifndef AppVersion
  #error Pass /DAppVersion=<version>, or use packaging/release.py --platform windows.
#endif
[Setup]
AppId={{FD8D6BDC-77E6-45DB-BFE9-6FC644DE7B92}
AppName=GrihaLekha
AppVersion={#AppVersion}
AppPublisher=GrihaLekha contributors
LicenseFile=..\dist\GrihaLekha\LICENSE
DefaultDirName={localappdata}\Programs\GrihaLekha
DefaultGroupName=GrihaLekha
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist\release
OutputBaseFilename=GrihaLekha-{#AppVersion}-windows-x64-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
SetupIconFile=..\icons\grihalekha.ico
UninstallDisplayIcon={app}\GrihaLekha.exe
[Files]
Source: "..\dist\GrihaLekha\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
[Icons]
Name: "{group}\GrihaLekha"; Filename: "{app}\GrihaLekha.exe"; AppUserModelID: "GrihaLekha.Desktop"
Name: "{autodesktop}\GrihaLekha"; Filename: "{app}\GrihaLekha.exe"; Tasks: desktopicon; AppUserModelID: "GrihaLekha.Desktop"
[Tasks]
Name: desktopicon; Description: "Create a desktop shortcut"; Flags: unchecked
[Run]
Filename: "{app}\GrihaLekha.exe"; Description: "Open GrihaLekha"; Flags: nowait postinstall skipifsilent

