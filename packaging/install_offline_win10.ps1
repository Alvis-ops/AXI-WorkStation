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
    [switch]$ForceReinstall,
    [switch]$Quiet
)

$ErrorActionPreference = "Stop"
$script:AppName = "Axi Factory Workstation"
$script:ExeName = "Axi Factory Workstation.exe"
$script:CliExeName = "Axi Factory Workstation CLI.exe"
$script:OtaHelperExeName = "Axi OTA Helper.exe"
$script:FirmwareLeaf = "axi_p1_factory_merged.hex"
$script:OtaFirmwareLeaf = "zephyr.signed.bin"
$script:LogPath = Join-Path ([System.IO.Path]::GetTempPath()) "AxiFactoryWorkstation_offline_install.log"

if (-not $PackageRoot) {
    $PackageRoot = Split-Path -Parent $PSCommandPath
}
if (-not $InstallRoot) {
    $InstallRoot = Join-Path $env:LOCALAPPDATA "Programs\$($script:AppName)"
}

$sharedScript = Join-Path $PackageRoot "shared\offline_jlink_env.ps1"
if (-not (Test-Path -LiteralPath $sharedScript)) {
    $sharedScript = Join-Path (Split-Path $PSScriptRoot -Parent) "shared\offline_jlink_env.ps1"
}
if (-not (Test-Path -LiteralPath $sharedScript)) {
    throw "Shared offline environment script not found: $sharedScript"
}
. $sharedScript
Initialize-OfflineJLinkEnv -RequiredVersion "7.94e"

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
    if (Test-VcRedistInstalled) {
        Write-OfflineLog "VC++ redistributable already installed; skipping"
        return $true
    }
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
    if (Test-NrfConnectBleReady -TargetDir $TargetDir) {
        Write-OfflineLog "nRF Connect BLE already present; skipping copy: $TargetDir"
        return $true
    }
    if (-not (Test-Path -LiteralPath $SourceDir)) { throw "nRF Connect BLE bundle missing: $SourceDir" }
    $exeName = "nRF Connect for Desktop Bluetooth Low Energy.exe"
    if (-not (Test-Path -LiteralPath (Join-Path $SourceDir $exeName))) { throw "Invalid nRF Connect BLE bundle; missing $exeName" }
    Write-OfflineLog "Deploying nRF Connect BLE to $TargetDir"
    New-Item -ItemType Directory -Path $TargetDir -Force | Out-Null
    & robocopy $SourceDir $TargetDir /MIR /NFL /NDL /NJH /NJS /NP | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "Failed copying nRF Connect BLE (robocopy exit $LASTEXITCODE)" }
    return $true
}

function Set-WorkstationDefaultFirmware {
    param(
        [string]$InstallRoot,
        [string]$FirmwareSource,
        [string]$OtaFirmwareSource,
        [string]$NrfjprogPath,
        [string]$JLinkDllPath,
        [string]$NrfConnectBlePath
    )
    if (-not (Test-Path -LiteralPath $FirmwareSource)) {
        throw "Default firmware hex missing: $FirmwareSource"
    }
    $firmwareTargetDir = Join-Path $InstallRoot "firmware"
    New-Item -ItemType Directory -Path $firmwareTargetDir -Force | Out-Null
    $firmwareTarget = Join-Path $firmwareTargetDir $script:FirmwareLeaf
    Copy-Item -LiteralPath $FirmwareSource -Destination $firmwareTarget -Force
    if (-not (Test-Path -LiteralPath $OtaFirmwareSource)) {
        throw "Default OTA image missing: $OtaFirmwareSource"
    }
    $otaFirmwareTarget = Join-Path $firmwareTargetDir $script:OtaFirmwareLeaf
    Copy-Item -LiteralPath $OtaFirmwareSource -Destination $otaFirmwareTarget -Force

    $configPath = Join-Path $InstallRoot "config.json"
    Update-WorkstationFlashConfig -ConfigPath $configPath -PreserveExistingUserSettings -ReplaceMissingFilePathSettings @("firmware_repo", "flash_script_path", "flash_image_path", "half_flash_image_path", "ota_image_path") -Values @{
        half_flash_before_test = $false
        firmware_repo = "."
        flash_script_path = "flash_selected_image.ps1"
        flash_backend = "nrfjprog"
        flash_image_path = "firmware\$($script:FirmwareLeaf)"
        half_flash_image_path = "firmware\$($script:FirmwareLeaf)"
        flash_after_wait_s = 8.0
        flash_timeout_s = 180.0
        flash_verify = $true
        nrfjprog_path = $(if ($NrfjprogPath) { $NrfjprogPath } else { "nrfjprog" })
        jlink_dll_path = $JLinkDllPath
        record_output_mode = "unified"
        nrf_connect_ble_path = $NrfConnectBlePath
        ota_image_path = "firmware\$($script:OtaFirmwareLeaf)"
    }
    Write-OfflineLog "Configured default flash and OTA images: $firmwareTarget ; $otaFirmwareTarget"
}

