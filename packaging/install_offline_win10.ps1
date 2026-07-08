# Offline install orchestrator for Win10 x64 factory workstation package.
param(
    [string]$PackageRoot = "",
    [string]$InstallRoot = "",
    [switch]$SkipVcRedist,
    [switch]$SkipNrfConnect,
    [switch]$SkipJLink,
    [switch]$SkipNordicCli,
    [switch]$SkipFirmware,
    [switch]$SkipWorkstation,
    [switch]$SkipHashCheck,
    [switch]$Quiet
)

$ErrorActionPreference = "Stop"

if (-not $PackageRoot) {
    $PackageRoot = Split-Path -Parent $PSCommandPath
}
if (-not $InstallRoot) {
    $InstallRoot = Join-Path $env:LOCALAPPDATA "Programs\Axi Factory Workstation"
}

$script:LogPath = Join-Path ([System.IO.Path]::GetTempPath()) "AxiFactoryWorkstation_offline_install.log"

function Write-OfflineLog {
    param([string]$Message)
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Add-Content -LiteralPath $script:LogPath -Value $line -Encoding UTF8
    if (-not $Quiet) { Write-Host $line }
}

function Show-OfflineMessage {
    param([string]$Title, [string]$Message)
    if ($Quiet) { return }
    try {
        $shell = New-Object -ComObject WScript.Shell
        [void]$shell.Popup($Message, 0, $Title, 64)
    } catch {
        Write-Host "$Title`n$Message"
    }
}

function Install-VcRedist {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        Write-OfflineLog "VC++ redistributable not found; skipping: $Path"
        return $false
    }
    Write-OfflineLog "Installing VC++ redistributable: $Path"
    $proc = Start-Process -FilePath $Path -ArgumentList "/install", "/quiet", "/norestart" -Wait -PassThru
    Write-OfflineLog ("VC++ installer exit code: {0}" -f $proc.ExitCode)
    return ($proc.ExitCode -eq 0 -or $proc.ExitCode -eq 1638 -or $proc.ExitCode -eq 3010)
}

function Test-OfflinePackageHashes {
    param([string]$Root)
    $sumFile = Join-Path $Root "SHA256SUMS.txt"
    if (-not (Test-Path -LiteralPath $sumFile)) {
        Write-OfflineLog "SHA256SUMS.txt not found; skipping integrity check"
        return
    }
    Write-OfflineLog "Verifying SHA256 checksums"
    $checked = 0
    foreach ($line in (Get-Content -LiteralPath $sumFile)) {
        $trimmed = $line.Trim()
        if (-not $trimmed) { continue }
        if ($trimmed -notmatch '^([0-9a-fA-F]{64})\s+(.+)$') {
            throw "Invalid checksum line: $line"
        }
        $expected = $matches[1].ToLowerInvariant()
        $relative = $matches[2].Trim().Replace('/', '\')
        $path = Join-Path $Root $relative
        if (-not (Test-Path -LiteralPath $path)) {
            throw "Checksum file missing target: $relative"
        }
        $actual = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actual -ne $expected) {
            throw "Checksum mismatch: $relative"
        }
        $checked++
    }
    Write-OfflineLog "SHA256 checksum OK: $checked files"
}

function Install-NrfConnectBlePortable {
    param([string]$SourceDir, [string]$TargetDir)
    if (-not (Test-Path -LiteralPath $SourceDir)) { throw "nRF Connect BLE bundle missing: $SourceDir" }
    $exeName = "nRF Connect for Desktop Bluetooth Low Energy.exe"
    if (-not (Test-Path -LiteralPath (Join-Path $SourceDir $exeName))) { throw "Invalid nRF Connect BLE bundle; missing $exeName" }
    Write-OfflineLog "Deploying nRF Connect BLE to $TargetDir"
    New-Item -ItemType Directory -Path $TargetDir -Force | Out-Null
    & robocopy $SourceDir $TargetDir /MIR /NFL /NDL /NJH /NJS /NP | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "Failed copying nRF Connect BLE (robocopy exit $LASTEXITCODE)" }
    return $true
}

function Install-ExeDependency {
    param(
        [string]$Name,
        [string]$Path,
        [string[]]$Arguments
    )
    if (-not (Test-Path -LiteralPath $Path)) {
        throw "$Name installer missing: $Path"
    }
    Write-OfflineLog "Installing ${Name}: $Path $($Arguments -join ' ')"
    $proc = Start-Process -FilePath $Path -ArgumentList $Arguments -Wait -PassThru
    Write-OfflineLog ("{0} installer exit code: {1}" -f $Name, $proc.ExitCode)
    if ($proc.ExitCode -ne 0 -and $proc.ExitCode -ne 3010 -and $proc.ExitCode -ne 1638) {
        throw "$Name installer failed with exit code $($proc.ExitCode)"
    }
    return $true
}

