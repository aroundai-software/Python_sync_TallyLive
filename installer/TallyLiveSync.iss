; TallyLive Sync — Inno Setup installer
; Compile with: ISCC.exe installer\TallyLiveSync.iss
; Expects dist_deployment\ to be populated by build_installer.py first.

#define MyAppName "TallyLive Sync"
#define MyAppVersion "1.2.3"
#define MyAppPublisher "TallyLive"
#define MyAppExeName "Tallylive_python_sync_background.exe"

[Setup]
AppId={{A7C3E2B1-9F14-4D6A-8C21-5E8B0D4F2A11}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\TallyLive Sync
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=..\installer_output
OutputBaseFilename=Setup_TallyLive_Sync_{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
PrivilegesRequired=admin
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern
SetupLogging=yes
; Single uninstallable product only — Settings is NOT a separate app in Apps & features.
UninstallDisplayName={#MyAppName}
CreateUninstallRegKey=yes
CloseApplications=force
SetupIconFile=tallylive_sync.ico
UninstallDisplayIcon={app}\Tallylive_python_sync_background\Tallylive_python_sync_background.exe
; Show IT allowlist inside the Setup wizard (no separate file required for clients)
InfoBeforeFile=IT_ALLOWLIST.txt

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Messages]
InfoBeforeLabel=IT allowlist — firewall & antivirus
InfoBeforeClickLabel=Please share this with IT if needed, then click Next to continue installing.

[Files]
; Service wrapper + helpers
Source: "..\dist_deployment\myserviceinstaller.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\dist_deployment\myserviceinstaller.xml"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\dist_deployment\install_service.bat"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\dist_deployment\uninstall_service.bat"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\dist_deployment\kill_ghosts.bat"; DestDir: "{app}"; Flags: ignoreversion
; Settings + updater — always installed with the product (not optional, not separately uninstallable)
Source: "..\dist_deployment\Tallylive_sync_settings.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\dist_deployment\Tallylive_updater.exe"; DestDir: "{app}"; Flags: ignoreversion
; IT allowlist (firewall / antivirus) — installed under Program Files for IT reference
Source: "IT_ALLOWLIST.txt"; DestDir: "{app}"; Flags: ignoreversion
; Sync app folder (EXE + _internal)
Source: "..\dist_deployment\Tallylive_python_sync_background\*"; DestDir: "{app}\Tallylive_python_sync_background"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
; Always create Settings in Start Menu (part of TallyLive Sync — no separate uninstall entry)
Name: "{group}\TallyLive Sync Settings"; Filename: "{app}\Tallylive_sync_settings.exe"; Comment: "Change Tally host, port, interval, and full reconcile"; AppUserModelID: "TallyLive.Sync.Settings"
; Desktop icon is optional (checkbox during setup)
Name: "{commondesktop}\TallyLive Sync Settings"; Filename: "{app}\Tallylive_sync_settings.exe"; Comment: "Change Tally host, port, interval, and full reconcile"; AppUserModelID: "TallyLive.Sync.Settings"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop icon for Settings"; GroupDescription: "Additional icons:"; Flags: unchecked

[Run]
; After files are copied, .env is written in CurStepChanged(ssPostInstall)
Filename: "{app}\myserviceinstaller.exe"; Parameters: "install"; StatusMsg: "Installing Windows service..."; Flags: runhidden waituntilterminated
Filename: "{app}\myserviceinstaller.exe"; Parameters: "start"; StatusMsg: "Starting TallyLive Sync service..."; Flags: runhidden waituntilterminated

[UninstallRun]
Filename: "{app}\myserviceinstaller.exe"; Parameters: "stop"; RunOnceId: "StopSvc"; Flags: runhidden waituntilterminated
Filename: "{app}\myserviceinstaller.exe"; Parameters: "uninstall"; RunOnceId: "UninstallSvc"; Flags: runhidden waituntilterminated

[Code]
var
  ConfigPage: TWizardPage;
  HostEdit: TNewEdit;
  PortEdit: TNewEdit;
  IntervalEdit: TNewEdit;
  FullCheck: TNewCheckBox;

procedure InitializeWizard;
var
  LabelHost, LabelPort, LabelInterval: TNewStaticText;
begin
  ConfigPage := CreateCustomPage(
    wpSelectDir,
    'Sync configuration',
    'Enter Tally connection and sync options for this PC.'
  );

  LabelHost := TNewStaticText.Create(ConfigPage);
  LabelHost.Parent := ConfigPage.Surface;
  LabelHost.Caption := 'Tally host (usually localhost):';
  LabelHost.Top := ScaleY(8);
  LabelHost.Left := ScaleX(0);

  HostEdit := TNewEdit.Create(ConfigPage);
  HostEdit.Parent := ConfigPage.Surface;
  HostEdit.Top := LabelHost.Top + LabelHost.Height + ScaleY(4);
  HostEdit.Left := ScaleX(0);
  HostEdit.Width := ConfigPage.SurfaceWidth;
  HostEdit.Text := 'localhost';

  LabelPort := TNewStaticText.Create(ConfigPage);
  LabelPort.Parent := ConfigPage.Surface;
  LabelPort.Caption := 'Port:';
  LabelPort.Top := HostEdit.Top + HostEdit.Height + ScaleY(12);
  LabelPort.Left := ScaleX(0);

  PortEdit := TNewEdit.Create(ConfigPage);
  PortEdit.Parent := ConfigPage.Surface;
  PortEdit.Top := LabelPort.Top + LabelPort.Height + ScaleY(4);
  PortEdit.Left := ScaleX(0);
  PortEdit.Width := ScaleX(120);
  PortEdit.Text := '9000';

  LabelInterval := TNewStaticText.Create(ConfigPage);
  LabelInterval.Parent := ConfigPage.Surface;
  LabelInterval.Caption := 'Sync interval (minutes):';
  LabelInterval.Top := PortEdit.Top + PortEdit.Height + ScaleY(12);
  LabelInterval.Left := ScaleX(0);

  IntervalEdit := TNewEdit.Create(ConfigPage);
  IntervalEdit.Parent := ConfigPage.Surface;
  IntervalEdit.Top := LabelInterval.Top + LabelInterval.Height + ScaleY(4);
  IntervalEdit.Left := ScaleX(0);
  IntervalEdit.Width := ScaleX(120);
  IntervalEdit.Text := '60';

  FullCheck := TNewCheckBox.Create(ConfigPage);
  FullCheck.Parent := ConfigPage.Surface;
  FullCheck.Caption := 'Full reconcile on first sync (one-time; heavier run)';
  FullCheck.Top := IntervalEdit.Top + IntervalEdit.Height + ScaleY(16);
  FullCheck.Left := ScaleX(0);
  FullCheck.Width := ConfigPage.SurfaceWidth;
  FullCheck.Checked := False;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  PortVal, IntervalVal: Integer;
begin
  Result := True;
  if CurPageID = ConfigPage.ID then
  begin
    if Trim(HostEdit.Text) = '' then
    begin
      MsgBox('Please enter a Tally host.', mbError, MB_OK);
      Result := False;
      Exit;
    end;
    PortVal := StrToIntDef(Trim(PortEdit.Text), -1);
    if (PortVal < 1) or (PortVal > 65535) then
    begin
      MsgBox('Port must be a number between 1 and 65535.', mbError, MB_OK);
      Result := False;
      Exit;
    end;
    IntervalVal := StrToIntDef(Trim(IntervalEdit.Text), -1);
    if IntervalVal < 1 then
    begin
      MsgBox('Sync interval must be at least 1 minute.', mbError, MB_OK);
      Result := False;
      Exit;
    end;
  end;
end;

procedure WriteEnvFile();
var
  EnvPath: String;
  Host, Port, IntervalSec, FullFlag: String;
  Minutes: Integer;
begin
  EnvPath := ExpandConstant('{app}\Tallylive_python_sync_background\.env');
  Host := Trim(HostEdit.Text);
  Port := Trim(PortEdit.Text);
  Minutes := StrToIntDef(Trim(IntervalEdit.Text), 60);
  IntervalSec := IntToStr(Minutes * 60);
  if FullCheck.Checked then
    FullFlag := '1'
  else
    FullFlag := '0';

  ForceDirectories(ExtractFilePath(EnvPath));
  SaveStringToFile(
    EnvPath,
    'TALLY_HOST=' + Host + #13#10 +
    'TALLY_PORT=' + Port + #13#10 +
    'SYNC_INTERVAL=' + IntervalSec + #13#10 +
    'FULL_RECONCILE=' + FullFlag + #13#10,
    False
  );
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    WriteEnvFile();
  end;
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  if CurPageID = wpFinished then
  begin
    WizardForm.FinishedLabel.Caption :=
      'TallyLive Sync was installed successfully.' + #13#10 + #13#10 +
      'The sync service is running in the background.' + #13#10 +
      'Open "TallyLive Sync Settings" from the Start Menu to change host, port, interval, or full reconcile.' + #13#10 + #13#10 +
      'IT allowlist file saved at:' + #13#10 +
      '  C:\Program Files\TallyLive Sync\IT_ALLOWLIST.txt' + #13#10 + #13#10 +
      'To remove the product, uninstall "TallyLive Sync" from Windows Apps ' +
      '(Settings is included and cannot be removed by itself).' + #13#10 + #13#10 +
      'Version: {#MyAppVersion}';
  end;
end;
