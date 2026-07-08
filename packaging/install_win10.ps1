$ErrorActionPreference = "Stop"

$script:LogPath = Join-Path ([System.IO.Path]::GetTempPath()) "AxiFactoryWorkstation_install.log"

function Write-InstallLog {
    param([string]$Message)
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Add-Content -LiteralPath $script:LogPath -Value $line -Encoding UTF8
}

function Show-InstallMessage {
    param(
        [string]$Title,
        [string]$Message,
        [int]$Seconds = 0
    )
    if ($env:AXI_FACTORY_NO_MESSAGE -eq "1") {
        return
    }
    try {
        $shell = New-Object -ComObject WScript.Shell
        [void]$shell.Popup($Message, $Seconds, $Title, 64)
    } catch {
        Write-Host "$Title`n$Message"
    }
}

function Test-IsElevated {
    try {
        $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
        $principal = New-Object Security.Principal.WindowsPrincipal($identity)
        return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    } catch {
        return $false
    }
}

function Expand-PayloadZip {
    param(
        [string]$ZipPath,
        [string]$Destination
    )

    if (Get-Command Expand-Archive -ErrorAction SilentlyContinue) {
        Write-InstallLog "Extracting with Expand-Archive"
        Expand-Archive -LiteralPath $ZipPath -DestinationPath $Destination -Force
        return
    }

    Write-InstallLog "Expand-Archive unavailable; extracting with Shell.Application"
    $shell = New-Object -ComObject Shell.Application
    $zip = $shell.NameSpace($ZipPath)
    $dest = $shell.NameSpace($Destination)
    if ($null -eq $zip -or $null -eq $dest) {
        throw "Unable to open payload zip. Please install PowerShell 5+ or use a newer Windows image."
    }

    $dest.CopyHere($zip.Items(), 20)
    $deadline = (Get-Date).AddMinutes(5)
    do {
        Start-Sleep -Milliseconds 500
        $sourceRoot = Join-Path $Destination "Axi Factory Workstation"
        $rootExe = Join-Path $Destination "Axi Factory Workstation.exe"
        if ((Test-Path -LiteralPath $sourceRoot) -or (Test-Path -LiteralPath $rootExe)) {
            return
        }
    } while ((Get-Date) -lt $deadline)

    throw "Timed out while extracting payload zip."
}

function New-Shortcut {
    param(
        [string]$ShortcutPath,
        [string]$TargetPath,
        [string]$WorkingDirectory,
        [string]$IconLocation,
        [string]$Arguments = ""
    )
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($ShortcutPath)
    $shortcut.TargetPath = $TargetPath
    if ($Arguments) {
        $shortcut.Arguments = $Arguments
    }
    $shortcut.WorkingDirectory = $WorkingDirectory
    $shortcut.IconLocation = $IconLocation
    $shortcut.Save()
}

function Try-NewShortcut {
    param(
        [string]$ShortcutPath,
        [string]$TargetPath,
        [string]$WorkingDirectory,
        [string]$IconLocation,
        [string]$Arguments = ""
    )
    try {
        $parent = Split-Path -Parent $ShortcutPath
        New-Item -ItemType Directory -Force -Path $parent | Out-Null
        New-Shortcut -ShortcutPath $ShortcutPath -TargetPath $TargetPath -WorkingDirectory $WorkingDirectory -IconLocation $IconLocation -Arguments $Arguments
        Write-InstallLog "Created shortcut: $ShortcutPath"
        return $true
    } catch {
        Write-InstallLog "Shortcut failed: $ShortcutPath ; $($_.Exception.Message)"
        return $false
    }
}

function Get-FolderPathSafe {
    param(
        [string]$Name,
        [string]$Fallback = ""
    )

    try {
        $value = [Environment]::GetFolderPath($Name)
        if ($value -ne $null -and $value.Trim().Length -gt 0) {
            return $value
        }
    } catch {
        Write-InstallLog "Special folder unavailable: $Name ; $($_.Exception.Message)"
    }
    return $Fallback
}

function Get-CommonDesktopPath {
    if ($env:PUBLIC) {
        return (Join-Path $env:PUBLIC "Desktop")
    }
    return ""
}

