# Axi Bare Board Workstation - Win10 x64 Offline USB Package

Built: {{DATE}}

This folder is a complete offline delivery package for factory PCs that cannot download dependencies from the internet. Copy the entire folder to a USB drive or local disk on the target PC, then run the offline installer.

## Contents

| Path | Purpose |
|------|---------|
| INSTALL_FIRST.txt | Quick install checklist |
| install_offline_win10.cmd | One-click offline install entry (run as administrator) |
| shared/offline_jlink_env.ps1 | Shared flash-environment detection and J-Link 7.94e pinning helpers |
| app/{{SETUP_EXE}} | Workstation setup installer |
| deps/vc_redist.x64.exe | Microsoft VC++ 2015-2022 x64 redistributable |
| deps/nordic-command-line-tools-installer.exe | Nordic Command Line Tools (`nrfjprog`) and its validated J-Link 7.94e package |
| firmware/poc3a_factory_merged.hex | Default factory test firmware for bare board flashing |
| SHA256SUMS.txt | Integrity checksums |
| MANIFEST.json | Build metadata |

## Install

**Read INSTALL_FIRST.txt before installing.**

Right-click and run as administrator:

```text
install_offline_win10.cmd
```

Do **not** install by running only `app\{{SETUP_EXE}}`. That skips J-Link 7.94e, its USB driver, nrfjprog, VC++, and default firmware setup.

The script will:

1. Verify package SHA256 checksums.
2. Install VC++ x64 redistributable silently, or skip if already installed.
3. Remove incompatible canonical J-Link 9.56 if present.
4. Reuse an existing compatible `nrfjprog + J-Link 7.94e` environment when detected; otherwise install Nordic Command Line Tools with bundled SEGGER.
5. Validate J-Link 7.94e with `nrfjprog --version --jdll <path>` and save that DLL path in workstation `config.json`.
6. Run `USBDriver\InstDrivers.exe /silent` when a fresh J-Link install is needed; register loose INF files with `pnputil` when available.
7. Launch the workstation setup installer, or skip if already installed unless `-ForceReinstall` is used.
8. Copy the default firmware hex into the workstation install folder and update `config.json`.
9. Run install self-checks using the pinned DLL: `nrfjprog --version --jdll`, optional `nrfjprog --ids --jdll`, and application/firmware paths.

Optional custom install path:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install_offline_win10.ps1 -InstallRoot "D:\Axi\BareBoardWorkstation"
```

## After Install

- Default UART: `115200`; set the correct COM port per machine in the GUI.
- Default flash backend: `nrfjprog`.
- Default flash image: `firmware\poc3a_factory_merged.hex` under the workstation install folder.
- Test flow: flash → wait 2s → open COM → send `AT+DRVTEST`.
- Records are written to `bare_board_records` under the install folder when SN recording is enabled.
- The workstation explicitly passes J-Link 7.94e through `nrfjprog --jdll`, so a separately installed newer J-Link does not override the validated version.
- If another workstation already installed the flash environment, the second install reuses it instead of reinstalling Nordic/J-Link.
- If flash says `No debuggers were discovered`, connect J-Link USB and re-run `install_offline_win10.cmd` as administrator.
- The installer can complete without a connected probe; connect/replug J-Link after installation and use `nrfjprog --ids` or the workstation's `检测烧录器` button.
- If Device Manager shows `VID_1366` / `PID_0105` with an error, unplug/replug J-Link after the installer completes. The script runs SEGGER's driver helper and also registers matching INF files when the installed J-Link version exposes them.
- If you see nrfjprog warnings such as `without --verify`, that is normal when verify is disabled; it is not an install failure.

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
