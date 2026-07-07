from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .config import WorkstationConfig


@dataclass
class OtaCommand:
    argv: list[str]
    cwd: str
    script_name: str


def _uses_nrf_dongle_backend(config: WorkstationConfig) -> bool:
    backend = (config.ble_scan_backend or "").strip().lower()
    return backend in {"nrf", "nrf_dongle", "dongle", "pc_ble_driver"}


def build_ota_command(config: WorkstationConfig, ble_address: str = "") -> OtaCommand:
    repo = Path(config.firmware_repo)
    image = Path(config.ota_image_path)
    if _uses_nrf_dongle_backend(config):
        script = repo / "tools" / "ota_smp_dongle.py"
        argv = [
            sys.executable,
            str(script),
            str(image),
            "--name",
            config.ble_name,
            "--dongle-port",
            config.ble_dongle_port or "COM8",
            "--sd-version",
            config.ble_dongle_sd_version or "auto",
            "--profile",
            "safe",
            "--timeout",
            "30",
            "--first-timeout",
            "120",
            "--verify-after-reset",
            "--post-reset-delay",
            str(config.ota_reboot_wait_s),
            "--post-reset-timeout",
            "30",
        ]
        if config.nrf_connect_ble_path:
            argv.extend(["--nrf-connect-ble-path", config.nrf_connect_ble_path])
    else:
        script = repo / "tools" / "ota_smp_ble.py"
        argv = [sys.executable, str(script), str(image), "--name", config.ble_name]
    if ble_address:
        argv.extend(["--addr", ble_address])
    return OtaCommand(argv=argv, cwd=str(repo), script_name=script.name)


def run_ota(config: WorkstationConfig, ble_address: str, line_callback) -> int:
    command = build_ota_command(config, ble_address)
    line_callback("INFO", f"OTA backend script: {command.script_name}")
    line_callback("INFO", "OTA version gate disabled by plan; uploading configured image directly.")
    proc = subprocess.Popen(
        command.argv,
        cwd=command.cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        line_callback("OTA", line.rstrip())
    return proc.wait()
