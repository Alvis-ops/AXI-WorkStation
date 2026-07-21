param([string]$InstallRoot = "")
$ErrorActionPreference = "Stop"
$utf8 = New-Object System.Text.UTF8Encoding $false
if (-not $InstallRoot) {
  foreach ($r in @((Join-Path $env:LOCALAPPDATA "Programs\Axi Factory Workstation"), "E:\AXI", "G:\AXI")) {
    if (Test-Path (Join-Path $r "Axi Factory Workstation.exe")) { $InstallRoot = $r; break }
  }
}
if (-not $InstallRoot) { throw "Pass -InstallRoot to the folder containing Axi Factory Workstation.exe" }
$cfg = Join-Path $InstallRoot "config.json"
$content = @"
{
  "firmware_repo": ".",
  "flash_script_path": "flash_selected_image.ps1",
  "half_flash_before_test": false,
  "flash_backend": "nrfjprog",
  "flash_image_path": "firmware\\axi_p1_factory_merged.hex",
  "half_flash_image_path": "firmware\\axi_p1_factory_merged.hex",
  "flash_after_wait_s": 8.0,
  "flash_timeout_s": 180.0,
  "flash_verify": true,
  "nrfjprog_path": "nrfjprog",
  "jlink_dll_path": "",
  "jlink_probe_id": "",
  "uart_port": "COM18",
  "uart_baudrate": 460800,
  "dut_alias": "",
  "ble_name": "AXI-P1-T",
  "ble_address_whitelist": [],
  "ble_scan_backend": "nrf_dongle",
  "ble_dongle_port": "COM8",
  "ble_dongle_sd_version": "auto",
  "nrf_connect_ble_path": "",
  "ota_image_path": "dfu_application.zip",
  "ota_enabled": false,
  "ota_reboot_wait_s": 15.0,
  "records_root": "factory_records",
  "prefer_transport": "UART",
  "station_id": "DEV",
  "sn_enabled": false,
  "capture_output_mode": "compact",
  "record_output_mode": "unified",
  "factory_at_required": true,
  "engineer_password_sha256": "",
  "sn_rule": {
    "min_len": 1,
    "max_len": 32,
    "prefix": "",
    "regex": "^[A-Za-z0-9_-]+$"
  },
  "at_timeouts": {
    "default_s": 5.0,
    "unlock_s": 8.0,
    "hw_short_s": 12.0,
    "touch_capture_s": 45.0,
    "vibcapture_s": 60.0,
    "ppg_capture_s": 45.0,
    "ota_s": 300.0
  }
}
"@
[System.IO.File]::WriteAllText($cfg, $content.Trim(), $utf8)
Write-Host "Fixed: $cfg"
