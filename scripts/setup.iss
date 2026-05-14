; wxsp Windows 安装器(M11)。
; 用 ISCC.exe 编译;由 build_windows.ps1 调用,/DAppVersion + /DSourceDir 通过命令行传入。

#define AppName "wxsp"
#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\dist\wxsp.dist"
#endif

[Setup]
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=wxsp
DefaultDirName={autopf}\wxsp
DefaultGroupName=wxsp
OutputDir=..\dist
OutputBaseFilename={#AppName}-{#AppVersion}-setup
Compression=lzma2
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
DisableProgramGroupPage=yes
WizardStyle=modern
SetupIconFile=..\assets\icon.ico

[Languages]
Name: "chinese"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "autostart"; Description: "开机自动启动 wxsp"; GroupDescription: "附加任务"
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\wxsp"; Filename: "{app}\wxsp.exe"
Name: "{group}\卸载 wxsp"; Filename: "{uninstallexe}"
Name: "{commondesktop}\wxsp"; Filename: "{app}\wxsp.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\wxsp.exe"; Parameters: "autostart enable"; Tasks: autostart; Flags: runhidden waituntilterminated
Filename: "{app}\wxsp.exe"; Description: "启动 wxsp"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{app}\wxsp.exe"; Parameters: "autostart disable"; Flags: runhidden waituntilterminated
