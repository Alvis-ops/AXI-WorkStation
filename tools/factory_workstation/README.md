# POC3A Factory Workstation

Run:

```powershell
python tools\factory_workstation\app.py
```

CLI:

```powershell
# Formal half-machine run with SN and records.
python tools\factory_workstation\cli.py half --transport uart --port COM18 --baudrate 460800 --sn AXIP1TEST001 --token <token>

# Temporary dry-run: no SN validation, no SN write/read and no record files.
python tools\factory_workstation\cli.py half --transport uart --port COM18 --baudrate 460800 --no-sn-record

# Host/device bring-up without MOMO touch steps.
python tools\factory_workstation\cli.py half --transport uart --port COM18 --baudrate 460800 --sn AXIP1TEST001 --token <token> --skip-momo

# Full-machine run. OTA is off unless config.json enables it or --ota is passed.
python tools\factory_workstation\cli.py full --transport uart --port COM18 --baudrate 460800 --sn AXIP1TEST001 --token <token> --no-ota --skip-momo

# New firmware defaults to compact capture output. Use legacy only when testing an older image.
python tools\factory_workstation\cli.py full --transport ble --ble-backend nrf_dongle --dongle-port COM8 --ble-address C8:B9:CA:AC:85:74 --no-sn-record --capture-output-mode legacy

# Print every capture frame during troubleshooting; default CLI output hides frame spam.
python tools\factory_workstation\cli.py full --transport ble --ble-backend nrf_dongle --dongle-port COM8 --ble-address C8:B9:CA:AC:85:74 --no-sn-record --verbose-frames
```

Host smoke:

```powershell
python tools\factory_workstation\smoke_p1_0h.py
```

Defaults:

- GUI stack: Python `tkinter` with `ttkbootstrap` theme widgets.
- Development transport: UART.
- BLE target name: `AXI-P1-T`.
- BLE scan backend: `nrf_dongle` by default. It reuses the installed nRF Connect for Desktop Bluetooth Low Energy app and the Nordic DONGLE CDC port, default `COM8`. The fallback backends are `windows` (Bleak/WinRT) and `auto`.
- BLE NUS connection backend: `nrf_dongle` by default. The same Dongle radio that scans also connects, enables NUS TX notifications and writes AT commands to NUS RX, so no Windows system Bluetooth is required. Select `bleak` in the GUI or with CLI `--ble-backend bleak` to fall back to Windows Bleak/WinRT.
- SN/record mode: enabled by default. Disable `启用 SN/记录` or pass `--no-sn-record` for temporary dry-run tests without SN validation, SN write, or CSV/log file output. If no factory token is provided in dry-run mode, the workstation skips `AT+FACTORY=UNLOCK` / `AT+FACTORY=LOCK` and sends the remaining AT steps directly.
- OTA: no firmware version restriction; the configured DFU package is uploaded directly.
- Default OTA image: `build_ondemand/axi-p1-embeded/zephyr/zephyr.signed.bin`. The older top-level `build_ondemand/dfu_application.zip` may be stale after incremental builds.
- Runtime tokens: environment or `tools/factory_workstation/.env`, not `config.json`.
- Capture output: `capture_output_mode` defaults to `compact`. The workstation sends `...,COMPACT` capture commands for MOMO empty capture, LRA vibcapture and PPG reflect capture when the firmware supports it. Set `capture_output_mode` to `legacy` for older firmware.

Useful token keys:

```text
AXI_FACTORY_ENGINEER_TOKEN=...
AXI_FACTORY_RECOVER_TOKEN=...
AXI_FACTORY_ENGINEER_PASSWORD=...
# or AXI_FACTORY_ENGINEER_PASSWORD_SHA256=...
```

The workstation stores each SN-record run under `factory_records/YYYY-MM-DD/` with `metadata.json`, `raw_at.log`, `unified_log.csv`, `factory_test_items.csv`, and compatibility capture CSV files.
`unified_log.csv` is the primary timeline for a run: AT TX/RX, step start/end, parsed MOMO/LRA/PPG frames, summaries and final result are ordered by `event_index`. `factory_test_items.csv` remains the compatibility step result file during the transition. Parser-generated files such as `items.csv` are auxiliary line-ingest outputs and are not the pass/fail source.
Dry-run mode (`--no-sn-record` or unchecked SN/record mode) does not create formal record files.

## BLE DONGLE scan and connection

The GUI BLE scan page and the BLE NUS connection can both go through the nRF Connect for Desktop backend that works with the Nordic DONGLE. Close the nRF Connect BLE app before scanning or connecting from the workstation, because the DONGLE COM port is exclusive. The scan result shows the device name, address, RSSI, and source such as `nRF dongle COM8`.

With the default `nrf_dongle` backend, the workstation scans, connects, enables NUS TX notifications and writes AT commands to NUS RX all through the Dongle radio. The CLI exposes this via `--ble-backend nrf_dongle --dongle-port COM8 --ble-address <addr>`. Select `bleak` to fall back to the Windows Bleak/WinRT transport, which requires a Windows Bluetooth adapter.

## OP mode vs engineering mode

- **OP mode**: half-machine, full-machine, OTA, BLE scan/selection, and connection controls. Manual AT engineering debug is disabled. Factory token is read from `.env` or environment variables; the operator does not need to see or type it.
- **Engineering mode**: enables settings, factory token setup, and manual AT engineering debug. It requires `AXI_FACTORY_ENGINEER_PASSWORD` or `AXI_FACTORY_ENGINEER_PASSWORD_SHA256`. The token setup dialog stores the token in `.env`, then hides it from the UI; after logout, OP mode can still run automated tests with that hidden token.

## Factory AT capability probe

On startup, the flow sends `AT+CAP?` and checks `factory_prod=1`. If the firmware does not expose factory AT (e.g., `POC3A_AT_TEST=n` in出厂交付状态), the flow stops with `NG: factory AT not available` before attempting unlock.

## OTA integration

- OTA is optional (`ota_enabled` in config, default `false`).
- When enabled, the full-machine flow runs: pre-check (`AT+VER?`, `AT+OTABUSY?`, image check) -> NUS disconnect -> SMP upload -> reconnect -> OTA state check -> factory tests.
- OTA does not restrict by firmware version; `AT+VER?` and image signing version are record-only.
- Same-hash upload (identical image) only verifies the upload path. It is recorded as `PENDING-HW` for the full OTA flow and does not continue into factory unlock/tests.
- A real OTA (different image) must reconnect over NUS and pass `AT+OTABUSY?` with `locked=0` before factory unlock/tests continue.

## Single firmware AT capability

The same single firmware image is used for both development and factory. If `POC3A_AT_TEST` is disabled in the出厂交付状态, the workstation detects this via `AT+CAP?` and warns the operator. Factory AT, `AT+LOG`, recover token, and production gate boundaries are documented in the plan (`P1_0_factory_host_workstation_build_plan.md`).
