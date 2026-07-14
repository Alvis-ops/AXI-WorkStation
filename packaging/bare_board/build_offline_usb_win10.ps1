# Build a Win10 x64 offline USB delivery folder for Axi Bare Board Workstation.
param(
    [string]$AppDir = "",
    [string]$VcRedistPath = "",
    [string]$NordicCliInstallerPath = "",
    [string]$FirmwareHexPath = "",
    [string]$OutputDir = "",
    [string]$PackageRevision = "r16",
    [switch]$DownloadVcRedist,
    [switch]$SkipNordicCli,
    [switch]$SkipFirmware,
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$distRoot = Join-Path $repoRoot "dist"
$dateStamp = Get-Date -Format "yyyyMMdd"
$appName = "Axi Bare Board Workstation"
$exeName = "Axi Bare Board Workstation.exe"
$payloadZipLeaf = "Axi_Bare_Board_Workstation_payload.zip"
$firmwareLeaf = "poc3a_factory_merged.hex"

function Get-FileSha256 {
    param([string]$Path)
    (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Test-WindowsExecutable {
    param([string]$Path)
    if (-not $Path -or -not (Test-Path -LiteralPath $Path)) { return $false }
    try {
        $bytes = Get-Content -LiteralPath $Path -Encoding Byte -TotalCount 2
        return ($bytes.Count -eq 2 -and $bytes[0] -eq 0x4D -and $bytes[1] -eq 0x5A)
    } catch {
        return $false
    }
}

function Resolve-InstallerPath {
    param([string]$Path, [string]$Name)
    if (-not $Path -or -not (Test-Path -LiteralPath $Path)) { return "" }
    $resolved = (Resolve-Path -LiteralPath $Path).Path
    if (-not (Test-WindowsExecutable -Path $resolved)) {
        throw "$Name is not a valid Windows installer exe: $resolved"
    }
    return $resolved
}

function Write-Utf8NoBom {
    param([string]$Path, [string[]]$Lines)
    $enc = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllLines($Path, $Lines, $enc)
}

function Write-Utf8NoBomText {
    param([string]$Path, [string]$Text)
    $enc = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Text, $enc)
}

function Remove-DirectoryIfAllowed {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return }
    $resolvedPath = [System.IO.Path]::GetFullPath($Path)
    $resolvedDist = [System.IO.Path]::GetFullPath($distRoot)
    if (-not $resolvedPath.StartsWith($resolvedDist, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to delete output outside dist root: $resolvedPath"
    }
    Remove-Item -LiteralPath $resolvedPath -Recurse -Force
}

function Find-LocalVcRedist {
    if ($VcRedistPath -and (Test-Path -LiteralPath $VcRedistPath)) {
        return (Resolve-Path -LiteralPath $VcRedistPath).Path
    }
    $matches = @(Get-ChildItem -LiteralPath $distRoot -Recurse -Filter "vc_redist.x64.exe" -File -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending)
    if ($matches.Count -gt 0) {
        return $matches[0].FullName
    }
    return ""
}

function Find-FirstFile {
    param([string[]]$Roots, [string[]]$Patterns, [switch]$RequireWindowsExecutable)
    foreach ($root in $Roots) {
        if (-not $root -or -not (Test-Path -LiteralPath $root)) { continue }
        foreach ($pattern in $Patterns) {
            $matches = @(Get-ChildItem -LiteralPath $root -Recurse -Filter $pattern -File -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending)
            foreach ($match in $matches) {
                if ($RequireWindowsExecutable -and -not (Test-WindowsExecutable -Path $match.FullName)) {
                    Write-Warning "Ignoring invalid exe candidate: $($match.FullName)"
                    continue
                }
                return $match.FullName
            }
        }
    }
    return ""
}

function Find-LocalNordicCliInstaller {
    if ($NordicCliInstallerPath -and (Test-Path -LiteralPath $NordicCliInstallerPath)) {
        return Resolve-InstallerPath -Path $NordicCliInstallerPath -Name "Nordic Command Line Tools installer"
    }
    return Find-FirstFile -Roots @($distRoot, $repoRoot) -Patterns @("nrf-command-line-tools*.exe", "nRF-Command-Line-Tools*.exe", "nordic-command-line-tools*.exe") -RequireWindowsExecutable
}

function Find-LocalFirmwareHex {
    if ($FirmwareHexPath -and (Test-Path -LiteralPath $FirmwareHexPath)) {
        return (Resolve-Path -LiteralPath $FirmwareHexPath).Path
    }
    $workspaceRoot = Split-Path -Parent $repoRoot
    foreach ($root in @($workspaceRoot, $repoRoot)) {
        if (-not $root -or -not (Test-Path -LiteralPath $root)) { continue }
        $direct = Join-Path $root "POC3\build_factory\merged.hex"
        if (Test-Path -LiteralPath $direct) {
            return (Resolve-Path -LiteralPath $direct).Path
        }
    }
    return Find-FirstFile -Roots @($distRoot, $repoRoot, $workspaceRoot) -Patterns @("merged.hex", "poc3a_factory_merged.hex")
}

function New-OfflineConfig {
    param([string]$Path)
    $config = [ordered]@{
        flash_backend = "nrfjprog"
        flash_image_path = "firmware\$firmwareLeaf"
        nrfjprog_path = "nrfjprog"
        jlink_dll_path = ""
        nrfjprog_family = ""
        jlink_probe_id = ""
        flash_verify = $false
        flash_timeout_s = 180.0
        flash_after_wait_s = 2.0
        serial_port = ""
        serial_baudrate = 115200
        serial_timeout_s = 90.0
        serial_open_wait_s = 0.0
        start_prompt_patterns = @()
        start_prompt_timeout_s = 0.0
        require_start_prompt = $false
        test_start_command = "AT+DRVTEST"
        pass_patterns = @("re:\[DRVTEST\]\[FINAL\].*overall=PASS")
        fail_patterns = @("re:\[DRVTEST\]\[FINAL\].*overall=FAIL", "re:\[DRVTEST\]\[ABORT\]")
        end_patterns = @("re:\[DRVTEST\]\[FINAL\]")
        records_root = "bare_board_records"
        station_id = "BARE"
        sn_record_enabled = $true
        sn_rule = [ordered]@{
            min_len = 1
            max_len = 48
            prefix = ""
            regex = "^[A-Za-z0-9_-]+$"
        }
    }
    $json = $config | ConvertTo-Json -Depth 8
    Write-Utf8NoBomText -Path $Path -Text $json
}

function New-WorkstationPayload {
    param(
        [string]$SourceAppDir,
        [string]$PayloadZip
    )

    if (-not (Test-Path -LiteralPath (Join-Path $SourceAppDir $exeName))) {
        throw "Workstation app folder is invalid: $SourceAppDir"
    }

    $stageRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("AxiBareBoardPayload_" + [System.Guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $stageRoot -Force | Out-Null
    try {
        Get-ChildItem -LiteralPath $SourceAppDir -Force | ForEach-Object {
            if ($_.Name -eq "bare_board_records") { return }
            Copy-Item -LiteralPath $_.FullName -Destination $stageRoot -Recurse -Force
        }
        $stageConfig = Join-Path $stageRoot "config.json"
        New-OfflineConfig -Path $stageConfig
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $payloadParent = Split-Path -Parent $PayloadZip
        $tempZip = Join-Path $payloadParent ("payload_" + [System.Guid]::NewGuid().ToString("N") + ".zip")
        if (Test-Path -LiteralPath $PayloadZip) {
            Remove-Item -LiteralPath $PayloadZip -Force
        }
        [System.IO.Compression.ZipFile]::CreateFromDirectory($stageRoot, $tempZip, [System.IO.Compression.CompressionLevel]::Optimal, $false)
        $copied = $false
        for ($attempt = 1; $attempt -le 5; $attempt++) {
            try {
                [System.IO.File]::Copy($tempZip, $PayloadZip, $true)
                $copied = $true
                break
            } catch {
                if ($attempt -eq 5) { throw }
                Start-Sleep -Milliseconds 500
            }
        }
        if (-not $copied -or -not (Test-Path -LiteralPath $PayloadZip)) {
            throw "Failed to create payload zip: $PayloadZip"
        }
        Remove-Item -LiteralPath $tempZip -Force -ErrorAction SilentlyContinue
    } finally {
        Remove-Item -LiteralPath $stageRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}

function New-WorkstationSetupExe {
    param(
        [string]$InstallerPayloadDir,
        [string]$TargetExe
    )

    $iexpress = Join-Path $env:SystemRoot "System32\iexpress.exe"
    if (-not (Test-Path -LiteralPath $iexpress)) {
        throw "iexpress.exe not found: $iexpress"
    }

    Copy-Item -LiteralPath (Join-Path $PSScriptRoot "install_win10.cmd") -Destination (Join-Path $InstallerPayloadDir "install.cmd") -Force
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot "install_win10.ps1") -Destination (Join-Path $InstallerPayloadDir "install.ps1") -Force

    $sedPath = Join-Path $InstallerPayloadDir "AxiBareBoardWorkstation.iexpress.sed"
    $installerPayloadDirWithSlash = $InstallerPayloadDir.TrimEnd('\') + "\"
    Write-Utf8NoBom -Path $sedPath -Lines @(
        "[Version]",
        "Class=IEXPRESS",
        "SEDVersion=3",
        "",
        "[Options]",
        "PackagePurpose=InstallApp",
        "ShowInstallProgramWindow=1",
        "HideExtractAnimation=0",
        "UseLongFileName=1",
        "InsideCompressed=0",
        "CAB_FixedSize=0",
        "CAB_ResvCodeSigning=0",
        "RebootMode=N",
        "InstallPrompt=%InstallPrompt%",
        "DisplayLicense=%DisplayLicense%",
        "FinishMessage=%FinishMessage%",
        "TargetName=%TargetName%",
        "FriendlyName=%FriendlyName%",
        "AppLaunched=install.cmd",
        "PostInstallCmd=<None>",
        "AdminQuietInstCmd=install.cmd /quiet",
        "UserQuietInstCmd=install.cmd /quiet",
        "SourceFiles=SourceFiles",
        "",
        "[Strings]",
        "InstallPrompt=",
        "DisplayLicense=",
        "FinishMessage=Axi Bare Board Workstation setup finished.",
        "TargetName=$TargetExe",
        "FriendlyName=Axi Bare Board Workstation Setup",
        "",
        "[SourceFiles]",
        "SourceFiles0=$installerPayloadDirWithSlash",
        "",
        "[SourceFiles0]",
        "install.cmd=",
        "install.ps1=",
        "$payloadZipLeaf="
    )

    if (Test-Path -LiteralPath $TargetExe) {
        Remove-Item -LiteralPath $TargetExe -Force
    }
    $proc = Start-Process -FilePath $iexpress -ArgumentList @("/N", "/Q", $sedPath) -Wait -PassThru
    if ($proc.ExitCode -ne 0) {
        throw "iexpress failed with exit code $($proc.ExitCode)"
    }
    if (-not (Test-Path -LiteralPath $TargetExe)) {
        throw "Setup exe was not created: $TargetExe"
    }
}

if (-not $AppDir) {
    $AppDir = Join-Path $distRoot $appName
}
if (-not (Test-Path -LiteralPath $AppDir)) {
    throw "Workstation app dir not found: $AppDir. Run: pyinstaller `"Axi Bare Board Workstation.spec`""
}

if (-not $OutputDir) {
    $suffix = if ($PackageRevision) { "_$PackageRevision" } else { "" }
    $OutputDir = Join-Path $distRoot ("AxiBareBoardWorkstation_win10_x64_offline_usb_{0}{1}" -f $dateStamp, $suffix)
}

if ((Test-Path -LiteralPath $OutputDir) -and -not $Force) {
    throw "Output directory already exists: $OutputDir. Use -Force."
}
Remove-DirectoryIfAllowed -Path $OutputDir

$appDirOut = Join-Path $OutputDir "app"
$depsDir = Join-Path $OutputDir "deps"
$firmwareDir = Join-Path $OutputDir "firmware"
$installerPayloadDir = Join-Path $distRoot "bare_board_installer_payload"
New-Item -ItemType Directory -Path $appDirOut, $depsDir, $firmwareDir, $installerPayloadDir -Force | Out-Null
Get-ChildItem -LiteralPath $installerPayloadDir -Filter "payload_*.zip" -File -ErrorAction SilentlyContinue | Remove-Item -Force

$payloadZip = Join-Path $installerPayloadDir $payloadZipLeaf
Write-Host "Building payload zip from: $AppDir"
New-WorkstationPayload -SourceAppDir $AppDir -PayloadZip $payloadZip

$setupLeaf = "Axi_Bare_Board_Workstation_Setup_${dateStamp}_win10_x64_${PackageRevision}.exe"
$setupExe = Join-Path $distRoot $setupLeaf
Write-Host "Building setup exe: $setupExe"
New-WorkstationSetupExe -InstallerPayloadDir $installerPayloadDir -TargetExe $setupExe

Copy-Item -LiteralPath $setupExe -Destination (Join-Path $appDirOut $setupLeaf) -Force
Copy-Item -LiteralPath $payloadZip -Destination (Join-Path $appDirOut $payloadZipLeaf) -Force

$vcRedistDest = Join-Path $depsDir "vc_redist.x64.exe"
if ($DownloadVcRedist) {
    $vcUrl = "https://aka.ms/vs/17/release/vc_redist.x64.exe"
    Write-Host "Downloading VC++ redistributable ..."
    Invoke-WebRequest -Uri $vcUrl -OutFile $vcRedistDest -UseBasicParsing
} else {
    $localVcRedist = Find-LocalVcRedist
    if ($localVcRedist) {
        Write-Host "Copying VC++ redistributable: $localVcRedist"
        Copy-Item -LiteralPath $localVcRedist -Destination $vcRedistDest -Force
    }
}

$nordicCliInstallerDest = Join-Path $depsDir "nordic-command-line-tools-installer.exe"
if (-not $SkipNordicCli) {
    $localNordicCliInstaller = Find-LocalNordicCliInstaller
    if ($localNordicCliInstaller) {
        Write-Host "Copying Nordic Command Line Tools installer: $localNordicCliInstaller"
        Copy-Item -LiteralPath $localNordicCliInstaller -Destination $nordicCliInstallerDest -Force
    } else {
        Write-Warning "Nordic Command Line Tools installer not found."
    }
}

$firmwareHexDest = Join-Path $firmwareDir $firmwareLeaf
if (-not $SkipFirmware) {
    $localFirmwareHex = Find-LocalFirmwareHex
    if ($localFirmwareHex) {
        Write-Host "Copying default firmware hex: $localFirmwareHex"
        Copy-Item -LiteralPath $localFirmwareHex -Destination $firmwareHexDest -Force
    } else {
        Write-Warning "Default firmware merged.hex not found."
    }
}

Copy-Item -LiteralPath (Join-Path $PSScriptRoot "install_offline_win10.ps1") -Destination (Join-Path $OutputDir "install_offline_win10.ps1") -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot "install_offline_win10.cmd") -Destination (Join-Path $OutputDir "install_offline_win10.cmd") -Force
$sharedOutputDir = Join-Path $OutputDir "shared"
New-Item -ItemType Directory -Path $sharedOutputDir -Force | Out-Null
Copy-Item -LiteralPath (Join-Path (Split-Path $PSScriptRoot -Parent) "shared\offline_jlink_env.ps1") -Destination (Join-Path $sharedOutputDir "offline_jlink_env.ps1") -Force

$missingDeps = @()
if (-not (Test-Path -LiteralPath $vcRedistDest)) { $missingDeps += "deps/vc_redist.x64.exe" }
if (-not (Test-Path -LiteralPath $nordicCliInstallerDest)) { $missingDeps += "deps/nordic-command-line-tools-installer.exe" }
if (-not (Test-Path -LiteralPath $firmwareHexDest)) { $missingDeps += "firmware/$firmwareLeaf" }

Write-Utf8NoBom -Path (Join-Path $depsDir "README_DEPS.txt") -Lines @(
    "This folder is complete when MANIFEST.json has missing_deps=[] and SHA256SUMS.txt verifies successfully.",
    "deps/nordic-command-line-tools-installer.exe provides nrfjprog and its validated J-Link 7.94e package.",
    "The offline installer enables bundled SEGGER installation and pins workstation commands with nrfjprog --jdll.",
    "For rebuilds, provide Microsoft VC++ 2015-2022 x64 redistributable as deps\vc_redist.x64.exe.",
    "For rebuilds, provide POC3 factory merged.hex as firmware\$firmwareLeaf."
)

$manifest = [ordered]@{
    package_type = "win10_x64_offline_usb"
    product = "Axi Bare Board Workstation"
    package_revision = $PackageRevision
    built_at_utc = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    setup_exe = "app/$setupLeaf"
    payload_zip = "app/$payloadZipLeaf"
    payload_layout = "root"
    jlink_source = "bundled_with_nordic_command_line_tools"
    jlink_required_version = "7.94e"
    jlink_r12_v956_remediation = "uninstall canonical SEGGER JLink path when DLL version is 9.56"
    jlink_dll_selection = "explicit nrfjprog --jdll path"
    jlink_driver_registration = "InstDrivers.exe /silent with optional pnputil INF fallback"
    shared_install_helpers = "shared/offline_jlink_env.ps1"
    environment_reuse = "skip VC++/Nordic/J-Link when compatible 7.94e stack already exists"
    nordic_command_line_tools_installer = "deps/nordic-command-line-tools-installer.exe"
    nordic_install_skip_bundled_segger = $false
    default_firmware_hex = "firmware/$firmwareLeaf"
    missing_deps = $missingDeps
    complete_offline = ($missingDeps.Count -eq 0)
    install_entry = "install_offline_win10.cmd"
}
$manifestJson = $manifest | ConvertTo-Json -Depth 4
Write-Utf8NoBomText -Path (Join-Path $OutputDir "MANIFEST.json") -Text $manifestJson

Write-Utf8NoBom -Path (Join-Path $OutputDir "INSTALL_FIRST.txt") -Lines @(
    "Axi Bare Board Workstation - Offline Install",
    "",
    "IMPORTANT:",
    "1. Copy this ENTIRE folder to the target PC (do not copy only the app subfolder).",
    "2. Right-click install_offline_win10.cmd and choose Run as administrator.",
    "3. Do NOT only run app\Axi_Bare_Board_Workstation_Setup_*.exe; that skips nrfjprog, VC++, J-Link USB driver, and firmware setup.",
    "4. The installer removes incompatible canonical J-Link 9.56, then reuses or installs validated J-Link 7.94e.",
    "5. If another workstation already installed nrfjprog + J-Link 7.94e, dependency install is skipped automatically.",
    "6. The installer runs USBDriver\InstDrivers.exe /silent; loose INF files are registered with pnputil when available.",
    "7. After install, connect/replug J-Link USB and verify using the workstation's detector.",
    "8. Open the workstation and set the correct COM port (115200).",
    "9. nrfjprog flash warnings about --verify are normal when verify is disabled.",
    "10. If flash says 'No debuggers were discovered', check Device Manager for VID_1366 / PID_0105 and rerun install_offline_win10.cmd as administrator."
)

$missingText = if ($missingDeps.Count -gt 0) { ($missingDeps | ForEach-Object { "- $_" }) -join "`n" } else { "- none" }
$templatePath = Join-Path $PSScriptRoot "README_OFFLINE_WIN10.template.md"
$readmeTemplate = [System.IO.File]::ReadAllText($templatePath, [System.Text.Encoding]::UTF8)
$readme = $readmeTemplate.Replace("{{DATE}}", (Get-Date -Format "yyyy-MM-dd")).
    Replace("{{SETUP_EXE}}", $setupLeaf).
    Replace("{{MISSING_DEPS}}", $missingText)
Write-Utf8NoBomText -Path (Join-Path $OutputDir "README_OFFLINE_WIN10.md") -Text $readme

$shaLines = @()
Get-ChildItem -LiteralPath $OutputDir -Recurse -File | Where-Object { $_.Name -ne "SHA256SUMS.txt" } | ForEach-Object {
    $relative = $_.FullName.Substring($OutputDir.Length).TrimStart('\', '/').Replace('\', '/')
    $shaLines += ("{0}  {1}" -f (Get-FileSha256 $_.FullName), $relative)
}
Write-Utf8NoBom -Path (Join-Path $OutputDir "SHA256SUMS.txt") -Lines ($shaLines | Sort-Object)

$totalSize = (Get-ChildItem -LiteralPath $OutputDir -Recurse -File | Measure-Object -Property Length -Sum).Sum
Write-Host "Offline USB folder ready: $OutputDir"
Write-Host ("Total size: {0:N1} MB" -f ($totalSize / 1MB))
$global:LASTEXITCODE = 0
