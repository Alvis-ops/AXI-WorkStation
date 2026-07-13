@echo off
setlocal
set AXI_BARE_BOARD_CMD_LOG=%TEMP%\AxiBareBoardWorkstation_install_cmd.log
echo [%DATE% %TIME%] Axi Bare Board Workstation installer started > "%AXI_BARE_BOARD_CMD_LOG%"
if /I "%~1"=="/quiet" (
    set AXI_BARE_BOARD_NO_PATH_DIALOG=1
    set AXI_BARE_BOARD_NO_MESSAGE=1
    set AXI_BARE_BOARD_NO_OPEN=1
)
powershell.exe -STA -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" >> "%AXI_BARE_BOARD_CMD_LOG%" 2>&1
set AXI_BARE_BOARD_EXIT=%ERRORLEVEL%
echo [%DATE% %TIME%] installer exit code: %AXI_BARE_BOARD_EXIT% >> "%AXI_BARE_BOARD_CMD_LOG%"
if not "%AXI_BARE_BOARD_EXIT%"=="0" (
    powershell.exe -NoProfile -Command "$ws=New-Object -ComObject WScript.Shell; [void]$ws.Popup('Axi Bare Board Workstation installation failed. Log: %TEMP%\AxiBareBoardWorkstation_install_cmd.log',0,'Axi Bare Board Workstation Setup Failed',16)" >nul 2>nul
)
exit /b %AXI_BARE_BOARD_EXIT%