function Test-InstalledTools {
    param(
        [string]$InstallRoot,
        [string]$NrfConnectBlePath,
        [string]$FirmwarePath,
        [string]$OtaFirmwarePath,
        [string]$NrfjprogPath,
        [string]$JLinkDllPath
    )
    Test-FlashEnvironmentReady -NrfjprogPath $NrfjprogPath -JLinkDllPath $JLinkDllPath -LogCallback ${function:Write-OfflineLog}

    $bleExe = Join-Path $NrfConnectBlePath "nRF Connect for Desktop Bluetooth Low Energy.exe"
    if (-not (Test-Path -LiteralPath $bleExe)) { throw "nRF Connect BLE backend missing: $bleExe" }
    Write-OfflineLog "nRF Connect BLE backend found: $bleExe"

    if (-not (Test-Path -LiteralPath $FirmwarePath)) { throw "Default firmware hex missing after install: $FirmwarePath" }
    Write-OfflineLog "Default firmware found: $FirmwarePath"
    if (-not (Test-Path -LiteralPath $OtaFirmwarePath)) { throw "Default OTA image missing after install: $OtaFirmwarePath" }
    Write-OfflineLog "Default OTA image found: $OtaFirmwarePath"

    foreach ($exeName in @($script:ExeName, $script:CliExeName, $script:OtaHelperExeName)) {
        $exePath = Join-Path $InstallRoot $exeName
        if (-not (Test-Path -LiteralPath $exePath)) { throw "Workstation executable missing after install: $exePath" }
        Write-OfflineLog "Workstation executable found: $exePath"
    }
}

function Find-SetupExe {
    param([string]$AppDir)
    $matches = Get-ChildItem -LiteralPath $AppDir -Filter "Axi_Factory_Workstation_Setup_*_win10_x64*.exe" | Sort-Object LastWriteTime -Descending
    if ($matches.Count -eq 0) { throw "Workstation setup exe not found under $AppDir" }
    return $matches[0].FullName
}

