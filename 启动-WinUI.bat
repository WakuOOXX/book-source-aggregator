@echo off
setlocal
REM NovelDownloader (WinUI 3) launcher. Auto-builds if exe missing.
set "ROOT=%~dp0"
set "PROJ=%ROOT%frontends\winui"
set "EXE=%PROJ%src\NovelDownloaderind\Debug
et8.0-windows10.0.19041.0\win-x64\NovelDownloader.exe"

if exist "%EXE%" goto launch

echo [INFO] exe not found, building first (about 15s)...
cd /d "%PROJ%"
dotnet build -c Debug -p:Platform=x64
if errorlevel 1 (
  echo [ERROR] build failed. Need .NET 8 SDK + nuget.org access.
  pause
  exit /b 1
)

:launch
if not exist "%EXE%" (
  echo [ERROR] exe still missing: %EXE%
  pause
  exit /b 1
)
echo Starting NovelDownloader (WinUI)...
start "" "%EXE%"
endlocal
