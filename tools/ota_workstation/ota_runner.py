from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import OtaConfig, PACKAGE_DIR, WORKSPACE_DIR


PROGRESS_RE = re.compile(r"\((?P<percent>\d+(?:\.\d+)?)%\)")
SAME_HASH_MARKERS = (
    "matches the active image",
    "same image hash",
    "uploaded image hash matches the active image",
)


@dataclass(frozen=True)
class OtaCommand:
    argv: list[str]
    cwd: str
    helper_name: str
    helper_path: str


@dataclass(frozen=True)
class OtaResult:
    status: str
    exit_code: int
    message: str


def _source_helpers() -> tuple[Path, Path]:
    windows_helper = WORKSPACE_DIR.parent / "axi-p1-embeded" / "tools" / "ota_smp_ble.py"
    dongle_helper = WORKSPACE_DIR / "tools" / "ota_smp_dongle.py"
    return windows_helper, dongle_helper


def _packaged_helpers() -> tuple[Path, Path]:
    app_dir = Path(sys.executable).resolve().parent
    return app_dir / "Axi OTA BLE Helper.exe", app_dir / "Axi OTA Dongle Helper.exe"


def _helper_argv(helper: Path) -> list[str]:
    if helper.suffix.lower() == ".py":
        return [sys.executable, str(helper)]
    return [str(helper)]


def build_ota_command(
    config: OtaConfig,
    address: str = "",
    *,
    windows_helper: Path | None = None,
    dongle_helper: Path | None = None,
) -> OtaCommand:
    if windows_helper is None or dongle_helper is None:
        defaults = _packaged_helpers() if getattr(sys, "frozen", False) else _source_helpers()
        windows_helper = windows_helper or defaults[0]
        dongle_helper = dongle_helper or defaults[1]

    backend = config.normalized_backend()
    helper = dongle_helper if backend == "nrf_dongle" else windows_helper
    argv = _helper_argv(helper)
    argv.extend(
        [
            str(Path(config.image_path)),
            "--name",
            config.ble_name.strip() or "AXI-P1-T",
            "--profile",
            config.profile,
        ]
    )
    target = str(address or config.ble_address or "").strip()
    if target:
        argv.extend(["--addr", target])
    if config.verify_after_reset:
        argv.extend(
            [
                "--verify-after-reset",
                "--post-reset-delay",
                str(float(config.reboot_wait_s)),
                "--post-reset-timeout",
                "30",
            ]
        )
    if backend == "nrf_dongle":
        argv.extend(
            [
                "--dongle-port",
                config.dongle_port.strip() or "COM8",
                "--sd-version",
                config.dongle_sd_version.strip() or "auto",
                "--timeout",
                "30",
                "--first-timeout",
                "120",
            ]
        )
        if config.nrf_connect_ble_path.strip():
            argv.extend(["--nrf-connect-ble-path", config.nrf_connect_ble_path.strip()])
    elif config.ble_pairing_enabled:
        argv.append("--pair")
    return OtaCommand(
        argv=argv,
        cwd=str(PACKAGE_DIR),
        helper_name=helper.name,
        helper_path=str(helper),
    )


def parse_progress(line: str) -> float | None:
    match = PROGRESS_RE.search(line)
    if not match:
        return None
    return min(100.0, max(0.0, float(match.group("percent"))))


class OtaRunner:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._process: subprocess.Popen[str] | None = None
        self._cancel_requested = False

    @property
    def running(self) -> bool:
        with self._lock:
            return self._process is not None and self._process.poll() is None

    def cancel(self) -> bool:
        with self._lock:
            proc = self._process
            if proc is None or proc.poll() is not None:
                return False
            self._cancel_requested = True
            proc.terminate()
        try:
            proc.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            proc.kill()
        return True

    def run(
        self,
        config: OtaConfig,
        address: str,
        line_callback: Callable[[str], None],
        progress_callback: Callable[[float], None] | None = None,
    ) -> OtaResult:
        command = build_ota_command(config, address)
        helper = Path(command.helper_path)
        if not helper.is_file():
            raise FileNotFoundError(f"找不到 OTA Helper：{helper}")

        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                raise RuntimeError("已有 OTA 任务正在运行")
            self._cancel_requested = False
            proc = subprocess.Popen(
                command.argv,
                cwd=command.cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=creationflags,
            )
            self._process = proc

        same_hash = False
        try:
            assert proc.stdout is not None
            for raw in proc.stdout:
                line = raw.rstrip()
                if line:
                    line_callback(line)
                lowered = line.lower()
                same_hash = same_hash or any(marker in lowered for marker in SAME_HASH_MARKERS)
                progress = parse_progress(line)
                if progress is not None and progress_callback is not None:
                    progress_callback(progress)
            code = proc.wait()
            with self._lock:
                cancelled = self._cancel_requested
            if cancelled:
                return OtaResult("cancelled", code, "OTA 已由操作员中止")
            if code == 0:
                return OtaResult("success", code, "OTA 升级及重启后校验完成")
            if same_hash:
                return OtaResult("same_hash", code, "设备已是相同固件，链路正常但未发生真实升级")
            return OtaResult("failed", code, f"OTA Helper 异常退出（代码 {code}）")
        finally:
            with self._lock:
                if self._process is proc:
                    self._process = None
