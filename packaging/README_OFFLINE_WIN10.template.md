# Axi Factory Workstation - Win10 x64 Offline USB Package

Built: {{DATE}}

This folder is a complete offline delivery package for factory PCs that cannot download dependencies from the internet. Copy the entire folder to a USB drive or local disk on the target PC, then run the offline installer.

## Contents

| Path | Purpose |
|------|---------|
| install_offline_win10.cmd | One-click offline install entry (recommended) |
| shared/offline_jlink_env.ps1 | Shared flash-environment detection and J-Link 7.94e pinning helpers |
| app/{{SETUP_EXE}} | Workstation setup installer |
| deps/nrfconnect-bluetooth-low-energy/ | Portable nRF Connect BLE runtime for DONGLE backend |
| deps/vc_redist.x64.exe | Microsoft VC++ 2015-2022 x64 redistributable |
| deps/nordic-command-line-tools-installer.exe | Nordic Command Line Tools (`nrfjprog`) and validated J-Link 7.94e |
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
2. Install VC++ x64 redistributable silently, or skip if already installed.
3. Copy nRF Connect BLE into `%LOCALAPPDATA%\Programs\nrfconnect-bluetooth-low-energy`, or skip if already present.
4. Remove incompatible canonical J-Link 9.56 if present.
5. Reuse an existing compatible `nrfjprog + J-Link 7.94e` environment when detected; otherwise install Nordic Command Line Tools with bundled SEGGER.
6. Pin `jlink_dll_path` in workstation `config.json` and pass `nrfjprog --jdll` for flashing.
7. Launch the workstation setup installer, or skip if already installed unless `-ForceReinstall` is used.
8. Copy the default firmware hex into the workstation install folder and update flash settings.
9. Run install self-checks using the pinned DLL: `nrfjprog --version --jdll`, optional `nrfjprog --ids --jdll`, BLE backend path, and firmware path.

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
- If a bare-board workstation or another factory install already configured the flash environment, the second install reuses it instead of reinstalling Nordic/J-Link.
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