function Find-Nrfjprog {
    $candidates = @()
    if (${env:ProgramFiles}) {
        $candidates += (Join-Path ${env:ProgramFiles} "Nordic Semiconductor\nrf-command-line-tools\bin\nrfjprog.exe")
    }
    if (${env:ProgramFiles(x86)}) {
        $candidates += (Join-Path ${env:ProgramFiles(x86)} "Nordic Semiconductor\nrf-command-line-tools\bin\nrfjprog.exe")
    }
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) { return $candidate }
    }
    $cmd = Get-Command nrfjprog -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return ""
}

function Find-JLinkTool {
    $candidates = @()
    if (${env:ProgramFiles}) {
        $candidates += (Join-Path ${env:ProgramFiles} "SEGGER\JLink\JLink.exe")
        $candidates += (Join-Path ${env:ProgramFiles} "SEGGER\JLink\JLinkExe.exe")
    }
    if (${env:ProgramFiles(x86)}) {
        $candidates += (Join-Path ${env:ProgramFiles(x86)} "SEGGER\JLink\JLink.exe")
        $candidates += (Join-Path ${env:ProgramFiles(x86)} "SEGGER\JLink\JLinkExe.exe")
    }
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) { return $candidate }
    }
    return ""
}

function Set-WorkstationDefaultFirmware {
    param(
        [string]$InstallRoot,
        [string]$FirmwareSource,
        [string]$NrfjprogPath,
        [string]$NrfConnectBlePath
    )
    if (-not (Test-Path -LiteralPath $FirmwareSource)) {
        throw "Default firmware hex missing: $FirmwareSource"
    }
    $firmwareTargetDir = Join-Path $InstallRoot "firmware"
    New-Item -ItemType Directory -Path $firmwareTargetDir -Force | Out-Null
    $firmwareTarget = Join-Path $firmwareTargetDir "axi_p1_factory_merged.hex"
    Copy-Item -LiteralPath $FirmwareSource -Destination $firmwareTarget -Force

    $configPath = Join-Path $InstallRoot "config.json"
    if (-not (Test-Path -LiteralPath $configPath)) {
        throw "Workstation config not found: $configPath"
    }
    $config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
    $config | Add-Member -NotePropertyName "half_flash_before_test" -NotePropertyValue $false -Force
    $config | Add-Member -NotePropertyName "flash_backend" -NotePropertyValue "nrfjprog" -Force
    $config | Add-Member -NotePropertyName "flash_image_path" -NotePropertyValue "firmware\axi_p1_factory_merged.hex" -Force
    $config | Add-Member -NotePropertyName "half_flash_image_path" -NotePropertyValue "firmware\axi_p1_factory_merged.hex" -Force
    $config | Add-Member -NotePropertyName "flash_after_wait_s" -NotePropertyValue 8.0 -Force
    $config | Add-Member -NotePropertyName "flash_timeout_s" -NotePropertyValue 180.0 -Force
    $config | Add-Member -NotePropertyName "flash_verify" -NotePropertyValue $true -Force
    $config | Add-Member -NotePropertyName "nrfjprog_path" -NotePropertyValue $(if ($NrfjprogPath) { $NrfjprogPath } else { "nrfjprog" }) -Force
    $config | Add-Member -NotePropertyName "record_output_mode" -NotePropertyValue "unified" -Force
    $config | Add-Member -NotePropertyName "nrf_connect_ble_path" -NotePropertyValue $NrfConnectBlePath -Force
    $config | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $configPath -Encoding UTF8
    Write-OfflineLog "Configured default firmware: $firmwareTarget"
}

function Test-InstalledTools {
    param(
        [string]$InstallRoot,
        [string]$NrfConnectBlePath,
        [string]$FirmwarePath
    )
    $nrfjprog = Find-Nrfjprog
    if (-not $nrfjprog) { throw "nrfjprog.exe not found after Nordic Command Line Tools install" }
    Write-OfflineLog "nrfjprog found: $nrfjprog"
    $version = & $nrfjprog --version 2>&1
    Write-OfflineLog "nrfjprog --version: $version"
    $ids = & $nrfjprog --ids 2>&1
    Write-OfflineLog "nrfjprog --ids: $ids"

    $jlink = Find-JLinkTool
    if (-not $jlink) { throw "SEGGER J-Link tool not found after install" }
    Write-OfflineLog "J-Link tool found: $jlink"

    $bleExe = Join-Path $NrfConnectBlePath "nRF Connect for Desktop Bluetooth Low Energy.exe"
    if (-not (Test-Path -LiteralPath $bleExe)) { throw "nRF Connect BLE backend missing: $bleExe" }
    Write-OfflineLog "nRF Connect BLE backend found: $bleExe"

    if (-not (Test-Path -LiteralPath $FirmwarePath)) { throw "Default firmware hex missing after install: $FirmwarePath" }
    Write-OfflineLog "Default firmware found: $FirmwarePath"

    $exePath = Join-Path $InstallRoot "Axi Factory Workstation.exe"
    if (-not (Test-Path -LiteralPath $exePath)) { throw "Workstation exe missing after install: $exePath" }
    Write-OfflineLog "Workstation exe found: $exePath"
}