function Get-CommonProgramsPath {
    if ($env:ProgramData) {
        return (Join-Path $env:ProgramData "Microsoft\Windows\Start Menu\Programs")
    }
    if ($env:ALLUSERSPROFILE) {
        return (Join-Path $env:ALLUSERSPROFILE "Microsoft\Windows\Start Menu\Programs")
    }
    return ""
}

function Select-InstallRoot {
    param([string]$DefaultPath)

    if ($env:AXI_FACTORY_INSTALL_DIR) {
        return [Environment]::ExpandEnvironmentVariables($env:AXI_FACTORY_INSTALL_DIR)
    }
    if ($env:AXI_FACTORY_NO_PATH_DIALOG -eq "1") {
        return $DefaultPath
    }

    try {
        New-Item -ItemType Directory -Force -Path $DefaultPath | Out-Null
        Add-Type -AssemblyName System.Windows.Forms
        $dialog = New-Object System.Windows.Forms.FolderBrowserDialog
        $dialog.Description = "Select the install folder for Axi Factory Workstation. Click Cancel to use the default folder."
        $dialog.SelectedPath = $DefaultPath
        $dialog.ShowNewFolderButton = $true
        $result = $dialog.ShowDialog()
        if (($result -eq [System.Windows.Forms.DialogResult]::OK) -and ($dialog.SelectedPath -ne $null) -and ($dialog.SelectedPath.Trim().Length -gt 0)) {
            return $dialog.SelectedPath
        }
        Write-InstallLog "Install folder dialog canceled; using default path"
        return $DefaultPath
    } catch {
        Write-InstallLog "Install folder dialog unavailable; using default path: $($_.Exception.Message)"
        return $DefaultPath
    }
}

function Resolve-PayloadSourceRoot {
    param([string]$ExtractionRoot)

    $nestedRoot = Join-Path $ExtractionRoot "Axi Factory Workstation"
    $nestedExe = Join-Path $nestedRoot "Axi Factory Workstation.exe"
    if (Test-Path -LiteralPath $nestedExe) {
        return $nestedRoot
    }

    $rootExe = Join-Path $ExtractionRoot "Axi Factory Workstation.exe"
    if (Test-Path -LiteralPath $rootExe) {
        return $ExtractionRoot
    }

    throw "Payload executable not found after extraction. Checked: $nestedExe ; $rootExe"
}

