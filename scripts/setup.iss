; wxsp Windows 安装器(M11)。
; 用 ISCC.exe 编译;由 build_windows.ps1 调用,/DAppVersion + /DSourceDir 通过命令行传入。

; AppName = 用户在控制面板/卸载列表/开始菜单看到的产品名(中文)。
; ExeName / OutputBaseFilename 保持 ASCII,避免 Windows 路径/分发场景的编码坑。
#define AppName "自动发布平台"
#define ExeName "wxsp"
#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\dist\wxsp.dist"
#endif

[Setup]
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppName}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
OutputDir=..\dist
OutputBaseFilename={#ExeName}-{#AppVersion}-setup
Compression=lzma2
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
DisableProgramGroupPage=yes
WizardStyle=modern
; SetupIconFile=..\assets\icon.ico  ; M11: assets/icon.ico 是空占位,真图标后补

[Languages]
; choco 装的 Inno Setup 6 默认只带 Default.isl(英文),中文翻译要单独下;
; 内部分发,装一次就行,先简单用英文界面。
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#ExeName}.exe"
Name: "{group}\卸载 {#AppName}"; Filename: "{uninstallexe}"
Name: "{commondesktop}\{#AppName}"; Filename: "{app}\{#ExeName}.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\{#ExeName}.exe"; Description: "启动 {#AppName}"; Flags: nowait postinstall skipifsilent
