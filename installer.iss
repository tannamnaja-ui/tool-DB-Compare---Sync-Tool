#define MyAppName      "DB Compare & Sync Tool"
#define MyAppVersion   "2.0"
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
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon";  Description: "สร้าง Desktop shortcut";          GroupDescription: "Shortcuts:"; Flags: unchecked
Name: "startupicon";  Description: "เริ่มโปรแกรมอัตโนมัติตอน login"; GroupDescription: "Auto-start:"; Flags: unchecked

[Files]
; Main application (standalone — Python + all libraries bundled inside)
Source: "dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion

; Visual C++ 2015-2022 Redistributable (bundled for offline install)
Source: "installer\redist\vc_redist.x64.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall

[Icons]
Name: "{group}\{#MyAppName}";               Filename: "{app}\{#MyAppExeName}"; Comment: "DB Compare & Sync Tool"
Name: "{group}\Uninstall {#MyAppName}";     Filename: "{uninstallexe}"
Name: "{commondesktop}\{#MyAppName}";       Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon
Name: "{userstartup}\{#MyAppName}";         Filename: "{app}\{#MyAppExeName}"; Tasks: startupicon

[Run]
; Install vcredist silently only if not already installed
Filename: "{tmp}\vc_redist.x64.exe"; Parameters: "/install /quiet /norestart"; \
  Flags: runhidden waituntilterminated; \
  Check: NeedsVCRedist

; Launch app after install (no CMD window — tray icon mode)
Filename: "{app}\{#MyAppExeName}"; Description: "เปิดโปรแกรมหลังติดตั้ง"; \
  Flags: nowait postinstall skipifsilent

[Code]

{ ─────────────────────────────────────────────────────────────────────────────
  Returns True when vcredist x64 >= 14.20 is NOT installed yet.
  Inno Setup calls this before running vc_redist.x64.exe in [Run].
  ───────────────────────────────────────────────────────────────────────────── }
function NeedsVCRedist: Boolean;
var
  Installed: Cardinal;
  RegVer:    String;
begin
  Result := True;

  if RegQueryDWordValue(HKEY_LOCAL_MACHINE,
       'SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64',
       'Installed', Installed) then
  begin
    if (Installed = 1) and
       RegQueryStringValue(HKEY_LOCAL_MACHINE,
         'SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64',
         'Version', RegVer) and
       (CompareStr(RegVer, 'v14.20') >= 0) then
    begin
      Result := False;
    end;
  end;
end;

{ ─────────────────────────────────────────────────────────────────────────────
  Ask user before re-installing over an existing installation.
  ───────────────────────────────────────────────────────────────────────────── }
function InitializeSetup(): Boolean;
var
  Version: String;
begin
  Result := True;
  if RegQueryStringValue(HKEY_LOCAL_MACHINE,
       'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\' +
       '{A1B2C3D4-E5F6-7890-ABCD-EF1234567890}_is1',
       'DisplayVersion', Version) then
  begin
    if MsgBox('พบการติดตั้ง ' + '{#MyAppName}' + ' เวอร์ชัน ' + Version +
              ' อยู่แล้ว' + #13#10 + 'ต้องการติดตั้งทับใหม่หรือไม่?',
              mbConfirmation, MB_YESNO) = IDNO then
    begin
      Result := False;
    end;
  end;
end;
