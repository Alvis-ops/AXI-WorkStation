@echo off
setlocal
set AXI_FACTORY_CMD_LOG=%TEMP%\AxiFactoryWorkstation_install_cmd.log
echo [%DATE% %TIME%] Axi Factory Workstation installer started > "%AXI_FACTORY_CMD_LOG%"
if /I "%~1"=="/quiet" (
    set AXI_FACTORY_NO_PATH_DIALOG=1
    set AXI_FACTORY_NO_MESSAGE=1
    set AXI_FACTORY_NO_OPEN=1
)
powershell.exe -STA -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" >> "%AXI_FACTORY_CMD_LOG%" 2>&1
set AXI_FACTORY_EXIT=%ERRORLEVEL%
echo [%DATE% %TIME%] installer exit code: %AXI_FACTORY_EXIT% >> "%AXI_FACTORY_CMD_LOG%"
if not "%AXI_FACTORY_EXIT%"=="0" (
    powershell.exe -NoProfile -Command "$ws=New-Object -ComObject WScript.Shell; [void]$ws.Popup('Axi Factory Workstation installation failed. Log: %TEMP%\AxiFactoryWorkstation_install_cmd.log',0,'Axi Factory Workstation Setup Failed',16)" >nul 2>nul
)
exit /b %AXI_FACTORY_EXIT%