function Find-SetupExe {
    param([string]$AppDir)
    $matches = Get-ChildItem -LiteralPath $AppDir -Filter "Axi_Factory_Workstation_Setup_*_win10_x64*.exe" | Sort-Object LastWriteTime -Descending
    if ($matches.Count -eq 0) { throw "Workstation setup exe not found under $AppDir" }
    return $matches[0].FullName
}

try {
    Write-OfflineLog "Offline install started. PackageRoot=$PackageRoot"
    if (-not $SkipHashCheck) {
        Test-OfflinePackageHashes -Root $PackageRoot
    }
    $depsDir = Join-Path $PackageRoot "deps"
    $appDir = Join-Path $PackageRoot "app"
    if (-not $SkipVcRedist) {
        if (-not (Install-VcRedist -Path (Join-Path $depsDir "vc_redist.x64.exe"))) {
            throw "VC++ redistributable installation failed"
        }
    }
    $targetBle = Join-Path $env:LOCALAPPDATA "Programs\nrfconnect-bluetooth-low-energy"
    if (-not $SkipNrfConnect) {
        Install-NrfConnectBlePortable -SourceDir (Join-Path $depsDir "nrfconnect-bluetooth-low-energy") -TargetDir $targetBle | Out-Null
        Write-OfflineLog "nRF Connect BLE ready at $targetBle"
    }
    if (-not $SkipJLink) {
        $jlinkInstaller = Join-Path $depsDir "segger-jlink-installer.exe"
        if (Test-Path -LiteralPath $jlinkInstaller) {
            Install-ExeDependency -Name "SEGGER J-Link" -Path $jlinkInstaller -Arguments @("/S") | Out-Null
        } else {
            Write-OfflineLog "Standalone SEGGER J-Link installer not bundled; Nordic Command Line Tools installer is expected to provide the required J-Link runtime."
        }
    }
    if (-not $SkipNordicCli) {
        Install-ExeDependency -Name "Nordic Command Line Tools" -Path (Join-Path $depsDir "nordic-command-line-tools-installer.exe") -Arguments @("/S") | Out-Null
    }
    if (-not $SkipWorkstation) {
        $setupExe = Find-SetupExe -AppDir $appDir
        Write-OfflineLog "Launching workstation setup: $setupExe"
        if ($InstallRoot) {
            $env:AXI_FACTORY_INSTALL_DIR = $InstallRoot
            $env:AXI_FACTORY_INSTALL_ROOT = $InstallRoot
        }
        $proc = Start-Process -FilePath $setupExe -Wait -PassThru
        Write-OfflineLog ("Workstation setup exit code: {0}" -f $proc.ExitCode)
        if ($proc.ExitCode -ne 0) { throw "Workstation setup failed with exit code $($proc.ExitCode)" }
    }
    $installedFirmware = Join-Path $InstallRoot "firmware\axi_p1_factory_merged.hex"
    if (-not $SkipFirmware) {
        Set-WorkstationDefaultFirmware `
            -InstallRoot $InstallRoot `
            -FirmwareSource (Join-Path $PackageRoot "firmware\axi_p1_factory_merged.hex") `
            -NrfjprogPath (Find-Nrfjprog) `
            -NrfConnectBlePath $targetBle
    }
    if (-not $SkipWorkstation -and -not $SkipNrfConnect -and -not $SkipNordicCli -and -not $SkipFirmware) {
        Test-InstalledTools -InstallRoot $InstallRoot -NrfConnectBlePath $targetBle -FirmwarePath $installedFirmware
    }
    Show-OfflineMessage -Title "Axi Factory Workstation Offline Setup" -Message ("Offline dependency install completed.`n`nLog:`n{0}" -f $script:LogPath)
    Write-OfflineLog "Offline install completed"
} catch {
    $message = $_.Exception.Message
    Write-OfflineLog "ERROR: $message"
    Show-OfflineMessage -Title "Axi Factory Workstation Offline Setup Failed" -Message ("Offline install failed.`n`nError:`n{0}`n`nLog:`n{1}" -f $message, $script:LogPath)
    exit 1
}
