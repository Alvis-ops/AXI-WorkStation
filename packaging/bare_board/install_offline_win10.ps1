# Offline install orchestrator for Win10 x64 bare board workstation package.
param(
    [string]$PackageRoot = "",
    [string]$InstallRoot = "",
    [switch]$SkipVcRedist,
    [switch]$SkipJLink,
    [switch]$SkipNordicCli,
    [switch]$SkipFirmware,
    [switch]$SkipWorkstation,
    [switch]$SkipHashCheck,
    [switch]$ForceReinstall,
    [switch]$Quiet
)

$ErrorActionPreference = "Stop"
$script:AppName = "Axi Bare Board Workstation"
$script:ExeName = "Axi Bare Board Workstation.exe"
$script:FirmwareLeaf = "poc3a_factory_merged.hex"
$script:LogPath = Join-Path ([System.IO.Path]::GetTempPath()) "AxiBareBoardWorkstation_offline_install.log"

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

function Set-WorkstationDefaultFirmware {
    param(
        [string]$InstallRoot,
        [string]$FirmwareSource,
        [string]$NrfjprogPath,
        [string]$JLinkDllPath
    )
    if (-not (Test-Path -LiteralPath $FirmwareSource)) {
        throw "Default firmware hex missing: $FirmwareSource"
    }
    $firmwareTargetDir = Join-Path $InstallRoot "firmware"
    New-Item -ItemType Directory -Path $firmwareTargetDir -Force | Out-Null
    $firmwareTarget = Join-Path $firmwareTargetDir $script:FirmwareLeaf
    Copy-Item -LiteralPath $FirmwareSource -Destination $firmwareTarget -Force

    $configPath = Join-Path $InstallRoot "config.json"
    Update-WorkstationFlashConfig -ConfigPath $configPath -PreserveExistingUserSettings -Values @{
        flash_backend = "nrfjprog"
        flash_image_path = "firmware\$($script:FirmwareLeaf)"
        flash_after_wait_s = 2.0
        flash_timeout_s = 180.0
        flash_verify = $false
        nrfjprog_path = $(if ($NrfjprogPath) { $NrfjprogPath } else { "nrfjprog" })
        jlink_dll_path = $JLinkDllPath
        nrfjprog_family = ""
        serial_baudrate = 115200
        serial_open_wait_s = 0.0
        start_prompt_patterns = @()
        start_prompt_timeout_s = 0.0
        require_start_prompt = $false
        test_start_command = "AT+DRVTEST"
        records_root = "bare_board_records"
    }
    Write-OfflineLog "Configured default firmware and flash settings: $firmwareTarget"
}

function Test-InstalledTools {
    param(
        [string]$InstallRoot,
        [string]$FirmwarePath,
        [string]$NrfjprogPath,
        [string]$JLinkDllPath
    )
    Test-FlashEnvironmentReady -NrfjprogPath $NrfjprogPath -JLinkDllPath $JLinkDllPath -LogCallback ${function:Write-OfflineLog}

    if (-not (Test-Path -LiteralPath $FirmwarePath)) { throw "Default firmware hex missing after install: $FirmwarePath" }
    Write-OfflineLog "Default firmware found: $FirmwarePath"

    $exePath = Join-Path $InstallRoot $script:ExeName
    if (-not (Test-Path -LiteralPath $exePath)) { throw "Workstation exe missing after install: $exePath" }
    Write-OfflineLog "Workstation exe found: $exePath"
}

function Find-SetupExe {
    param([string]$AppDir)
    $matches = Get-ChildItem -LiteralPath $AppDir -Filter "Axi_Bare_Board_Workstation_Setup_*_win10_x64*.exe" | Sort-Object LastWriteTime -Descending
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
    $configPath = Join-Path $InstallRoot "config.json"
    $preferredJLinkDll = Get-ConfigJLinkDllPath -ConfigPath $configPath

    if (-not $SkipVcRedist) {
        if (-not (Install-VcRedist -Path (Join-Path $depsDir "vc_redist.x64.exe"))) {
            throw "VC++ redistributable installation failed"
        }
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
                $env:AXI_BARE_BOARD_INSTALL_DIR = $InstallRoot
            }
            $env:AXI_BARE_BOARD_NO_PATH_DIALOG = "1"
            $proc = Start-Process -FilePath $setupExe -Wait -PassThru
            Write-OfflineLog ("Workstation setup exit code: {0}" -f $proc.ExitCode)
            if ($proc.ExitCode -ne 0) { throw "Workstation setup failed with exit code $($proc.ExitCode)" }
        }
    }

    $installedFirmware = Join-Path $InstallRoot ("firmware\{0}" -f $script:FirmwareLeaf)
    if (-not $SkipFirmware) {
        Set-WorkstationDefaultFirmware `
            -InstallRoot $InstallRoot `
            -FirmwareSource (Join-Path $PackageRoot ("firmware\{0}" -f $script:FirmwareLeaf)) `
            -NrfjprogPath $nrfjprogPath `
            -JLinkDllPath $(if ($jlinkInstallation) { $jlinkInstallation.DllPath } else { $preferredJLinkDll })
        $finalJLinkDll = if ($jlinkInstallation) { $jlinkInstallation.DllPath } else { Get-ConfigJLinkDllPath -ConfigPath $configPath }
        $probeIds = Get-NrfjprogProbeIds -NrfjprogPath $nrfjprogPath -JLinkDllPath $finalJLinkDll
        if ($probeIds.Count -eq 1) {
            Update-WorkstationFlashConfig -ConfigPath $configPath -Values @{ jlink_probe_id = $probeIds[0] }
            Write-OfflineLog "Configured J-Link probe ID: $($probeIds[0])"
        } elseif ($probeIds.Count -gt 1) {
            Write-OfflineLog "NOTE: Multiple J-Link probes detected ($($probeIds -join ', ')); set jlink_probe_id in the GUI before flashing."
        }
    }

    if (-not $SkipWorkstation -and -not $SkipJLink -and -not $SkipNordicCli -and -not $SkipFirmware) {
        $finalJLinkDll = if ($jlinkInstallation) { $jlinkInstallation.DllPath } else { Get-ConfigJLinkDllPath -ConfigPath $configPath }
        Test-InstalledTools `
            -InstallRoot $InstallRoot `
            -FirmwarePath $installedFirmware `
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
        "1. Connect J-Link USB and bare board UART.",
        "2. Verify probe with the workstation's 检测烧录器 button.",
        "3. Open the workstation and set the correct COM port (115200).",
        "4. The workstation is pinned to Nordic-compatible J-Link 7.94e via nrfjprog --jdll.",
        "5. If another workstation already installed the flash environment, dependency install was skipped automatically.",
        "",
        "Install log:",
        $script:LogPath
    ) | Set-Content -LiteralPath $installNote -Encoding UTF8
    Write-OfflineLog "Wrote install note: $installNote"

    Show-OfflineMessage -Title "$($script:AppName) Offline Setup" -Message ("Offline install completed.`n`nInstall path:`n{0}`n`nSet COM port in the GUI before testing.`n`nLog:`n{1}" -f $InstallRoot, $script:LogPath)
    Write-OfflineLog "Offline install completed"
} catch {
    $message = $_.Exception.Message
    Write-OfflineLog "ERROR: $message"
    Show-OfflineMessage -Title "$($script:AppName) Offline Setup Failed" -Message ("Offline install failed.`n`nError:`n{0}`n`nLog:`n{1}" -f $message, $script:LogPath)
    exit 1
}
