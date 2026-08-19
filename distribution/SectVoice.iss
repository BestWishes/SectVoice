#ifndef AppVersion
  #define AppVersion "0.3.1"
#endif
#define CatalogUrl "https://github.com/BestWishes/SectVoice-Downloads/releases/latest/download/catalog.json"
#ifndef CoreInstalledBytes
  #define CoreInstalledBytes 1049524708
#endif
#ifndef BasicDownloadBytes
  #define BasicDownloadBytes 420796975
#endif
#ifndef BasicInstalledBytes
  #define BasicInstalledBytes 881108266
#endif
#ifndef StandardDownloadBytes
  #define StandardDownloadBytes 5434735730
#endif
#ifndef StandardInstalledBytes
  #define StandardInstalledBytes 8577931694
#endif
#ifndef ReleaseAssetDir
  #define ReleaseAssetDir "v" + AppVersion
#endif
#ifndef ReleaseRoot
  #define ReleaseRoot "..\..\release"
#endif
#ifndef InstallerOutputDir
  #define InstallerOutputDir "..\..\release\installer"
#endif

[Setup]
AppId={{5D4A2989-0718-4BF1-9A8B-E6F2098633B5}
AppName=SectVoice Reader
AppVersion={#AppVersion}
AppPublisher=BestWishes
AppPublisherURL=https://github.com/BestWishes/SectVoice-Downloads
AppUpdatesURL=https://github.com/BestWishes/SectVoice-Downloads/releases/latest
DefaultDirName={localappdata}\SectVoice
DefaultGroupName=SectVoice Reader
OutputDir={#InstallerOutputDir}
OutputBaseFilename=SectVoice-Setup-{#AppVersion}-x64
Compression=lzma2/ultra64
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest
WizardStyle=modern dynamic
UninstallDisplayName=SectVoice Reader
LicenseFile=..\LICENSE
InfoBeforeFile=README-INSTALL.txt
SetupLogging=yes
SetupIconFile=..\src\sectvoice\assets\VoiceIcon.ico
UninstallDisplayIcon={app}\app\SectVoiceReader.exe

[Languages]
Name: "chinesesimp"; MessagesFile: "languages\ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Components]
Name: "core"; Description: "Reader Core（必需，含FFmpeg和声音创建转写组件）"; Types: full compact custom; Flags: fixed
Name: "basic"; Description: "Basic基础语音包（CPU；下载约0.39GiB，安装约0.82GiB）"; Types: full
Name: "standard"; Description: "Standard中级语音包（NVIDIA GPU/CUDA；下载约5.06GiB，安装约7.99GiB）"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "快捷方式："; Flags: unchecked

[Files]
Source: "{#ReleaseRoot}\{#ReleaseAssetDir}\core-root\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs; Components: core

[Icons]
Name: "{group}\SectVoice Reader"; Filename: "{app}\app\SectVoiceReader.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\SectVoice Reader"; Filename: "{app}\app\SectVoiceReader.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\app\SectVoiceReader.exe"; Description: "启动 SectVoice Reader"; Flags: nowait postinstall skipifsilent; Components: core

[UninstallDelete]
Type: filesandordirs; Name: "{app}\runtime\engines"
Type: filesandordirs; Name: "{app}\models\basic"
Type: filesandordirs; Name: "{app}\models\standard"
Type: filesandordirs; Name: "{app}\downloads"
Type: filesandordirs; Name: "{app}\temp"
Type: filesandordirs; Name: "{app}\cache"

[Code]
var
  PreviousComponentsClickCheck: TNotifyEvent;
  FailedPackages: String;

function GetDiskFreeSpaceExW(
  lpDirectoryName: String;
  var lpFreeBytesAvailableToCaller: Int64;
  var lpTotalNumberOfBytes: Int64;
  var lpTotalNumberOfFreeBytes: Int64
): Boolean;
  external 'GetDiskFreeSpaceExW@kernel32.dll stdcall';

function SelectedDownloadBytes: Int64;
begin
  Result := 0;
  if WizardIsComponentSelected('basic') then
    Result := Result + {#BasicDownloadBytes};
  if WizardIsComponentSelected('standard') then
    Result := Result + {#StandardDownloadBytes};
end;

function SelectedInstalledBytes: Int64;
begin
  Result := {#CoreInstalledBytes};
  if WizardIsComponentSelected('basic') then
    Result := Result + {#BasicInstalledBytes};
  if WizardIsComponentSelected('standard') then
    Result := Result + {#StandardInstalledBytes};
end;

function SelectedPeakBytes: Int64;
begin
  { The online ZIPs coexist with Core and extracted package staging until
    promotion succeeds.  Add 512MiB for filesystem and installer overhead. }
  Result := SelectedInstalledBytes + SelectedDownloadBytes + 536870912;
end;

function GiB(const Bytes: Int64): String;
var
  Whole, Hundredths: Int64;
begin
  Whole := Bytes div 1073741824;
  Hundredths := ((Bytes mod 1073741824) * 100 + 536870912) div 1073741824;
  if Hundredths >= 100 then begin
    Whole := Whole + 1;
    Hundredths := 0;
  end;
  Result := IntToStr(Whole) + '.';
  if Hundredths < 10 then
    Result := Result + '0';
  Result := Result + IntToStr(Hundredths);
end;

procedure UpdateSelectedSpaceLabel;
begin
  WizardForm.ComponentsDiskSpaceLabel.Caption :=
    '当前选择：需要下载 ' + GiB(SelectedDownloadBytes) + ' GiB；' +
    '安装完成约占用 ' + GiB(SelectedInstalledBytes) + ' GiB；' +
    '安装过程请至少保留 ' + GiB(SelectedPeakBytes) + ' GiB 空闲空间。';
end;

procedure ComponentsClickCheck(Sender: TObject);
begin
  if PreviousComponentsClickCheck <> nil then
    PreviousComponentsClickCheck(Sender);
  UpdateSelectedSpaceLabel;
end;

function ExistingAncestor(Path: String): String;
var
  ParentPath: String;
begin
  Result := Path;
  while not DirExists(Result) do begin
    ParentPath := ExtractFileDir(RemoveBackslashUnlessRoot(Result));
    if ParentPath = Result then
      Break;
    Result := ParentPath;
  end;
end;

function HasSelectedInstallSpace: Boolean;
var
  FreeBytes, TotalBytes, TotalFreeBytes, RequiredBytes: Int64;
  CheckPath: String;
begin
  Result := True;
  CheckPath := ExistingAncestor(WizardDirValue);
  { Query the volume directly every time Next is clicked.  This avoids a stale
    wizard/session value after the user frees space without closing Setup. }
  if not GetDiskFreeSpaceExW(
    AddBackslash(CheckPath), FreeBytes, TotalBytes, TotalFreeBytes
  ) then begin
    MsgBox('无法读取目标磁盘的剩余空间：' + CheckPath, mbError, MB_OK);
    Result := False;
    Exit;
  end;
  RequiredBytes := SelectedPeakBytes;
  if FreeBytes < RequiredBytes then begin
    MsgBox(
      '目标磁盘空间不足。当前组件安装过程至少需要 ' +
      GiB(RequiredBytes) + ' GiB 空闲空间，但现在只有 ' +
      GiB(FreeBytes) + ' GiB。' + #13#10 +
      '请减少组件、清理目标磁盘，或返回选择其它安装目录。',
      mbError, MB_OK
    );
    Result := False;
  end;
end;

function HasCoreInstallSpace: Boolean;
var
  FreeBytes, TotalBytes, TotalFreeBytes, RequiredBytes: Int64;
  CheckPath: String;
begin
  Result := True;
  CheckPath := ExistingAncestor(WizardDirValue);
  if not GetDiskFreeSpaceExW(
    AddBackslash(CheckPath), FreeBytes, TotalBytes, TotalFreeBytes
  ) then begin
    MsgBox('无法读取目标磁盘的剩余空间：' + CheckPath, mbError, MB_OK);
    Result := False;
    Exit;
  end;
  { Before the component page, only enforce the mandatory Core requirement;
    otherwise a user with room for Basic could never reach the page where
    Standard can be deselected. }
  RequiredBytes := {#CoreInstalledBytes} + 536870912;
  if FreeBytes < RequiredBytes then begin
    MsgBox(
      '目标磁盘空间不足。Reader Core安装过程至少需要 ' +
      GiB(RequiredBytes) + ' GiB空闲空间，但现在只有 ' +
      GiB(FreeBytes) + ' GiB。请清理目标磁盘或选择其它安装目录。',
      mbError, MB_OK
    );
    Result := False;
  end;
end;

procedure InitializeWizard;
begin
  PreviousComponentsClickCheck := WizardForm.ComponentsList.OnClickCheck;
  WizardForm.ComponentsList.OnClickCheck := @ComponentsClickCheck;
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  if CurPageID = wpSelectComponents then
    UpdateSelectedSpaceLabel;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if CurPageID = wpSelectDir then
    Result := HasCoreInstallSpace
  else if (CurPageID = wpSelectComponents) or
     (CurPageID = wpReady) then
    Result := HasSelectedInstallSpace;
end;

function InstallOnlinePackage(
  const TierName: String;
  const DisplayName: String
): Boolean;
var
  ResultCode: Integer;
begin
  Result := False;
  WizardForm.StatusLabel.Caption := '正在下载、校验并安装' + DisplayName +
    '；安装器会保持在当前页面，请勿关闭……';
  WizardForm.StatusLabel.Refresh;
  if not ExecAndLogOutput(
    ExpandConstant('{app}\app\SectVoicePackage.exe'),
    'install-catalog ' + TierName + ' --catalog "{#CatalogUrl}"',
    ExpandConstant('{app}'), SW_SHOWNORMAL, ewWaitUntilTerminated, ResultCode, nil
  ) then begin
    Log('Cannot start online package helper for ' + TierName);
    FailedPackages := FailedPackages + DisplayName + '（无法启动安装助手）' + #13#10;
    Exit;
  end;
  if ResultCode <> 0 then begin
    Log('Online package helper failed for ' + TierName +
      ', exit code ' + IntToStr(ResultCode));
    FailedPackages := FailedPackages + DisplayName + '（下载、校验或安装未完成）' + #13#10;
    Exit;
  end;
  Result := True;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then begin
    FailedPackages := '';
    if WizardIsComponentSelected('basic') then
      InstallOnlinePackage('basic', 'Basic基础语音包');
    if WizardIsComponentSelected('standard') then
      InstallOnlinePackage('standard', 'Standard中级语音包');
    if FailedPackages <> '' then begin
      Log('One or more optional online packages were not installed: ' + FailedPackages);
      if not WizardSilent then
        MsgBox(
          'Reader Core已经安装完成，但以下在线语音包未能完成：' + #13#10 +
          FailedPackages + #13#10 +
          '这不会伪装成语音包安装成功。请启动Reader，在“管理语音引擎/模型包”中重试；'
          + '已下载的有效断点会继续使用。详细记录：' +
          ExpandConstant('{app}\data\logs\package-installer.log'),
          mbError, MB_OK
        );
    end;
  end;
end;