try {
    if (Test-Path -LiteralPath $script:LogPath) {
        Remove-Item -LiteralPath $script:LogPath -Force
    }
    Write-OfflineLog "Offline install started. PackageRoot=$PackageRoot InstallRoot=$InstallRoot"
    if (-not $SkipHashCheck) {
        Test-OfflinePackageHashes -Root $PackageRoot
    }
    $depsDir = Join-Path $PackageRoot "deps"
    $appDir = Join-Path $PackageRoot "app"
    $targetBle = Join-Path $env:LOCALAPPDATA "Programs\nrfconnect-bluetooth-low-energy"
    $configPath = Join-Path $InstallRoot "config.json"
    $preferredJLinkDll = Get-ConfigJLinkDllPath -ConfigPath $configPath

    if (-not $SkipVcRedist) {
        if (-not (Install-VcRedist -Path (Join-Path $depsDir "vc_redist.x64.exe"))) {
            throw "VC++ redistributable installation failed"
        }
    }
    if (-not $SkipNrfConnect) {
        Install-NrfConnectBlePortable -SourceDir (Join-Path $depsDir "nrfconnect-bluetooth-low-energy") -TargetDir $targetBle | Out-Null
        Write-OfflineLog "nRF Connect BLE ready at $targetBle"
    }

    $jlinkInstallation = $null
    if (-not $SkipJLink -and -not $SkipNordicCli) {
        $nordicInstaller = Join-Path $depsDir "nordic-command-line-tools-installer.exe"
        $jlinkInstallation = Ensure-JLinkStack `
            -NordicInstallerPath $nordicInstaller `
            -PreferredJLinkDllPath $preferredJLinkDll `
            -LogCallback ${function:Write-OfflineLog}
    }

    $nrfjprogPath = Find-Nrfjprog
    if (-not $SkipWorkstation) {
        $exePath = Join-Path $InstallRoot $script:ExeName
        if ((Test-Path -LiteralPath $exePath) -and -not $ForceReinstall) {
            Write-OfflineLog "Workstation already installed; skipping setup exe: $exePath"
        } else {
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
    }

    $installedFirmware = Join-Path $InstallRoot ("firmware\{0}" -f $script:FirmwareLeaf)
    $installedOtaFirmware = Join-Path $InstallRoot ("firmware\{0}" -f $script:OtaFirmwareLeaf)
    if (-not $SkipFirmware) {
        Set-WorkstationDefaultFirmware `
            -InstallRoot $InstallRoot `
            -FirmwareSource (Join-Path $PackageRoot ("firmware\{0}" -f $script:FirmwareLeaf)) `
            -OtaFirmwareSource (Join-Path $PackageRoot ("firmware\{0}" -f $script:OtaFirmwareLeaf)) `
            -NrfjprogPath $nrfjprogPath `
            -JLinkDllPath $(if ($jlinkInstallation) { $jlinkInstallation.DllPath } else { $preferredJLinkDll }) `
            -NrfConnectBlePath $targetBle
    }

    if (-not $SkipWorkstation -and -not $SkipJLink -and -not $SkipNordicCli -and -not $SkipFirmware) {
        $finalJLinkDll = if ($jlinkInstallation) { $jlinkInstallation.DllPath } else { Get-ConfigJLinkDllPath -ConfigPath $configPath }
        Test-InstalledTools `
            -InstallRoot $InstallRoot `
            -NrfConnectBlePath $targetBle `
            -FirmwarePath $installedFirmware `
            -OtaFirmwarePath $installedOtaFirmware `
            -NrfjprogPath $nrfjprogPath `
            -JLinkDllPath $finalJLinkDll
    }

    New-Item -ItemType Directory -Path $InstallRoot -Force | Out-Null
    $installNote = Join-Path $InstallRoot "INSTALL_README.txt"
    @(
        "$($script:AppName) offline install completed.",
        "",
        "Install folder:",
        $InstallRoot,
        "",
        "Next steps:",
        "1. Connect J-Link USB and factory dongle/UART as needed.",
        "2. Verify probe with the workstation's 烧录检测 button.",
        "3. Configure engineering credentials and COM ports in the GUI.",
        "4. The workstation is pinned to Nordic-compatible J-Link 7.94e via nrfjprog --jdll.",
        "5. If another workstation already installed the flash environment, dependency install was skipped automatically.",
        "",
        "Install log:",
        $script:LogPath
    ) | Set-Content -LiteralPath $installNote -Encoding UTF8
    Write-OfflineLog "Wrote install note: $installNote"

    Show-OfflineMessage -Title "$($script:AppName) Offline Setup" -Message ("Offline install completed.`n`nInstall path:`n{0}`n`nLog:`n{1}" -f $InstallRoot, $script:LogPath)
    Write-OfflineLog "Offline install completed"
} catch {
    $message = $_.Exception.Message
    Write-OfflineLog "ERROR: $message"
    Show-OfflineMessage -Title "$($script:AppName) Offline Setup Failed" -Message ("Offline install failed.`n`nError:`n{0}`n`nLog:`n{1}" -f $message, $script:LogPath)
    exit 1
}
