@echo off
setlocal EnableExtensions
cd /d "%~dp0"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0PackMusicLibraryExtension.ps1" -OpenFolder
set "exitCode=%ERRORLEVEL%"
if not "%exitCode%"=="0" (
  echo.
  echo Packaging failed with exit code %exitCode%.
  pause
)
exit /b %exitCode%
