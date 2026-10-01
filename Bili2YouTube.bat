@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

rem ImDisk normally needs an elevated token to create/use the temporary RAM disk.
rem Ask for elevation only when this launcher is not already Administrator.
fltmc >nul 2>&1
if errorlevel 1 (
    echo Requesting Administrator permission for the temporary RAM disk...
    powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "$p = Start-Process -FilePath 'powershell.exe' -Verb RunAs -Wait -PassThru -ArgumentList @('-NoLogo','-NoProfile','-ExecutionPolicy','Bypass','-File','%~dp0Bili2YouTube.ps1'); exit $p.ExitCode"
    set "exitCode=!ERRORLEVEL!"
) else (
    powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0Bili2YouTube.ps1"
    set "exitCode=!ERRORLEVEL!"
)

echo.
if "%exitCode%"=="0" (
    echo Import finished successfully.
) else (
    echo Import stopped with exit code %exitCode%.
)
pause
exit /b %exitCode%
