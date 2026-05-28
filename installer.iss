#define MyAppName      "DB Compare & Sync Tool"
#define MyAppVersion   "1.0"
#define MyAppPublisher "IM HosXP Plus"
#define MyAppURL       "https://github.com/tannamnaja-ui/tool-DB-Compare---Sync-Tool"
#define MyAppExeName   "tool-DB-Compare-Sync.exe"
#define MyAppDir       "DB Compare Sync Tool"

[Setup]
AppId={{A1B2C3D4-E5F6-7890-ABCD-EF1234567890}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
DefaultDirName={autopf}\{#MyAppDir}
DefaultGroupName={#MyAppName}
AllowNoIcons=yes
OutputDir=setup
OutputBaseFilename=tool-DB-Compare-Sync-Setup
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\{#MyAppExeName}
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon";    Description: "สร้าง Desktop shortcut";       GroupDescription: "Shortcuts:"; Flags: unchecked
Name: "startupicon";   Description: "เริ่มโปรแกรมอัตโนมัติตอน login"; GroupDescription: "Auto-start:"; Flags: unchecked

[Files]
Source: "dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}";         Filename: "{app}\{#MyAppExeName}"; Comment: "DB Compare & Sync Tool"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{commondesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon; Comment: "DB Compare & Sync Tool"
Name: "{userstartup}\{#MyAppName}";   Filename: "{app}\{#MyAppExeName}"; Tasks: startupicon; Comment: "DB Compare & Sync Tool"

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "เปิดโปรแกรมหลังติดตั้ง"; Flags: nowait postinstall skipifsilent

[Code]
function InitializeSetup(): Boolean;
var
  Version: String;
  InstallPath: String;
begin
  // ตรวจสอบว่ามีการติดตั้งอยู่แล้วหรือไม่
  if RegQueryStringValue(HKEY_CURRENT_USER, 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{A1B2C3D4-E5F6-7890-ABCD-EF1234567890}_is1', 'DisplayVersion', Version) then
  begin
    if MsgBox('พบการติดตั้ง ' + '{#MyAppName}' + ' เวอร์ชัน ' + Version + ' อยู่แล้ว' + #13#10 + 'ต้องการติดตั้งใหม่ทับหรือไม่?', mbConfirmation, MB_YESNO) = IDNO then
    begin
      Result := False;
      Exit;
    end;
  end;
  Result := True;
end;
