@echo off
setlocal
cd /d "%~dp0"

net session >nul 2>&1
if not "%ERRORLEVEL%"=="0" (
    echo Requesting administrator privileges for dependency install...
    powershell.exe -NoProfile -Command "$p=Start-Process -FilePath '%~f0' -ArgumentList '%*' -Verb RunAs -Wait -PassThru; exit $p.ExitCode"
    exit /b %ERRORLEVEL%
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_offline_win10.ps1" %*
exit /b %ERRORLEVEL%
