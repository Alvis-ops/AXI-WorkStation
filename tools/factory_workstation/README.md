# POC3A Factory Workstation

Maintained source: this `AXI-WorkStation/tools/factory_workstation/` directory is the only current workstation source. The older `axi-p1-embeded/tools/factory_workstation/` copy is historical and is not a launch entry.

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

# Half-machine run with chip flash first. This uses the same half flow after flashing.
python tools\factory_workstation\cli.py half --transport uart --port COM18 --baudrate 460800 --sn AXIP1TEST001 --token <token> --flash-before-test --flash-image E:\firmware\merged.hex --flash-backend nrfjprog --jlink-probe-id 69730371

# Flash-first dry-run for fixture bring-up: no SN validation and no formal records.
python tools\factory_workstation\cli.py half --transport uart --port COM18 --baudrate 460800 --no-sn-record --flash-before-test --flash-image E:\firmware\merged.hex --flash-after-wait-s 8

# Full-machine run. OTA is off unless config.json enables it or --ota is passed.
python tools\factory_workstation\cli.py full --transport uart --port COM18 --baudrate 460800 --sn AXIP1TEST001 --token <token> --no-ota --skip-momo

# Preview the standalone OTA command without connecting or uploading.
python tools\factory_workstation\cli.py ota --ble-backend bleak --ble-address E3:A9:F3:49:97:A7 --ota-dry-run

# Standalone OTA: connect directly over SMP and upload.
python tools\factory_workstation\cli.py ota --ble-backend bleak --ble-address E3:A9:F3:49:97:A7

# Firmware whose SMP characteristic requires authenticated access: pair through Windows first.
python tools\factory_workstation\cli.py ota --ble-backend bleak --ble-pair --ble-address E3:A9:F3:49:97:A7

# Optional recovery handoff: connect NUS, request AT+RST, release BLE, then upload.
python tools\factory_workstation\cli.py ota --ble-backend bleak --ble-address E3:A9:F3:49:97:A7 --ota-handoff

# New firmware defaults to compact capture output. Use legacy only when testing an older image.
python tools\factory_workstation\cli.py full --transport ble --ble-backend nrf_dongle --dongle-port COM8 --ble-address C8:B9:CA:AC:85:74 --no-sn-record --capture-output-mode legacy

# Print every capture frame during troubleshooting; default CLI output hides frame spam.
python tools\factory_workstation\cli.py full --transport ble --ble-backend nrf_dongle --dongle-port COM8 --ble-address C8:B9:CA:AC:85:74 --no-sn-record --verbose-frames

# Write legacy compatibility record files in addition to unified_log.csv.
python tools\factory_workstation\cli.py full --transport uart --port COM18 --baudrate 460800 --sn AXIP1TEST001 --token <token> --no-ota --record-output-mode split
```

MES diagnostic CLI:

```powershell
# Send checkroute and print the complete request/response.
# This returns exit code 3 when HTTP succeeds but the MES business success rule is not configured.
python tools\factory_workstation\cli.py mes-checkroute --sn AXIP1TEST001

# Temporarily treat HTTP 2xx as success while investigating an undocumented MES response.
python tools\factory_workstation\cli.py mes-checkroute --sn AXIP1TEST001 --mes-http-2xx-is-success

# Preview a flat postxtdata payload with nested per-item details without sending it.
python tools\factory_workstation\cli.py mes-post --sn AXIP1TEST001 --mes-station full