try {
    if (Test-Path -LiteralPath $script:LogPath) {
        Remove-Item -LiteralPath $script:LogPath -Force
    }
    Write-InstallLog "Installer started"

    $packageDir = Split-Path -Parent $MyInvocation.MyCommand.Path
    $payloadZip = Join-Path $packageDir "Axi_Factory_Workstation_payload.zip"
    if (-not (Test-Path -LiteralPath $payloadZip)) {
        throw "Missing payload: $payloadZip"
    }

    $defaultInstallRoot = Join-Path $env:LOCALAPPDATA "Programs\Axi Factory Workstation"
    $installRoot = Select-InstallRoot -DefaultPath $defaultInstallRoot
    $installRoot = [System.IO.Path]::GetFullPath($installRoot)
    Write-InstallLog "Install root: $installRoot"

    # Only create the install folder itself. Creating the drive root for paths
    # such as E:\AXI can fail on some locked-down factory PCs.
    New-Item -ItemType Directory -Force -Path $installRoot | Out-Null

    $tempRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("AxiFactoryWorkstation_" + [System.Guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Force -Path $tempRoot | Out-Null
    try {
        Expand-PayloadZip -ZipPath $payloadZip -Destination $tempRoot
        $sourceRoot = Resolve-PayloadSourceRoot -ExtractionRoot $tempRoot
        Write-InstallLog "Payload source root: $sourceRoot"

        Get-ChildItem -LiteralPath $sourceRoot -Force | ForEach-Object {
            if (@("config.json", ".env", ".env.template", "factory_records") -contains $_.Name) {
                return
            }
            Copy-Item -LiteralPath $_.FullName -Destination $installRoot -Recurse -Force
        }

        $configPath = Join-Path $installRoot "config.json"
        $sourceConfig = Join-Path $sourceRoot "config.json"
        $forceConfig = $env:AXI_FACTORY_FORCE_CONFIG -eq "1"
        $configValid = $false
        if ((Test-Path -LiteralPath $configPath) -and -not $forceConfig) {
            try {
                $cfgBytes = [System.IO.File]::ReadAllBytes($configPath)
                if ($cfgBytes.Length -gt 0 -and -not ($cfgBytes[0] -eq 0x7B -and $cfgBytes.Length -ge 2 -and $cfgBytes[1] -eq 0x00)) {
                    $null = ([System.Text.Encoding]::UTF8.GetString($cfgBytes) | ConvertFrom-Json)
                    $configValid = $true
                }
            } catch {
                Write-InstallLog "Existing config.json is invalid; replacing from payload"
            }
        }
        if (-not $configValid) {
            Copy-Item -LiteralPath $sourceConfig -Destination $configPath -Force
            Write-InstallLog "Wrote config.json from payload"
        }

        $envTemplatePath = Join-Path $installRoot ".env.template"
        if (-not (Test-Path -LiteralPath $envTemplatePath)) {
            Copy-Item -LiteralPath (Join-Path $sourceRoot ".env.template") -Destination $envTemplatePath -Force
        }

        New-Item -ItemType Directory -Force -Path (Join-Path $installRoot "factory_records") | Out-Null

        $exePath = Join-Path $installRoot "Axi Factory Workstation.exe"
        if (-not (Test-Path -LiteralPath $exePath)) {
            throw "Installed executable not found: $exePath"
        }

        $uninstallPath = Join-Path $installRoot "uninstall.ps1"
        @"
`$ErrorActionPreference = "Stop"
`$installRoot = Split-Path -Parent `$MyInvocation.MyCommand.Path
`$commonPrograms = if (`$env:ProgramData) { Join-Path `$env:ProgramData "Microsoft\Windows\Start Menu\Programs" } elseif (`$env:ALLUSERSPROFILE) { Join-Path `$env:ALLUSERSPROFILE "Microsoft\Windows\Start Menu\Programs" } else { "" }
`$userPrograms = [Environment]::GetFolderPath("Programs")
`$startMenuDirs = @(`$commonPrograms, `$userPrograms) | Where-Object { `$_ } | ForEach-Object { Join-Path `$_ "Axi Factory Workstation" } | Select-Object -Unique
`$commonDesktop = if (`$env:PUBLIC) { Join-Path `$env:PUBLIC "Desktop" } else { "" }
`$userDesktop = [Environment]::GetFolderPath("Desktop")
`$desktopShortcuts = @()
if (`$commonDesktop) { `$desktopShortcuts += (Join-Path `$commonDesktop "Axi Factory Workstation.lnk") }
if (`$userDesktop) { `$desktopShortcuts += (Join-Path `$userDesktop "Axi Factory Workstation.lnk") }
`$desktopShortcuts = `$desktopShortcuts | Where-Object { `$_ } | Select-Object -Unique
foreach (`$shortcut in `$desktopShortcuts) {
    Remove-Item -LiteralPath `$shortcut -Force -ErrorAction SilentlyContinue
}
foreach (`$dir in `$startMenuDirs) {
    Remove-Item -LiteralPath `$dir -Recurse -Force -ErrorAction SilentlyContinue
}
Remove-Item -LiteralPath "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\AxiFactoryWorkstation" -Recurse -Force -ErrorAction SilentlyContinue
Write-Host "Installed files remain at: `$installRoot"
Write-Host "Remove this folder manually if you also want to delete config, tokens, and test records."
"@ | Set-Content -LiteralPath $uninstallPath -Encoding UTF8

        $createdShortcuts = @()
        if ($env:AXI_FACTORY_NO_SHORTCUTS -ne "1") {
            $isElevated = Test-IsElevated
            $commonDesktop = Get-CommonDesktopPath
            $userDesktop = Get-FolderPathSafe -Name "Desktop"
            $commonPrograms = Get-CommonProgramsPath
            $userPrograms = Get-FolderPathSafe -Name "Programs"

            $desktopCandidates = @()
            if ($isElevated -and $commonDesktop) { $desktopCandidates += (Join-Path $commonDesktop "Axi Factory Workstation.lnk") }
            if ($userDesktop) { $desktopCandidates += (Join-Path $userDesktop "Axi Factory Workstation.lnk") }

            $desktopCreated = $false
            foreach ($candidate in ($desktopCandidates | Select-Object -Unique)) {
                if (Try-NewShortcut -ShortcutPath $candidate -TargetPath $exePath -WorkingDirectory $installRoot -IconLocation "$exePath,0") {
                    $createdShortcuts += $candidate
                    $desktopCreated = $true
                    break
                }
            }
            if (-not $desktopCreated) {
                Write-InstallLog "No desktop shortcut could be created"
            }

            $programsRoot = if ($isElevated -and $commonPrograms) { $commonPrograms } else { $userPrograms }
            if ($programsRoot) {
                $startMenuDir = Join-Path $programsRoot "Axi Factory Workstation"
                $startShortcut = Join-Path $startMenuDir "Axi Factory Workstation.lnk"
                if (Try-NewShortcut -ShortcutPath $startShortcut -TargetPath $exePath -WorkingDirectory $installRoot -IconLocation "$exePath,0") {
                    $createdShortcuts += $startShortcut
                }
                $uninstallShortcut = Join-Path $startMenuDir "Uninstall Axi Factory Workstation.lnk"
                if (Try-NewShortcut -ShortcutPath $uninstallShortcut -TargetPath "powershell.exe" -WorkingDirectory $installRoot -IconLocation "powershell.exe,0" -Arguments "-NoProfile -ExecutionPolicy Bypass -File `"$uninstallPath`"") {
                    $createdShortcuts += $uninstallShortcut
                }
            }
        }

        $uninstallKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\AxiFactoryWorkstation"
        New-Item -Path $uninstallKey -Force | Out-Null
        New-ItemProperty -Path $uninstallKey -Name "DisplayName" -Value "Axi Factory Workstation" -PropertyType String -Force | Out-Null
        New-ItemProperty -Path $uninstallKey -Name "DisplayVersion" -Value "2026.07.06-r5" -PropertyType String -Force | Out-Null
        New-ItemProperty -Path $uninstallKey -Name "Publisher" -Value "AXI" -PropertyType String -Force | Out-Null
        New-ItemProperty -Path $uninstallKey -Name "InstallLocation" -Value $installRoot -PropertyType String -Force | Out-Null
        New-ItemProperty -Path $uninstallKey -Name "UninstallString" -Value "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$uninstallPath`"" -PropertyType String -Force | Out-Null

        if ($env:AXI_FACTORY_NO_LOCATION_NOTE -ne "1") {
            $noteDesktop = Get-FolderPathSafe -Name "Desktop"
            $locationNote = if ($noteDesktop) { Join-Path $noteDesktop "Axi Factory Workstation Install Location.txt" } else { "" }
            try {
                if ($locationNote) {
                    @(
                        "Axi Factory Workstation install location:",
                        $installRoot,
                        "",
                        "Executable:",
                        $exePath,
                        "",
                        "Install log:",
                        $script:LogPath
                    ) | Set-Content -LiteralPath $locationNote -Encoding UTF8
                    Write-InstallLog "Wrote location note: $locationNote"
                } else {
                    Write-InstallLog "Desktop folder unavailable; location note skipped"
                }
            } catch {
                Write-InstallLog "Could not write location note: $($_.Exception.Message)"
            }
        }

        Copy-Item -LiteralPath $script:LogPath -Destination (Join-Path $installRoot "install.log") -Force -ErrorAction SilentlyContinue

        if ($env:AXI_FACTORY_NO_OPEN -ne "1") {
            try {
                Start-Process explorer.exe -ArgumentList "/select,`"$exePath`""
            } catch {
                Write-InstallLog "Could not open install folder: $($_.Exception.Message)"
            }
        }

        $shortcutText = if ($createdShortcuts.Count -gt 0) {
            "Shortcut:`n" + (($createdShortcuts | Select-Object -First 3) -join "`n")
        } else {
            "No shortcut was created. Open the exe from the install folder."
        }
        Show-InstallMessage -Title "Axi Factory Workstation Setup" -Message ("Installation completed.`n`nInstall path:`n{0}`n`n{1}`n`nLog:`n{2}" -f $installRoot, $shortcutText, $script:LogPath)

        Write-InstallLog "Installation completed"
    } finally {
        if (Test-Path -LiteralPath $tempRoot) {
            Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
} catch {
    $message = $_.Exception.Message
    Write-InstallLog "ERROR: $message"
    Show-InstallMessage -Title "Axi Factory Workstation Setup Failed" -Message ("Installation failed.`n`nError:`n{0}`n`nLog:`n{1}" -f $message, $script:LogPath)
    exit 1
}
