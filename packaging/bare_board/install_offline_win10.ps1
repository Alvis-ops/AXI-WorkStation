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
    [switch]$Quiet
)

$ErrorActionPreference = "Stop"
$script:AppName = "Axi Bare Board Workstation"
$script:ExeName = "Axi Bare Board Workstation.exe"
$script:FirmwareLeaf = "poc3a_factory_merged.hex"
$script:RequiredJLinkVersion = "7.94e"

if (-not $PackageRoot) {
    $PackageRoot = Split-Path -Parent $PSCommandPath
}
if (-not $InstallRoot) {
    $InstallRoot = Join-Path $env:LOCALAPPDATA "Programs\$($script:AppName)"
}

$script:LogPath = Join-Path ([System.IO.Path]::GetTempPath()) "AxiBareBoardWorkstation_offline_install.log"

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

function Get-SeggerInstallRoots {
    $roots = @()
    foreach ($programFiles in @(${env:ProgramFiles}, ${env:ProgramFiles(x86)})) {
        if (-not $programFiles) { continue }
        $seggerRoot = Join-Path $programFiles "SEGGER"
        if (-not (Test-Path -LiteralPath $seggerRoot)) { continue }
        $roots += @(Get-ChildItem -LiteralPath $seggerRoot -Directory -Filter "JLink*" -ErrorAction SilentlyContinue |
                ForEach-Object { $_.FullName })
    }
    if ($env:USERPROFILE) {
        $userSeggerRoot = Join-Path $env:USERPROFILE "SEGGER"
        if (Test-Path -LiteralPath $userSeggerRoot) {
            $roots += @(Get-ChildItem -LiteralPath $userSeggerRoot -Directory -Filter "JLink*" -ErrorAction SilentlyContinue |
                    ForEach-Object { $_.FullName })
        }
    }
    $uninstallRoots = @(
        "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
        "HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
        "HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
    )
    foreach ($uninstallRoot in $uninstallRoots) {
        if (-not (Test-Path -LiteralPath $uninstallRoot)) { continue }
        $roots += @(
            Get-ChildItem -LiteralPath $uninstallRoot -ErrorAction SilentlyContinue |
                ForEach-Object { Get-ItemProperty -LiteralPath $_.PSPath -ErrorAction SilentlyContinue } |
                Where-Object {
                    $_.DisplayName -like "*J-Link*" -and
                    $_.InstallLocation -and
                    (Test-Path -LiteralPath $_.InstallLocation)
                } |
                ForEach-Object { $_.InstallLocation }
        )
    }
    return @(
        $roots |
            Where-Object { $_ -and (Test-Path -LiteralPath $_) } |
            ForEach-Object { (Get-Item -LiteralPath $_).FullName } |
            Sort-Object -Unique
    )
}