# Send a reviewed JSON payload file explicitly.
python tools\factory_workstation\cli.py mes-post --mes-payload .\mes_payload.json --mes-send
```

The `mes` section in `config.json` contains the two URLs, device ID, line, half/full station names,
timeout and response-success rule. MES has no independent runtime enable switch: formal
`启用 SN/记录` mode always uses MES for both half-machine and full-machine tests, while
`--no-sn-record` is the explicit local dry-run mode and skips MES.

Formal sequencing is:

1. Validate the local SN/token/full-machine half-record prerequisites.
2. Create the local run record. `checkroute_enabled` currently defaults to `false`, so route checking is skipped; enable it later when MES confirms the route response contract.
3. Start optional flashing and device tests.
4. Upload one `postxtdata` result after the final device outcome. Run metadata, `device_result`,
   `test_items` and `failed_items` are top-level fields; each test item still contains nested
   PASS/NG state, elapsed time, failure reason, response summary and parsed measurements.
   `measurements.summary` contains result summaries and `measurements.samples` stores parsed
   Touch/LRA/PPG values as channel arrays. The same array index identifies the same frame across
   every channel; missing fields are represented by JSON `null`. `sample_count`,
   `uploaded_sample_count` and `truncated` make frame loss explicit. Up to 1000 frames are carried
   per test item; normal configured captures are below it.
5. Close the local run record after MES confirms the upload or a pending JSON is safely written.

The GUI factory-operation page and CLI output report device result and MES result separately.
`MES：已确认上传` / `[MES] CONFIRMED` means MES accepted the result. `MES：未确认` /
`[MES] UNCONFIRMED` means it must not be treated as uploaded; the message includes the pending
file path when one was created. CLI exit code `3` represents MES not confirmed, even when the
device itself passed. Device NG with MES-confirmed FAIL remains exit code `2`.

The current `postxtdata` response rule uses `res == "OK"`. HTTP 2xx alone is not treated as
business success. After a confirmed upload, the GUI result dialog explicitly displays
`MES 数据上传成功` for both device PASS and device FAIL outcomes.

If `mes-post --mes-send` cannot confirm success, the CLI writes
`factory_records/mes_pending/<run_id>.json`. It does not retry automatically.
Formal half/full runs use the same pending directory when `postxtdata` is not confirmed.
The pending JSON contains the complete payload and the last error for controlled manual replay.
Automatic replay is intentionally disabled until MES idempotency rules are confirmed.

Host smoke:

```powershell
python tools\factory_workstation\smoke_p1_0h.py
python tools\factory_workstation\smoke_mes.py
```

Defaults:

- GUI stack: Python `tkinter` with `ttkbootstrap` theme widgets.
- Development transport: UART.
- BLE target name: `AXI-P1-T`.
- BLE scan backend: `nrf_dongle` by default. It reuses the installed nRF Connect for Desktop Bluetooth Low Energy app and the Nordic DONGLE CDC port, default `COM8`. The fallback backends are `windows` (Bleak/WinRT) and `auto`.
- BLE NUS connection backend: `nrf_dongle` by default. The same Dongle radio that scans also connects, enables NUS TX notifications and writes AT commands to NUS RX, so no Windows system Bluetooth is required. Select `bleak` in the GUI or with CLI `--ble-backend bleak` to fall back to Windows Bleak/WinRT.
- BLE authentication/pairing: disabled by default. Enable `BLE 认证/配对（Windows）` for firmware whose GATT permissions require an authenticated link (for example mcumgr `RW_AUTHEN`). The switch forces Windows Bleak for scan, NUS and OTA, and the first connection may display a Windows pairing prompt. CLI uses `--ble-pair`; `--no-ble-pair` overrides a saved setting. The nRF Dongle backend does not implement host pairing.
- SN/record mode: enabled by default. Disable `启用 SN/记录` or pass `--no-sn-record` for temporary dry-run tests without SN validation, SN write, or CSV/log file output. If no factory token is provided in dry-run mode, the workstation skips `AT+FACTORY=UNLOCK` / `AT+FACTORY=LOCK` and sends the remaining AT steps directly.
- OTA: no firmware version restriction; the configured DFU package is uploaded directly. When the GUI is connected to the selected BLE target, it requests `AT+RST`, waits for the current BLE session to close completely, and then starts the standalone OTA helper. This cold-start handoff also supports firmware versions whose disconnect-to-advertising recovery is unreliable. An operator may scan, connect, confirm the SN, and then click OTA without manually disconnecting.
- Default OTA image: `build_ondemand/axi-p1-embeded/zephyr/zephyr.signed.bin`. The older top-level `build_ondemand/dfu_application.zip` may be stale after incremental builds.
- Runtime tokens: environment or `tools/factory_workstation/.env`, not `config.json`.
- Chip flash: optional and disabled by default. Engineering mode can enable `half_flash_before_test`, select `nrfjprog` or `script`, choose independent flash image `flash_image_path`, choose half-machine pre-test flash image `half_flash_image_path`, set `jlink_probe_id`, and set `flash_after_wait_s`. The bundled `flash_selected_image.ps1` script consumes the selected image, nrfjprog path, J-Link DLL, probe ID and verify option explicitly. When enabled, half-machine test first flashes `half_flash_image_path`, then reconnects and runs the original half-machine AT flow.
- `nrfjprog` flashing uses `flash_timeout_s` (default 180 seconds). Offline installs pin `jlink_dll_path` to Nordic-compatible J-Link 7.94e and every flash passes it through `nrfjprog --jdll`. Every flash also runs `nrfjprog --version` and `--ids`: a single detected probe is selected automatically; multi-probe stations must set `jlink_probe_id` (SNR), and a configured SNR must be detected. Exit code 41 is reported as “未检测到 J-Link 探头”.
- Capture output: `capture_output_mode` defaults to `compact`. The workstation sends `...,COMPACT` capture commands for MOMO empty capture, LRA vibcapture and PPG reflect capture when the firmware supports it. Set `capture_output_mode` to `legacy` for older firmware.
- Record output: `record_output_mode` defaults to `unified`. In the GUI settings page, set `记录格式` to `集成记录（单个 unified_log.csv）` or `分散记录（兼容多文件）`. CLI can override it with `--record-output-mode unified|split`.

Useful token keys:

```text
AXI_FACTORY_ENGINEER_TOKEN=...
AXI_FACTORY_RECOVER_TOKEN=...
AXI_FACTORY_ENGINEER_PASSWORD=...
# or AXI_FACTORY_ENGINEER_PASSWORD_SHA256=...
```

The workstation stores each SN-record run under `factory_records/YYYY-MM-DD/<run_id>/`. In the default `unified` mode, it writes only `unified_log.csv`; this CSV is the formal record for the run: run metadata, AT TX/RX, step start/end, parsed MOMO/LRA/PPG frames, summaries and final result are ordered by `event_index`. In `split` mode, it still writes `unified_log.csv` and also writes the compatibility files such as `raw_at.log`, `factory_test_items.csv`, `momo_raw.csv`, `momo_filt.csv`, `lra_frames.csv`, `ppg_frames.csv`, `capture_summary.csv`, `metadata.json`, and `summary.csv`.
Dry-run mode (`--no-sn-record` or unchecked SN/record mode) does not create formal record files.

## BLE DONGLE scan and connection

The GUI BLE scan page and the BLE NUS connection can both go through the nRF Connect for Desktop backend that works with the Nordic DONGLE. Close the nRF Connect BLE app before scanning or connecting from the workstation, because the DONGLE COM port is exclusive. The scan result shows the device name, address, RSSI, and source such as `nRF dongle COM8`.

With the default `nrf_dongle` backend, the workstation scans, connects, enables NUS TX notifications and writes AT commands to NUS RX all through the Dongle radio. The CLI exposes this via `--ble-backend nrf_dongle --dongle-port COM8 --ble-address <addr>`. Select `bleak` to fall back to the Windows Bleak/WinRT transport, which requires a Windows Bluetooth adapter.

For an authenticated SMP characteristic, turn on `BLE 认证/配对（Windows）` before scanning or connecting. If Windows already has a stale bond and pairing still fails, remove the device in Windows Bluetooth settings and clear the device-side pairing data before retrying. This switch requests pairing; it cannot bypass the firmware's authentication policy.

## OP mode vs engineering mode

- **OP mode**: half-machine, full-machine, OTA, BLE scan/selection, and connection controls. Manual AT engineering debug is disabled. Factory token is read from `.env` or environment variables; the operator does not need to see or type it. The AT log panel hides raw TX/RX lines in OP mode; step status, INFO/OK/WARN/ERR, and file records remain complete.
- **First launch**: if this station has no engineer password yet (`.env` / environment / `config.engineer_password_sha256`), the GUI shows a blocking setup dialog. An engineer password is required before the main window can be used; factory token may be left empty and set later after engineering login.
- **Engineering mode**: enables settings, independent chip flashing, factory token setup, and manual AT engineering debug. It requires `AXI_FACTORY_ENGINEER_PASSWORD` or `AXI_FACTORY_ENGINEER_PASSWORD_SHA256`. First-setup and later password saves store SHA256 only. The token setup dialog stores the token in `.env`, then hides it from the UI; after logout, OP mode can still run automated tests with that hidden token. Half-machine flash-before-test can be configured by an engineer and then executed by the operator as part of the automated half flow. Engineering mode also shows detailed AT TX/RX in the log panel.

## UI / record performance notes

- Record writes use batched flush for CSV sinks and `raw_at.log` (when split mode is enabled). Step end and run close force a flush so the last batch is on disk.
- Capture frame RX lines are stored as semantic events (`touch_frame` / `vib_frame` / `ppg_frame`) in `unified_log.csv` and no longer also write a redundant `at_rx` row. Non-frame RX still writes `at_rx`. `ingest_line` still runs for every RX line, so split frame CSVs are unchanged.
- GUI event loop drains a bounded snapshot each tick, processes control events (`step` / `busy` / `flow_done` / connection / prompts) first, then batch-inserts log lines once. PASS/NG status colors use a small overlay on the status cell only; other step text stays black. Window resize layout is debounced until the drag settles.

## Chip flashing

- Default backend: `nrfjprog`.
- Default images in the offline installer: both independent flashing and half-machine pre-test flashing point to `firmware\axi_p1_factory_merged.hex`.
- Independent flashing is available on the `芯片烧录` tab after engineering login and uses `flash_image_path`.
- The `J-Link ID` scan button enumerates attached probes. A single probe is filled into the GUI and saved automatically; multiple probes require the engineer to leave only the target connected or enter the intended ID.
- Starting every `nrfjprog` flash performs the pinned-DLL J-Link precheck (`--version`, then `--ids`) in addition to local path validation. The `烧录检测` button remains available for an explicit check before starting a job.
- The same probe enumeration and configured-ID validation applies to the `script` backend. Select the bundled `flash_selected_image.ps1` to make the GUI's firmware path authoritative; the workstation passes `-ImagePath`, nrfjprog, J-Link DLL, probe ID and verification choice to it. Legacy custom scripts remain argument-compatible and may opt into `AXI_FLASH_IMAGE_PATH`, `AXI_FLASH_NRFJPROG_PATH`, `AXI_FLASH_JLINK_DLL_PATH`, `AXI_FLASH_VERIFY`, and `POC3A_JLINK_ID`; otherwise their internal fixed path still takes precedence.
- Half-machine flash-before-test is configured in `设置` and uses `half_flash_image_path`. When enabled, the workstation closes any current UART/BLE connection, runs J-Link flashing, waits `flash_after_wait_s`, reconnects with the selected transport, probes `AT` and `AT+VER?`, then starts the existing half-machine flow.
- Flash failures stop the half-machine flow before `AT+FACTORY=UNLOCK`.
- In SN/record mode, `unified_log.csv` contains `flash_start`, preflight/flash logs, `flash_end` (including selected SNR, full command, output, checksum, verification and reset result), and a `Firmware flash` step. In dry-run mode, no formal CSV record is created.

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
