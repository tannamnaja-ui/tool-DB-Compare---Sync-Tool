#define MyAppName      "DB Compare & Sync Tool"
#define MyAppVersion   "2.0.0"
#define MyAppPublisher "IM HosXP Plus"
#define MyAppURL       "https://github.com/tannamnaja-ui/tool-DB-Compare---Sync-Tool"
#define MyAppExeName   "tool-DB-Compare-Sync.exe"
#define MyAppDir       "DB Compare Sync Tool"
#define MyAppID        "4F2A8D1E-7B3C-4E9A-B6D5-0F1234567890"

[Setup]
AppId={{{#MyAppID}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={autopf}\{#MyAppDir}
DefaultGroupName={#MyAppName}
AllowNoIcons=yes
; --- offline installer: ไม่ต้องใช้ internet ---
OutputDir=..\setup
OutputBaseFilename=tool-DB-Compare-Sync-Setup
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\{#MyAppExeName}
; ผู้ใช้ทั่วไป (ไม่ต้อง admin) หรือเลือก admin ได้
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesInstallIn64BitMode=x64compatible
; ไม่มีภาษาไทยใน Inno Setup default — ใช้ English
MinVersion=10.0

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon";  Description: "สร้าง Shortcut บน Desktop";         GroupDescription: "Shortcuts:";   Flags: unchecked
Name: "startupicon";  Description: "เริ่มโปรแกรมอัตโนมัติตอน Windows เปิด"; GroupDescription: "Auto-start:";  Flags: unchecked

[Files]
; --- exe หลัก (self-contained — Python + Flask + deps ทุกอย่างอยู่ในไฟล์เดียว) ---
Source: "..\dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion
; --- config ตัวอย่าง (ไม่ทับถ้ามีอยู่แล้ว) ---
Source: "..\config.example.json"; DestDir: "{app}"; DestName: "config.example.json"; Flags: ignoreversion onlyifdoesntexist

[Icons]
Name: "{group}\{#MyAppName}";                    Filename: "{app}\{#MyAppExeName}"; Comment: "DB Compare & Sync Tool"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{commondesktop}\{#MyAppName}";            Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon; Comment: "DB Compare & Sync Tool"
Name: "{userstartup}\{#MyAppName}";              Filename: "{app}\{#MyAppExeName}"; Tasks: startupicon; Comment: "DB Compare & Sync Tool"

[Run]
; เปิดโปรแกรมทันทีหลังติดตั้ง (ไม่มี CMD window เพราะ console=False ใน PyInstaller)
Filename: "{app}\{#MyAppExeName}"; Description: "เปิดโปรแกรมหลังติดตั้ง"; Flags: nowait postinstall skipifsilent

[Code]
// =====================================================================
// ตรวจสอบก่อนติดตั้ง:
//   - ถ้าติดตั้งอยู่แล้ว เวอร์ชันเดิม → ถามว่าจะติดตั้งทับหรือไม่
//   - ถ้าเวอร์ชันใหม่กว่า → อัพเดทโดยไม่ถาม
// =====================================================================
function GetInstalledVersion(): String;
var
  Ver: String;
begin
  Result := '';
  if RegQueryStringValue(HKEY_CURRENT_USER,
      'Software\Microsoft\Windows\CurrentVersion\Uninstall\{' + '{#MyAppID}' + '}_is1',
      'DisplayVersion', Ver) then
    Result := Ver
  else if RegQueryStringValue(HKEY_LOCAL_MACHINE,
      'Software\Microsoft\Windows\CurrentVersion\Uninstall\{' + '{#MyAppID}' + '}_is1',
      'DisplayVersion', Ver) then
    Result := Ver;
end;

function CompareVersions(V1, V2: String): Integer;
var
  P1, P2, N1, N2: Integer;
  S1, S2: String;
begin
  Result := 0;
  while (V1 <> '') or (V2 <> '') do
  begin
    P1 := Pos('.', V1); if P1 = 0 then P1 := Length(V1) + 1;
    P2 := Pos('.', V2); if P2 = 0 then P2 := Length(V2) + 1;
    S1 := Copy(V1, 1, P1 - 1); V1 := Copy(V1, P1 + 1, MaxInt);
    S2 := Copy(V2, 1, P2 - 1); V2 := Copy(V2, P2 + 1, MaxInt);
    N1 := StrToIntDef(S1, 0);
    N2 := StrToIntDef(S2, 0);
    if N1 > N2 then begin Result := 1;  Exit; end;
    if N1 < N2 then begin Result := -1; Exit; end;
  end;
end;

function InitializeSetup(): Boolean;
var
  InstalledVer: String;
  Cmp: Integer;
begin
  Result := True;
  InstalledVer := GetInstalledVersion();
  if InstalledVer = '' then Exit;   // ยังไม่ได้ติดตั้ง → ติดตั้งปกติ

  Cmp := CompareVersions('{#MyAppVersion}', InstalledVer);

  if Cmp > 0 then
  begin
    // เวอร์ชันใหม่กว่า → อัพเดทโดยไม่ถาม
    MsgBox('พบ ' + '{#MyAppName}' + ' เวอร์ชัน ' + InstalledVer + ' ติดตั้งอยู่' + #13#10 +
           'จะอัพเดทเป็นเวอร์ชัน ' + '{#MyAppVersion}' + ' อัตโนมัติ', mbInformation, MB_OK);
    Result := True;
  end
  else if Cmp = 0 then
  begin
    // เวอร์ชันเดิม → ถามว่าจะติดตั้งทับหรือไม่
    Result := (MsgBox('พบ ' + '{#MyAppName}' + ' เวอร์ชัน ' + InstalledVer + ' ติดตั้งอยู่แล้ว' + #13#10 +
                      'ต้องการติดตั้งใหม่ทับหรือไม่?', mbConfirmation, MB_YESNO) = IDYES);
  end
  else
  begin
    // เวอร์ชันที่ติดตั้งใหม่กว่า → แจ้งเตือนและยกเลิก
    MsgBox('พบ ' + '{#MyAppName}' + ' เวอร์ชันใหม่กว่า (' + InstalledVer + ') ติดตั้งอยู่แล้ว' + #13#10 +
           'ไม่สามารถติดตั้งเวอร์ชันเก่ากว่าได้', mbError, MB_OK);
    Result := False;
  end;
end;
