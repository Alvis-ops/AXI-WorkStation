# Shared J-Link / nrfjprog environment helpers for offline workstation installers.
# Dot-source from packaging/*/install_offline_win10.ps1

if (-not $script:RequiredJLinkVersion) {
    $script:RequiredJLinkVersion = "7.94e"
}

function Initialize-OfflineJLinkEnv {
    param([string]$RequiredVersion = "7.94e")
    $script:RequiredJLinkVersion = $RequiredVersion
}

function Refresh-ProcessPath {
    $machinePath = [System.Environment]::GetEnvironmentVariable("Path", "Machine")
    $userPath = [System.Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = @($machinePath, $userPath) -join ";"
}

function Test-VcRedistInstalled {
    $paths = @(
        "HKLM:\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64",
        "HKLM:\SOFTWARE\WOW6432Node\Microsoft\VisualStudio\14.0\VC\Runtimes\x64"
    )
    foreach ($path in $paths) {
        if (-not (Test-Path -LiteralPath $path)) { continue }
        $installed = (Get-ItemProperty -LiteralPath $path -ErrorAction SilentlyContinue).Installed
        if ($installed -eq 1) { return $true }
    }
    return $false
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
    $stdoutPath = Join-Path $tempRoot ("AxiOffline_stdout_" + [System.Guid]::NewGuid().ToString("N") + ".log")
    $stderrPath = Join-Path $tempRoot ("AxiOffline_stderr_" + [System.Guid]::NewGuid().ToString("N") + ".log")
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

function Invoke-NrfjprogChecked {
    param(
        [string]$NrfjprogPath,
        [string[]]$Arguments
    )
    return Invoke-ExternalProcess -FilePath $NrfjprogPath -Arguments $Arguments
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

function Find-CompatibleJLinkInstallation {
    param([string]$NrfjprogPath)

    if (-not $NrfjprogPath) { return $null }

    $candidates = @()
    foreach ($root in (Get-SeggerInstallRoots)) {
        $dlls = @(
            Get-ChildItem -LiteralPath $root -File -ErrorAction SilentlyContinue |
                Where-Object { $_.Name -in @("JLinkARM.dll", "JLink_x64.dll") }
        )
        foreach ($dll in $dlls) {
            $versionCheck = Invoke-NrfjprogChecked `
                -NrfjprogPath $NrfjprogPath `
                -Arguments @("--version", "--jdll", $dll.FullName)
            $idsCheck = Invoke-NrfjprogChecked `
                -NrfjprogPath $NrfjprogPath `
                -Arguments @("--ids", "--jdll", $dll.FullName)
            if ($script:OfflineLogCallback) {
                & $script:OfflineLogCallback ("J-Link DLL check exit=$($versionCheck.ExitCode) path=$($dll.FullName): $($versionCheck.Output)")
                if ($idsCheck.Output) {
                    & $script:OfflineLogCallback ("J-Link DLL ids exit=$($idsCheck.ExitCode) path=$($dll.FullName): $($idsCheck.Output)")
                }
            }
            $version = ""
            if ($versionCheck.Output -match '(?im)^\s*JLinkARM\.dll version:\s*([^\s]+)\s*$') {
                $version = $matches[1]
            }
            $prefer64 = [Environment]::Is64BitOperatingSystem -and $dll.Name -ieq "JLink_x64.dll"
            if (
                $versionCheck.ExitCode -eq 0 -and
                $idsCheck.ExitCode -eq 0 -and
                $version -ieq $script:RequiredJLinkVersion -and
                $versionCheck.Output -notmatch 'JLinkARM\.dll reported error' -and
                $idsCheck.Output -notmatch 'JLinkARM\.dll reported error'
            ) {
                $candidates += [pscustomobject]@{
                    Root = $root
                    DllPath = $dll.FullName
                    Version = $version
                    Prefer64 = $prefer64
                }
            }
        }
    }
    return @(
        $candidates |
            Sort-Object `
                @{ Expression = { $_.Prefer64 }; Descending = $true },
                @{ Expression = { $_.DllPath } } |
            Select-Object -First 1
    )
}

function Test-CompatibleJLinkDll {
    param(
        [string]$NrfjprogPath,
        [string]$JLinkDllPath
    )
    if (-not $NrfjprogPath -or -not $JLinkDllPath -or -not (Test-Path -LiteralPath $JLinkDllPath)) {
        return $false
    }
    $check = Invoke-NrfjprogChecked -NrfjprogPath $NrfjprogPath -Arguments @("--version", "--jdll", $JLinkDllPath)
    if ($check.ExitCode -ne 0 -or $check.Output -match 'JLinkARM\.dll reported error') {
        return $false
    }
    return $check.Output -match ("(?im)^\s*JLinkARM\.dll version:\s*" + [regex]::Escape($script:RequiredJLinkVersion) + "\s*$")
}

function Get-ExistingFlashEnvironment {
    param(
        [string]$NrfjprogPath = "",
        [string]$PreferredJLinkDllPath = ""
    )

    Refresh-ProcessPath
    if (-not $NrfjprogPath) {
        $NrfjprogPath = Find-Nrfjprog
    }

    $jlinkInstallation = $null
    if ($PreferredJLinkDllPath -and (Test-CompatibleJLinkDll -NrfjprogPath $NrfjprogPath -JLinkDllPath $PreferredJLinkDllPath)) {
        $jlinkInstallation = [pscustomobject]@{
            Root = Split-Path -Parent $PreferredJLinkDllPath
            DllPath = $PreferredJLinkDllPath
            Version = $script:RequiredJLinkVersion
        }
    } elseif ($NrfjprogPath) {
        $jlinkInstallation = Find-CompatibleJLinkInstallation -NrfjprogPath $NrfjprogPath
    }

    $ready = [bool]$NrfjprogPath -and [bool]$jlinkInstallation
    return [pscustomobject]@{
        Ready = $ready
        NrfjprogPath = $NrfjprogPath
        JLinkInstallation = $jlinkInstallation
        JLinkDllPath = if ($jlinkInstallation) { $jlinkInstallation.DllPath } else { "" }
        JLinkToolPath = Find-JLinkTool
    }
}

function Remove-IncompatibleJLink956Installation {
    param([scriptblock]$LogCallback = $null)

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
            if ($LogCallback) {
                & $LogCallback "WARN: Found incompatible J-Link $version at $root, but Uninstall.exe is missing. Workstation commands will still pin J-Link $($script:RequiredJLinkVersion) explicitly."
            }
            continue
        }
        if ($LogCallback) {
            & $LogCallback "Removing incompatible J-Link $version from canonical path: $root"
        }
        $result = Invoke-ExternalProcess -FilePath $uninstaller -Arguments @("/S")
        if ($LogCallback) {
            & $LogCallback "J-Link $version uninstall exit=$($result.ExitCode): $($result.Output)"
        }
        if ($result.ExitCode -ne 0 -and $result.ExitCode -ne 3010) {
            if ($LogCallback) {
                & $LogCallback "WARN: J-Link $version uninstall failed; continuing because workstation commands use an explicit --jdll path."
            }
        } else {
            Start-Sleep -Milliseconds 500
        }
    }
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
        if ($script:OfflineLogCallback) {
            & $script:OfflineLogCallback "pnputil /add-driver $($inf.Name) exit=$($result.ExitCode): $($result.Output)"
        }
        if ($result.ExitCode -ne 0 -and $result.ExitCode -ne 3010) {
            if ($script:OfflineLogCallback) {
                & $script:OfflineLogCallback "WARN: Could not add $($inf.Name) through pnputil; continuing with remaining driver files"
            }
        }
    }
}

function Request-PnPDeviceRescan {
    $scan = Invoke-PnPUtil -Arguments @("/scan-devices")
    if ($script:OfflineLogCallback) {
        & $script:OfflineLogCallback "pnputil /scan-devices exit=$($scan.ExitCode): $($scan.Output)"
    }
    if ($scan.ExitCode -eq 0) {
        Start-Sleep -Milliseconds 500
    }
}

function Test-ConnectedSeggerDeviceStatus {
    param([scriptblock]$LogCallback = $null)

    $getPnpDevice = Get-Command Get-PnpDevice -ErrorAction SilentlyContinue
    if (-not $getPnpDevice) {
        if ($LogCallback) {
            & $LogCallback "NOTE: Get-PnpDevice is unavailable; Driver Store verification completed without connected-device status."
        }
        return
    }
    try {
        $devices = @(
            Get-PnpDevice -PresentOnly -ErrorAction Stop |
                Where-Object { $_.InstanceId -like "USB\VID_1366*" }
        )
    } catch {
        if ($LogCallback) {
            & $LogCallback "WARN: Could not query connected SEGGER devices: $($_.Exception.Message)"
        }
        return
    }
    if ($devices.Count -eq 0) {
        if ($LogCallback) {
            & $LogCallback "NOTE: No J-Link USB device is connected; SEGGER drivers were registered for use after the probe is connected."
        }
        return
    }
    foreach ($device in $devices) {
        $name = if ($device.FriendlyName) { $device.FriendlyName } else { $device.Class }
        if ($LogCallback) {
            & $LogCallback "SEGGER PnP: status=$($device.Status) name=$name instance=$($device.InstanceId)"
        }
    }
    $failed = @($devices | Where-Object { $_.Status -ne "OK" })
    if ($failed.Count -gt 0 -and $LogCallback) {
        & $LogCallback "WARN: SEGGER USB driver files are registered, but one or more connected J-Link interfaces are not ready. Unplug and reconnect J-Link, then run nrfjprog --ids."
    }
}

function Install-SeggerUsbDrivers {
    param(
        [Parameter(Mandatory = $true)]$DriverInstallation,
        [scriptblock]$LogCallback = $null
    )

    if ($LogCallback) {
        & $LogCallback "Installing SEGGER USB drivers: $($DriverInstallation.InstDriversPath) /silent"
    }
    $driverInstaller = Invoke-ExternalProcess `
        -FilePath $DriverInstallation.InstDriversPath `
        -Arguments @("/silent") `
        -WorkingDirectory $DriverInstallation.UsbDriverRoot
    if ($LogCallback) {
        & $LogCallback "InstDrivers.exe exit=$($driverInstaller.ExitCode): $($driverInstaller.Output)"
    }
    $helperSucceeded = (
        $driverInstaller.ExitCode -eq 0 -or
        $driverInstaller.ExitCode -eq 3010 -or
        $driverInstaller.ExitCode -eq 1638
    )
    $infFiles = @($DriverInstallation.InfFiles)
    if ($infFiles.Count -gt 0) {
        if (-not $helperSucceeded -and $LogCallback) {
            & $LogCallback "WARN: InstDrivers.exe returned nonzero; attempting direct Driver Store registration."
        }
        $driverStore = Get-SeggerDriverStoreStatus -InfFiles $DriverInstallation.InfFiles
        if ($driverStore.Missing.Count -gt 0) {
            if ($LogCallback) {
                & $LogCallback "SEGGER Driver Store missing: $($driverStore.Missing -join ', '); registering bundled INF files."
            }
            Register-SeggerDriverStore -InfFiles $DriverInstallation.InfFiles
            $driverStore = Get-SeggerDriverStoreStatus -InfFiles $DriverInstallation.InfFiles
        }
        if ($driverStore.Missing.Count -gt 0) {
            throw "SEGGER USB drivers were not registered in the Windows Driver Store: $($driverStore.Missing -join ', ')"
        }
        if ($LogCallback) {
            & $LogCallback "SEGGER Driver Store verified: $($driverStore.Registered -join ', ')"
        }
    } elseif ($helperSucceeded) {
        if ($LogCallback) {
            & $LogCallback "SEGGER InstDrivers.exe completed successfully. No loose JLink INF files were exposed by this J-Link version; skipping file-based Driver Store verification."
        }
    } else {
        throw "SEGGER InstDrivers.exe failed with exit code $($driverInstaller.ExitCode), and no loose JLink INF files were available for fallback registration."
    }
    Request-PnPDeviceRescan
    Test-ConnectedSeggerDeviceStatus -LogCallback $LogCallback
}

function Install-ExeDependency {
    param(
        [string]$Name,
        [string]$Path,
        [string[]]$Arguments,
        [scriptblock]$LogCallback = $null
    )
    if (-not (Test-Path -LiteralPath $Path)) {
        throw "$Name installer missing: $Path"
    }
    if ($LogCallback) {
        & $LogCallback "Installing ${Name}: $Path $($Arguments -join ' ')"
    }
    $proc = Start-Process -FilePath $Path -ArgumentList $Arguments -Wait -PassThru
    if ($LogCallback) {
        & $LogCallback ("{0} installer exit code: {1}" -f $Name, $proc.ExitCode)
    }
    if ($proc.ExitCode -ne 0 -and $proc.ExitCode -ne 3010 -and $proc.ExitCode -ne 1638) {
        throw "$Name installer failed with exit code $($proc.ExitCode)"
    }
    return $true
}

function Ensure-JLinkStack {
    param(
        [string]$NordicInstallerPath,
        [string]$NrfjprogPath = "",
        [string]$PreferredJLinkDllPath = "",
        [switch]$SkipDriverInstall,
        [scriptblock]$LogCallback = $null
    )

    $script:OfflineLogCallback = $LogCallback
    Remove-IncompatibleJLink956Installation -LogCallback $LogCallback
    Refresh-ProcessPath

    $existing = Get-ExistingFlashEnvironment -NrfjprogPath $NrfjprogPath -PreferredJLinkDllPath $PreferredJLinkDllPath
    if ($existing.Ready) {
        if ($LogCallback) {
            & $LogCallback "Existing compatible flash environment detected; skipping Nordic/J-Link reinstall."
            & $LogCallback "Using nrfjprog: $($existing.NrfjprogPath)"
            & $LogCallback "Pinned J-Link $($script:RequiredJLinkVersion): $($existing.JLinkDllPath)"
        }
        return $existing.JLinkInstallation
    }

    if (-not $existing.NrfjprogPath) {
        if (-not $NordicInstallerPath -or -not (Test-Path -LiteralPath $NordicInstallerPath)) {
            throw "nrfjprog.exe not found and Nordic Command Line Tools installer is missing: $NordicInstallerPath"
        }
        Install-ExeDependency -Name "Nordic Command Line Tools" -Path $NordicInstallerPath -Arguments @("/S") -LogCallback $LogCallback | Out-Null
        Refresh-ProcessPath
        $existing.NrfjprogPath = Find-Nrfjprog
    }

    if (-not $existing.NrfjprogPath) {
        throw "nrfjprog.exe is required to validate the Nordic-bundled J-Link $($script:RequiredJLinkVersion)"
    }

    $compatible = Find-CompatibleJLinkInstallation -NrfjprogPath $existing.NrfjprogPath
    if (-not $compatible) {
        throw "Compatible J-Link $($script:RequiredJLinkVersion) was not found after Nordic Command Line Tools install. Rerun with bundled SEGGER enabled."
    }
    if ($LogCallback) {
        & $LogCallback "Pinned Nordic-compatible J-Link $($compatible.Version): $($compatible.DllPath)"
    }

    if (-not $SkipDriverInstall) {
        $driverInstallation = Get-SeggerDriverInstallation -PreferredRoot $compatible.Root
        if (-not $driverInstallation) {
            throw "J-Link $($compatible.Version) was found, but its USBDriver\InstDrivers.exe was not found."
        }
        Install-SeggerUsbDrivers -DriverInstallation $driverInstallation -LogCallback $LogCallback
    }
    return $compatible
}

function Test-FlashEnvironmentReady {
    param(
        [string]$NrfjprogPath,
        [string]$JLinkDllPath,
        [scriptblock]$LogCallback = $null
    )

    if (-not $NrfjprogPath) { throw "nrfjprog.exe not found after install" }
    if (-not $JLinkDllPath -or -not (Test-Path -LiteralPath $JLinkDllPath)) {
        throw "Pinned J-Link DLL not found after install: $JLinkDllPath"
    }
    if ($LogCallback) {
        & $LogCallback "nrfjprog found: $NrfjprogPath"
    }
    $jlinkArgs = @("--jdll", $JLinkDllPath)
    $version = Invoke-NrfjprogChecked -NrfjprogPath $NrfjprogPath -Arguments (@("--version") + $jlinkArgs)
    if ($LogCallback) {
        & $LogCallback "nrfjprog --version exit=$($version.ExitCode): $($version.Output)"
    }
    if (
        $version.ExitCode -ne 0 -or
        $version.Output -notmatch ("(?im)^\s*JLinkARM\.dll version:\s*" + [regex]::Escape($script:RequiredJLinkVersion) + "\s*$") -or
        $version.Output -match 'JLinkARM\.dll reported error'
    ) {
        throw "nrfjprog is not using clean J-Link $($script:RequiredJLinkVersion) output: $($version.Output)"
    }
    $ids = Invoke-NrfjprogChecked -NrfjprogPath $NrfjprogPath -Arguments (@("--ids") + $jlinkArgs)
    if ($LogCallback) {
        & $LogCallback "nrfjprog --ids exit=$($ids.ExitCode): $($ids.Output)"
    }
    if ($ids.ExitCode -ne 0 -or -not $ids.Output) {
        if ($LogCallback) {
            & $LogCallback "NOTE: J-Link probe was not detected during install self-check. Connect/replug J-Link USB, then verify with nrfjprog --ids or the workstation detector."
        }
    }

    $jlink = Find-JLinkTool
    if (-not $jlink) {
        throw "J-Link tool not found after Nordic Command Line Tools installed its bundled SEGGER package."
    }
    if ($LogCallback) {
        & $LogCallback "J-Link tool found: $jlink"
    }
}

function Get-ConfigJLinkDllPath {
    param([string]$ConfigPath)

    if (-not (Test-Path -LiteralPath $ConfigPath)) { return "" }
    try {
        $config = Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json
        return [string]$config.jlink_dll_path
    } catch {
        return ""
    }
}

function Get-NrfjprogProbeIds {
    param(
        [string]$NrfjprogPath,
        [string]$JLinkDllPath
    )
    if (-not $NrfjprogPath -or -not $JLinkDllPath) { return @() }
    $ids = Invoke-NrfjprogChecked -NrfjprogPath $NrfjprogPath -Arguments @("--ids", "--jdll", $JLinkDllPath)
    $probeIds = @()
    foreach ($raw in ($ids.Output -split "[\r\n]+")) {
        $line = $raw.Trim()
        if ($line -match '^\d{6,16}$') {
            $probeIds += $line
        }
    }
    return @($probeIds | Select-Object -Unique)
}

function Update-WorkstationFlashConfig {
    param(
        [string]$ConfigPath,
        [hashtable]$Values,
        [switch]$PreserveExistingUserSettings,
        [string[]]$ReplaceMissingFilePathSettings = @()
    )

    if (-not (Test-Path -LiteralPath $ConfigPath)) {
        throw "Workstation config not found: $ConfigPath"
    }
    $config = Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json
    $configDir = Split-Path -Parent $ConfigPath
    foreach ($key in $Values.Keys) {
        $value = $Values[$key]
        if ($PreserveExistingUserSettings -and $config.PSObject.Properties.Name -contains $key) {
            $existing = [string]$config.$key
            $replaceMissingPath = $false
            if ($existing -and $ReplaceMissingFilePathSettings -contains $key) {
                $existingPath = if ([System.IO.Path]::IsPathRooted($existing)) {
                    $existing
                } else {
                    Join-Path $configDir $existing
                }
                $replaceMissingPath = -not (Test-Path -LiteralPath $existingPath -PathType Leaf)
            }
            if ($existing -and -not $replaceMissingPath) { continue }
        }
        $config | Add-Member -NotePropertyName $key -NotePropertyValue $value -Force
    }
    $config | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $ConfigPath -Encoding UTF8
}

function Test-NrfConnectBleReady {
    param([string]$TargetDir)

    $exeName = "nRF Connect for Desktop Bluetooth Low Energy.exe"
    $exePath = Join-Path $TargetDir $exeName
    return (Test-Path -LiteralPath $exePath)
}
