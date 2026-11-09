#ifndef AppVersion
  #error AppVersion must be supplied by build_release.ps1
#endif
[Setup]
AppId={{7B2CBA82-2D7E-4F73-9A12-0D2B392F789A}
AppName=Outlook SoCLaaS Triage
AppVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\OutlookTriage
DefaultGroupName=Outlook SoCLaaS Triage
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir=..\dist
OutputBaseFilename=Outlook-SoCLaaS-Triage-{#AppVersion}-windows-x64-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\outlook-triage.exe
CloseApplications=yes
RestartApplications=no
SetupLogging=yes

[Files]
; Only the clean PyInstaller bundle and rendered guides enter the installer.
Source: "..\dist\OutlookTriage\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\build\guides\*.html"; DestDir: "{app}\guides"; Flags: ignoreversion

[Icons]
Name: "{group}\Outlook Triage - Setup"; Filename: "{app}\outlook-triage.exe"; Parameters: "setup --pause"; WorkingDir: "{app}"
Name: "{group}\Outlook Triage - First-run Check"; Filename: "{app}\outlook-triage.exe"; Parameters: "verify --pause"; WorkingDir: "{app}"
Name: "{group}\Outlook Triage - Troubleshooting"; Filename: "{app}\outlook-triage.exe"; Parameters: "doctor --pause"; WorkingDir: "{app}"
Name: "{group}\Outlook Triage - User Guide"; Filename: "{app}\guides\User-Guide.html"
Name: "{group}\Outlook Triage - Stop Background Operation"; Filename: "{app}\outlook-triage.exe"; Parameters: "background stop --pause"; WorkingDir: "{app}"
Name: "{group}\Outlook Triage - Settings Folder"; Filename: "{win}\explorer.exe"; Parameters: """{userappdata}\OutlookTriage"""
Name: "{group}\Outlook Triage - Reports and Logs"; Filename: "{win}\explorer.exe"; Parameters: """{localappdata}\OutlookTriage"""

[Run]
Filename: "{app}\outlook-triage.exe"; Parameters: "setup --pause"; Description: "Open Outlook Triage Setup"; Flags: postinstall nowait skipifsilent

[Code]
function StopOwnedTasks(): Boolean;
var ResultCode: Integer;
begin
  Result := True;
  if FileExists(ExpandConstant('{app}\outlook-triage.exe')) then
  begin
    Result := Exec(ExpandConstant('{app}\outlook-triage.exe'), 'background stop',
      ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, ResultCode);
    if Result then Result := ResultCode = 0;
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  if not StopOwnedTasks() then
    Result := 'Outlook Triage could not stop its background tasks. Open Troubleshooting from Start, resolve background task access, then retry installation. Your settings and data have been kept.';
end;

function InitializeUninstall(): Boolean;
begin
  Result := StopOwnedTasks();
  if not Result then
    MsgBox('Background tasks could not be stopped. Open Troubleshooting, resolve task access, and retry uninstall. Your settings and data have been kept.', mbError, MB_OK);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
    MsgBox('Outlook Triage was removed. Your settings, credentials, reports, logs and SQLite data have been kept in your OutlookTriage user folders.', mbInformation, MB_OK);
end;
