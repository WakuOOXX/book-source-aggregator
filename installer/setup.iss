; 书源聚合下载器 安装脚本 (Inno Setup 6)
; 构建 (仓库根): build-installer.bat, 或直接
;   ISCC.exe /DAppVersion=1.31 /DChineseIsl="<简中语言文件路径>" installer\setup.iss
; 前置: installer\staging\ 已组装好 (前端 publish 输出 + bookdl-backend.exe + seed\bookSource.json)。
;
; 数据目录约定: 运行时数据在 %LOCALAPPDATA%\BookSourceAggregator (可在设置页改),
; 卸载只删程序本体, 不动用户数据/已下载的书。

#define AppName "书源聚合下载器"
#define AppPublisher "WakuOOXX"
#define InstallDirName "BookSourceAggregator"

[Setup]
; AppId 固定 GUID: 升级安装/卸载识别都靠它, 永远不要改。
AppId={{7B4E9C2A-6D15-4F8B-A0C3-9E2D71F58B46}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL=https://github.com/WakuOOXX/book-source-aggregator
DefaultDirName={autopf}\{#InstallDirName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
; 允许安装时自选目录 (需求: 下载→解压→安装→选目录)。
DisableDirPage=no
UninstallDisplayIcon={app}\{#AppName}.exe
OutputDir=out
OutputBaseFilename={#AppName}-Setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern
PrivilegesRequired=admin
; 允许向导/命令行改为用户级安装 (无管理员也能装到 %LOCALAPPDATA%)。
PrivilegesRequiredOverridesAllowed=dialog commandline

[Languages]
; 简中界面: build-installer.bat 探测到 ChineseSimplified.isl 才传 /DChineseIsl,
; 没传时退英文向导 (Inno 官方包不含简中语言文件)。
#ifdef ChineseIsl
Name: "chinesesimplified"; MessagesFile: "{#ChineseIsl}"
#endif
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
; 主程序改名落盘 (程序集名保持 NovelDownloader, 不改以免孤儿化老用户的 settings.json)。
Source: "staging\NovelDownloader.exe"; DestDir: "{app}"; DestName: "{#AppName}.exe"; Flags: ignoreversion
Source: "staging\*"; DestDir: "{app}"; Excludes: "NovelDownloader.exe"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppName}.exe"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppName}.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务:"

[Run]
Filename: "{app}\{#AppName}.exe"; Description: "完成后立即运行 {#AppName}"; Flags: nowait postinstall skipifsilent