function Get-SeggerDriverInstallation {
    param([string]$PreferredRoot = "")

    $candidates = @()
    foreach ($root in (Get-SeggerInstallRoots)) {
        $driverHelpers = @(
            Get-ChildItem -LiteralPath $root -Recurse -File -Filter "InstDrivers.exe" -ErrorAction SilentlyContinue
        )
        foreach ($driverHelper in $driverHelpers) {
            $usbDriverRoot = $driverHelper.Directory.FullName
            $infFiles = @(
                Get-ChildItem -LiteralPath $usbDriverRoot -Recurse -File -Filter "*.inf" -ErrorAction SilentlyContinue |
                    Where-Object { $_.Name -like "JLink*" } |
                    Sort-Object FullName
            )
            $candidates += [pscustomobject]@{
                Root = $root
                UsbDriverRoot = $usbDriverRoot
                InstDriversPath = $driverHelper.FullName
                InfFiles = $infFiles
                LastWriteTimeUtc = $driverHelper.LastWriteTimeUtc
            }
        }
    }
    return @(
        $candidates |
            Sort-Object `
                @{ Expression = {
                    $PreferredRoot -and
                    [string]::Equals($_.Root, $PreferredRoot, [System.StringComparison]::OrdinalIgnoreCase)
                }; Descending = $true },
                @{ Expression = { $_.Root -match "\\JLink($|\\)" }; Descending = $true },
                @{ Expression = { $_.LastWriteTimeUtc }; Descending = $true } |
            Select-Object -First 1
    )
}

function Install-JLinkStack {
    param([string]$NrfjprogPath)

    $compatible = Find-CompatibleJLinkInstallation -NrfjprogPath $NrfjprogPath
    if (-not $compatible) {
        throw "Nordic Command Line Tools did not install the required J-Link $($script:RequiredJLinkVersion). Rerun its installer with bundled SEGGER enabled."
    }
    Write-OfflineLog "Pinned Nordic-compatible J-Link $($compatible.Version): $($compatible.DllPath)"
    $driverInstallation = Get-SeggerDriverInstallation -PreferredRoot $compatible.Root
    if (-not $driverInstallation) {
        throw "J-Link $($compatible.Version) was found, but its USBDriver\InstDrivers.exe was not found."
    }
    Install-SeggerUsbDrivers -DriverInstallation $driverInstallation
    return $compatible
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

function ConvertTo-ProcessArgument {
    param([string]$Value)
    if ($Value -notmatch '[\s"]') {
        return $Value
    }
    return '"' + $Value.Replace('"', '\"') + '"'
}

function Invoke-ExternalProcess {
    param(
        [Parameter(Mandatory = $true)]
        [string]$FilePath,
        [string[]]$Arguments = @(),
        [string]$WorkingDirectory = ""
    )

    $tempRoot = [System.IO.Path]::GetTempPath()
    $stdoutPath = Join-Path $tempRoot ("AxiBareBoard_stdout_" + [System.Guid]::NewGuid().ToString("N") + ".log")
    $stderrPath = Join-Path $tempRoot ("AxiBareBoard_stderr_" + [System.Guid]::NewGuid().ToString("N") + ".log")
    try {
        $argumentLine = (($Arguments | ForEach-Object { ConvertTo-ProcessArgument -Value $_ }) -join " ")
        $startArgs = @{
            FilePath = $FilePath
            ArgumentList = $argumentLine
            Wait = $true
            PassThru = $true
            WindowStyle = "Hidden"
            RedirectStandardOutput = $stdoutPath
            RedirectStandardError = $stderrPath
        }
        if ($WorkingDirectory) {
            $startArgs.WorkingDirectory = $WorkingDirectory
        }
        $proc = Start-Process @startArgs
        $stdout = if (Test-Path -LiteralPath $stdoutPath) {
            [System.IO.File]::ReadAllText($stdoutPath)
        } else {
            ""
        }
        $stderr = if (Test-Path -LiteralPath $stderrPath) {
            [System.IO.File]::ReadAllText($stderrPath)
        } else {
            ""
        }
        return [pscustomobject]@{
            ExitCode = $proc.ExitCode
            Output = (@($stdout.Trim(), $stderr.Trim()) | Where-Object { $_ }) -join [Environment]::NewLine
        }
    } finally {
        Remove-Item -LiteralPath $stdoutPath, $stderrPath -Force -ErrorAction SilentlyContinue
    }
}

function Remove-R12JLink956Installation {
    foreach ($root in (Get-SeggerInstallRoots)) {
        if ([System.IO.Path]::GetFileName($root.TrimEnd('\')) -ine "JLink") {
            continue
        }
        $dll = @(
            "JLinkARM.dll",
            "JLink_x64.dll"
        ) |
            ForEach-Object { Join-Path $root $_ } |
            Where-Object { Test-Path -LiteralPath $_ } |
            Select-Object -First 1
        if (-not $dll) { continue }

        $versionInfo = [System.Diagnostics.FileVersionInfo]::GetVersionInfo($dll)
        $version = if ($versionInfo.ProductVersion) { $versionInfo.ProductVersion } else { $versionInfo.FileVersion }
        if ($version -notmatch '^9\.56(?:\D|$)') { continue }

        $uninstaller = Join-Path $root "Uninstall.exe"
        if (-not (Test-Path -LiteralPath $uninstaller)) {
            Write-OfflineLog "WARN: Found r12 J-Link $version at $root, but Uninstall.exe is missing. The workstation will still pin J-Link $($script:RequiredJLinkVersion) explicitly."
            continue
        }
        Write-OfflineLog "Removing incompatible r12 J-Link $version from canonical path: $root"
        $result = Invoke-ExternalProcess -FilePath $uninstaller -Arguments @("/S")
        Write-OfflineLog "J-Link $version uninstall exit=$($result.ExitCode): $($result.Output)"
        if ($result.ExitCode -ne 0 -and $result.ExitCode -ne 3010) {
            Write-OfflineLog "WARN: J-Link $version uninstall failed; continuing because workstation commands use an explicit --jdll path."
        } else {
            Start-Sleep -Milliseconds 500
        }
    }
}

function Invoke-PnPUtil {
    param([string[]]$Arguments)

    $pnputil = Join-Path $env:SystemRoot "System32\pnputil.exe"
    if (-not (Test-Path -LiteralPath $pnputil)) {
        $pnputil = "pnputil.exe"
    }
    return Invoke-ExternalProcess -FilePath $pnputil -Arguments $Arguments
}

function Get-SeggerDriverStoreStatus {
    param([System.IO.FileInfo[]]$InfFiles)

    $result = Invoke-PnPUtil -Arguments @("/enum-drivers")
    if ($result.ExitCode -ne 0) {
        throw "pnputil /enum-drivers failed with exit code $($result.ExitCode): $($result.Output)"
    }
    $registered = @()
    foreach ($inf in $InfFiles) {
        $pattern = "(?im)(?<![A-Za-z0-9_.-])" + [regex]::Escape($inf.Name) + "(?![A-Za-z0-9_.-])"
        if ($result.Output -match $pattern) {
            $registered += $inf.Name
        }
    }
    $expected = @($InfFiles | ForEach-Object { $_.Name })
    return [pscustomobject]@{
        Expected = $expected
        Registered = @($registered | Select-Object -Unique)
        Missing = @($expected | Where-Object { $_ -notin $registered })
    }
}

function Register-SeggerDriverStore {
    param([System.IO.FileInfo[]]$InfFiles)

    foreach ($inf in $InfFiles) {
        $result = Invoke-PnPUtil -Arguments @("/add-driver", $inf.FullName, "/install")
        Write-OfflineLog "pnputil /add-driver $($inf.Name) exit=$($result.ExitCode): $($result.Output)"
        if ($result.ExitCode -ne 0 -and $result.ExitCode -ne 3010) {
            Write-OfflineLog "WARN: Could not add $($inf.Name) through pnputil; continuing with remaining driver files"
        }
    }
}

function Request-PnPDeviceRescan {
    $scan = Invoke-PnPUtil -Arguments @("/scan-devices")
    Write-OfflineLog "pnputil /scan-devices exit=$($scan.ExitCode): $($scan.Output)"
    if ($scan.ExitCode -eq 0) {
        Start-Sleep -Milliseconds 500
    }
}

function Test-ConnectedSeggerDeviceStatus {
    $getPnpDevice = Get-Command Get-PnpDevice -ErrorAction SilentlyContinue
    if (-not $getPnpDevice) {
        Write-OfflineLog "NOTE: Get-PnpDevice is unavailable; Driver Store verification completed without connected-device status."
        return
    }
    try {
        $devices = @(
            Get-PnpDevice -PresentOnly -ErrorAction Stop |
                Where-Object { $_.InstanceId -like "USB\VID_1366*" }
        )
    } catch {
        Write-OfflineLog "WARN: Could not query connected SEGGER devices: $($_.Exception.Message)"
        return
    }
    if ($devices.Count -eq 0) {
        Write-OfflineLog "NOTE: No J-Link USB device is connected; SEGGER drivers were registered for use after the probe is connected."
        return
    }
    foreach ($device in $devices) {
        $name = if ($device.FriendlyName) { $device.FriendlyName } else { $device.Class }
        Write-OfflineLog "SEGGER PnP: status=$($device.Status) name=$name instance=$($device.InstanceId)"
    }
    $failed = @($devices | Where-Object { $_.Status -ne "OK" })
    if ($failed.Count -gt 0) {
        Write-OfflineLog "WARN: SEGGER USB driver files are registered, but one or more connected J-Link interfaces are not ready. Unplug and reconnect J-Link, then run nrfjprog --ids."
    }
}

function Install-SeggerUsbDrivers {
    param([Parameter(Mandatory = $true)]$DriverInstallation)

    Write-OfflineLog "Installing SEGGER USB drivers: $($DriverInstallation.InstDriversPath) /silent"
    $driverInstaller = Invoke-ExternalProcess `
        -FilePath $DriverInstallation.InstDriversPath `
        -Arguments @("/silent") `
        -WorkingDirectory $DriverInstallation.UsbDriverRoot
    Write-OfflineLog "InstDrivers.exe exit=$($driverInstaller.ExitCode): $($driverInstaller.Output)"
    $helperSucceeded = (
        $driverInstaller.ExitCode -eq 0 -or
        $driverInstaller.ExitCode -eq 3010 -or
        $driverInstaller.ExitCode -eq 1638
    )
    $infFiles = @($DriverInstallation.InfFiles)
    if ($infFiles.Count -gt 0) {
        if (-not $helperSucceeded) {
            Write-OfflineLog "WARN: InstDrivers.exe returned nonzero; attempting direct Driver Store registration."
        }
        $driverStore = Get-SeggerDriverStoreStatus -InfFiles $DriverInstallation.InfFiles
        if ($driverStore.Missing.Count -gt 0) {
            Write-OfflineLog "SEGGER Driver Store missing: $($driverStore.Missing -join ', '); registering bundled INF files."
            Register-SeggerDriverStore -InfFiles $DriverInstallation.InfFiles
            $driverStore = Get-SeggerDriverStoreStatus -InfFiles $DriverInstallation.InfFiles
        }
        if ($driverStore.Missing.Count -gt 0) {
            throw "SEGGER USB drivers were not registered in the Windows Driver Store: $($driverStore.Missing -join ', ')"
        }
        Write-OfflineLog "SEGGER Driver Store verified: $($driverStore.Registered -join ', ')"
    } elseif ($helperSucceeded) {
        Write-OfflineLog "SEGGER InstDrivers.exe completed successfully. No loose JLink INF files were exposed by this J-Link version; skipping file-based Driver Store verification."
    } else {
        throw "SEGGER InstDrivers.exe failed with exit code $($driverInstaller.ExitCode), and no loose JLink INF files were available for fallback registration."
    }
    Request-PnPDeviceRescan
    Test-ConnectedSeggerDeviceStatus
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
    foreach ($root in (Get-SeggerInstallRoots)) {
        foreach ($name in @("JLink.exe", "JLinkExe.exe")) {
            $candidate = Join-Path $root $name
            if (Test-Path -LiteralPath $candidate) { return $candidate }
        }
    }
    return ""
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
    if (-not (Test-Path -LiteralPath $configPath)) {
        throw "Workstation config not found: $configPath"
    }
    $config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
    $config | Add-Member -NotePropertyName "flash_backend" -NotePropertyValue "nrfjprog" -Force
    $config | Add-Member -NotePropertyName "flash_image_path" -NotePropertyValue ("firmware\{0}" -f $script:FirmwareLeaf) -Force
    $config | Add-Member -NotePropertyName "flash_after_wait_s" -NotePropertyValue 2.0 -Force
    $config | Add-Member -NotePropertyName "flash_timeout_s" -NotePropertyValue 180.0 -Force
    $config | Add-Member -NotePropertyName "flash_verify" -NotePropertyValue $false -Force
    $config | Add-Member -NotePropertyName "nrfjprog_path" -NotePropertyValue $(if ($NrfjprogPath) { $NrfjprogPath } else { "nrfjprog" }) -Force
    $config | Add-Member -NotePropertyName "jlink_dll_path" -NotePropertyValue $JLinkDllPath -Force
    $config | Add-Member -NotePropertyName "serial_baudrate" -NotePropertyValue 115200 -Force
    $config | Add-Member -NotePropertyName "serial_open_wait_s" -NotePropertyValue 0.0 -Force
    $config | Add-Member -NotePropertyName "start_prompt_patterns" -NotePropertyValue @() -Force
    $config | Add-Member -NotePropertyName "start_prompt_timeout_s" -NotePropertyValue 0.0 -Force
    $config | Add-Member -NotePropertyName "require_start_prompt" -NotePropertyValue $false -Force
    $config | Add-Member -NotePropertyName "test_start_command" -NotePropertyValue "AT+DRVTEST" -Force
    $config | Add-Member -NotePropertyName "records_root" -NotePropertyValue "bare_board_records" -Force
    $config | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $configPath -Encoding UTF8
    Write-OfflineLog "Configured default firmware: $firmwareTarget"
}

function Invoke-NrfjprogChecked {
    param(
        [string]$NrfjprogPath,
        [string[]]$Arguments
    )
    return Invoke-ExternalProcess -FilePath $NrfjprogPath -Arguments $Arguments
}

function Find-CompatibleJLinkInstallation {
    param([string]$NrfjprogPath)

    $candidates = @()
    foreach ($root in (Get-SeggerInstallRoots)) {
        $dlls = @(
            Get-ChildItem -LiteralPath $root -File -ErrorAction SilentlyContinue |
                Where-Object { $_.Name -in @("JLinkARM.dll", "JLink_x64.dll") }
        )
        foreach ($dll in $dlls) {
            $check = Invoke-NrfjprogChecked `
                -NrfjprogPath $NrfjprogPath `
                -Arguments @("--version", "--jdll", $dll.FullName)
            Write-OfflineLog "J-Link DLL check exit=$($check.ExitCode) path=$($dll.FullName): $($check.Output)"
            $version = ""
            if ($check.Output -match '(?im)^\s*JLinkARM\.dll version:\s*([^\s]+)\s*$') {
                $version = $matches[1]
            }
            if (
                $check.ExitCode -eq 0 -and
                $version -ieq $script:RequiredJLinkVersion -and
                $check.Output -notmatch 'JLinkARM\.dll reported error'
            ) {
                $candidates += [pscustomobject]@{
                    Root = $root
                    DllPath = $dll.FullName
                    Version = $version
                }
            }
        }
    }
    return @($candidates | Sort-Object DllPath | Select-Object -First 1)
}

function Test-InstalledTools {
    param(
        [string]$InstallRoot,
        [string]$FirmwarePath,
        [string]$JLinkDllPath
    )
    $nrfjprog = Find-Nrfjprog
    if (-not $nrfjprog) { throw "nrfjprog.exe not found after Nordic Command Line Tools install" }
    Write-OfflineLog "nrfjprog found: $nrfjprog"
    if (-not $JLinkDllPath -or -not (Test-Path -LiteralPath $JLinkDllPath)) {
        throw "Pinned J-Link DLL not found after installation: $JLinkDllPath"
    }
    $jlinkArgs = @("--jdll", $JLinkDllPath)
    $version = Invoke-NrfjprogChecked -NrfjprogPath $nrfjprog -Arguments (@("--version") + $jlinkArgs)
    Write-OfflineLog "nrfjprog --version exit=$($version.ExitCode): $($version.Output)"
    if (
        $version.ExitCode -ne 0 -or
        $version.Output -notmatch ("(?im)^\s*JLinkARM\.dll version:\s*" + [regex]::Escape($script:RequiredJLinkVersion) + "\s*$") -or
        $version.Output -match 'JLinkARM\.dll reported error'
    ) {
        throw "nrfjprog is not using clean J-Link $($script:RequiredJLinkVersion) output: $($version.Output)"
    }
    $ids = Invoke-NrfjprogChecked -NrfjprogPath $nrfjprog -Arguments (@("--ids") + $jlinkArgs)
    Write-OfflineLog "nrfjprog --ids exit=$($ids.ExitCode): $($ids.Output)"
    if ($ids.ExitCode -ne 0 -or -not $ids.Output) {
        Write-OfflineLog "NOTE: J-Link probe was not detected during install self-check. This does not mean the offline install failed. Connect/replug J-Link USB, then verify with nrfjprog --ids or the workstation's detector."
    }

    $jlink = Find-JLinkTool
    if ($jlink) {
        Write-OfflineLog "J-Link tool found: $jlink"
    } else {
        throw "J-Link tool not found after Nordic Command Line Tools installed its bundled SEGGER package."
    }

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

function Refresh-ProcessPath {
    $machinePath = [System.Environment]::GetEnvironmentVariable("Path", "Machine")
    $userPath = [System.Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = @($machinePath, $userPath) -join ";"
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
    $jlinkInstallation = $null
    if (-not $SkipVcRedist) {
        if (-not (Install-VcRedist -Path (Join-Path $depsDir "vc_redist.x64.exe"))) {
            throw "VC++ redistributable installation failed"
        }
    }
    if (-not $SkipJLink -and -not $SkipNordicCli) {
        Remove-R12JLink956Installation
    }
    if (-not $SkipNordicCli) {
        $nordicInstaller = Join-Path $depsDir "nordic-command-line-tools-installer.exe"
        $nordicArguments = @("/S")
        if ($SkipJLink) {
            $nordicArguments += "NoSegger=1"
        }
        Install-ExeDependency -Name "Nordic Command Line Tools" -Path $nordicInstaller -Arguments $nordicArguments | Out-Null
        Refresh-ProcessPath
    }
    if (-not $SkipJLink) {
        $nrfjprogForJLink = Find-Nrfjprog
        if (-not $nrfjprogForJLink) {
            throw "nrfjprog.exe is required to validate the Nordic-bundled J-Link $($script:RequiredJLinkVersion)"
        }
        $jlinkInstallation = Install-JLinkStack -NrfjprogPath $nrfjprogForJLink
    }
    if (-not $SkipWorkstation) {
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
    $installedFirmware = Join-Path $InstallRoot ("firmware\{0}" -f $script:FirmwareLeaf)
    if (-not $SkipFirmware) {
        Set-WorkstationDefaultFirmware `
            -InstallRoot $InstallRoot `
            -FirmwareSource (Join-Path $PackageRoot ("firmware\{0}" -f $script:FirmwareLeaf)) `
            -NrfjprogPath (Find-Nrfjprog) `
            -JLinkDllPath $(if ($jlinkInstallation) { $jlinkInstallation.DllPath } else { "" })
    }
    if (-not $SkipWorkstation -and -not $SkipJLink -and -not $SkipNordicCli -and -not $SkipFirmware) {
        Test-InstalledTools `
            -InstallRoot $InstallRoot `
            -FirmwarePath $installedFirmware `
            -JLinkDllPath $jlinkInstallation.DllPath
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
        "2. Verify probe: nrfjprog --ids",
        "3. Open the workstation and set the correct COM port (115200).",
        "4. If Device Manager shows VID_1366 / PID_0105 with an error, unplug and reconnect J-Link, then run nrfjprog --ids again.",
        "5. If flash says 'No debuggers were discovered', rerun install_offline_win10.cmd as administrator.",
        "6. The workstation is pinned to Nordic-compatible J-Link $($script:RequiredJLinkVersion), even if another J-Link version is installed.",
        "7. Run a test; nrfjprog flash warnings about --verify are normal when verify is disabled.",
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
