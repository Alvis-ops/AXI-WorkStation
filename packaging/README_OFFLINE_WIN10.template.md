# Axi Factory Workstation - Win10 x64 Offline USB Package

Built: {{DATE}}

This folder is a complete offline delivery package for factory PCs that cannot download dependencies from the internet. Copy the entire folder to a USB drive or local disk on the target PC, then run the offline installer.

## Contents

| Path | Purpose |
|------|---------|
| install_offline_win10.cmd | One-click offline install entry (recommended) |
| app/{{SETUP_EXE}} | Workstation setup installer |
| deps/nrfconnect-bluetooth-low-energy/ | Portable nRF Connect BLE runtime for DONGLE backend |
| deps/vc_redist.x64.exe | Microsoft VC++ 2015-2022 x64 redistributable |
| deps/nordic-command-line-tools-installer.exe | Nordic Command Line Tools installer (`nrfjprog`) |
| deps/segger-jlink-installer.exe | Optional standalone SEGGER J-Link installer, only present when explicitly bundled |
| firmware/axi_p1_factory_merged.hex | Default single firmware image for factory flashing |
| SHA256SUMS.txt | Integrity checksums |
| MANIFEST.json | Build metadata |

Bundled nRF Connect BLE version: {{NRF_VERSION}}

## Install

Double-click:

```text
install_offline_win10.cmd
```

The script will:

1. Verify package SHA256 checksums.
2. Install VC++ x64 redistributable silently, or accept an existing installation.
3. Copy nRF Connect BLE into `%LOCALAPPDATA%\Programs\nrfconnect-bluetooth-low-energy`.
4. Install standalone SEGGER J-Link if bundled.
5. Install Nordic Command Line Tools. This is the primary flashing dependency and is expected to provide `nrfjprog` plus the required J-Link runtime.
6. Launch the workstation setup installer.
7. Copy the default firmware hex into the workstation install folder and update `config.json`.
8. Run install self-checks: `nrfjprog --version`, `nrfjprog --ids`, J-Link tool path, BLE backend path, and default firmware path.

Optional custom install path:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install_offline_win10.ps1 -InstallRoot "D:\Axi\FactoryWorkstation"
```

## After Install

- No engineering token or password is included. Configure credentials in the GUI engineering menu on the target PC.
- Default BLE target name: `AXI-P1-T`.
- Default DONGLE COM port: `COM8`.
- Default UART: `COM18 @ 460800`; change it per machine if needed.
- Default flash backend: `nrfjprog`.
- Default flash image: `firmware\axi_p1_factory_merged.hex` under the workstation install folder.
- The installer writes the install path to the completion dialog and to a desktop text note when possible.

## Verify Checksums Manually

From PowerShell in this folder:

```powershell
Get-Content .\SHA256SUMS.txt | ForEach-Object {
  $expected, $rel = $_ -split '\s+', 2
  $actual = (Get-FileHash -LiteralPath $rel -Algorithm SHA256).Hash.ToLowerInvariant()
  if ($actual -ne $expected) { throw "Mismatch: $rel" }
}
"SHA256 OK"
```

## Missing Items In This Build

{{MISSING_DEPS}}

If anything is missing, rebuild the package on a prepared build PC before taking the USB package to the factory PC.
