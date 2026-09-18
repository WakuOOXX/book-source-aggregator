@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem ============================================================
rem  书源聚合下载器 安装包一键构建
rem  链路: 冻结后端 (PyInstaller onefile) → 发布前端 (self-contained)
rem        → 组装 installer\staging → Inno Setup 编译 → dist\ 成品
rem  前置: dotnet SDK / .build-venv (Python 3.12 + pyinstaller)
rem        / Inno Setup 6 / 构建机本地 shuyuan\bookSource.json (种子书源, 不入库)
rem ============================================================

set VERSION=1.31
set VENV=.build-venv
set STAGING=installer\staging

echo [1/5] 环境检测...

where dotnet >nul 2>nul
if errorlevel 1 (echo   [X] 缺 dotnet SDK, 请先安装 .NET 8 SDK。 & exit /b 1)

if not exist "%VENV%\Scripts\pyinstaller.exe" (
    echo   [X] 缺打包 venv。先创建 ^(Python 3.12, 3.14 与 py_mini_racer 不兼容^):
    echo       py -3.12 -m venv .build-venv
    echo       .build-venv\Scripts\pip install requests curl_cffi websocket-client beautifulsoup4 mini-racer pyinstaller
    exit /b 1
)

set "ISCC="
where iscc >nul 2>nul && set "ISCC=iscc"
if not defined ISCC if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not defined ISCC if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
rem winget 用户级安装落点 (JRSoftware.InnoSetup 无管理员时在这)。
if not defined ISCC if exist "%LocalAppData%\Programs\Inno Setup 6\ISCC.exe" set "ISCC=%LocalAppData%\Programs\Inno Setup 6\ISCC.exe"
if not defined ISCC (
    echo   [X] 缺 Inno Setup 6。安装: winget install JRSoftware.InnoSetup
    exit /b 1
)

if not exist shuyuan\bookSource.json (
    echo   [X] 缺种子书源 shuyuan\bookSource.json ^(构建机本地文件, 不入 git^)。
    exit /b 1
)

echo   dotnet / pyinstaller / Inno Setup / 种子书源 OK

rmdir /s /q "%STAGING%" 2>nul
rmdir /s /q "installer\dist" 2>nul
rmdir /s /q "installer\build" 2>nul

echo [2/5] 冻结后端 bookdl-backend.exe (PyInstaller onefile)...
"%VENV%\Scripts\pyinstaller.exe" installer\backend.spec --noconfirm --distpath installer\dist --workpath installer\build
if errorlevel 1 (echo   [X] 后端冻结失败, 见上方 PyInstaller 输出。 & exit /b 1)

echo [3/5] 发布前端 (Release win-x64 self-contained)...
dotnet publish frontends\winui\src\NovelDownloader\NovelDownloader.csproj -c Release -r win-x64 --self-contained true -o "%STAGING%"
if errorlevel 1 (echo   [X] 前端发布失败。 & exit /b 1)

echo [4/5] 组装 staging (后端 exe + 种子书源)...
copy /y "installer\dist\bookdl-backend.exe" "%STAGING%\" >nul
if errorlevel 1 (echo   [X] 找不到 installer\dist\bookdl-backend.exe。 & exit /b 1)
mkdir "%STAGING%\seed" 2>nul
copy /y shuyuan\bookSource.json "%STAGING%\seed\bookSource.json" >nul

echo [5/5] 编译安装包 (Inno Setup)...
set "ISL="
if exist "%ProgramFiles(x86)%\Inno Setup 6\Languages\ChineseSimplified.isl" set "ISL=%ProgramFiles(x86)%\Inno Setup 6\Languages\ChineseSimplified.isl"
if not defined ISL if exist "%ProgramFiles%\Inno Setup 6\Languages\ChineseSimplified.isl" set "ISL=%ProgramFiles%\Inno Setup 6\Languages\ChineseSimplified.isl"
if not defined ISL if exist "%LocalAppData%\Programs\Inno Setup 6\Languages\ChineseSimplified.isl" set "ISL=%LocalAppData%\Programs\Inno Setup 6\Languages\ChineseSimplified.isl"
if defined ISL (
    "%ISCC%" /DAppVersion=%VERSION% "/DChineseIsl=%ISL%" installer\setup.iss
) else (
    echo   ! 未装简中语言文件 ChineseSimplified.isl, 向导用英文。
    "%ISCC%" /DAppVersion=%VERSION% installer\setup.iss
)
if errorlevel 1 (echo   [X] 安装包编译失败。 & exit /b 1)

mkdir dist 2>nul
copy /y "installer\out\*-%VERSION%.exe" dist\ >nul

echo.
echo ✔ 完成: dist\ 下的 BookSourceAggregator-Setup-%VERSION%.exe
echo   直接上传 GitHub Release (资产名限 ASCII, 与产物同名)。
