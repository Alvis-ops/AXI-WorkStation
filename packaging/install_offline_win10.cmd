@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_offline_win10.ps1" %*
exit /b %ERRORLEVEL%
