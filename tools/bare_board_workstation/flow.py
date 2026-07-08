from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import BareBoardConfig
from .flash_runner import FlashOutcome, run_flash
from .records import RecordStorage
from .serial_runner import SerialOutcome, collect_serial_log


LineCallback = Callable[[str, str], None]
ProgressCallback = Callable[[str, str, str], None]
FlashRunner = Callable[[BareBoardConfig, LineCallback | None], FlashOutcome]
SerialRunner = Callable[[BareBoardConfig, LineCallback | None, threading.Event | None], SerialOutcome]


@dataclass
class FlowOutcome:
    ok: bool
    result: str
    message: str
    record_path: str
    flash: FlashOutcome | None
    serial: SerialOutcome | None
    elapsed_ms: int


def run_bare_board_test(
    config: BareBoardConfig,
    sn: str,
    line_callback: LineCallback | None = None,
    progress_callback: ProgressCallback | None = None,
    flash_runner: FlashRunner | None = None,
    serial_runner: SerialRunner | None = None,
    stop_event: threading.Event | None = None,
) -> FlowOutcome:
    started = time.monotonic()
    sn_value = sn.strip()
    ok, reason = config.validate_sn(sn_value)
    if not ok:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        return FlowOutcome(False, "NG", reason, "", None, None, elapsed_ms)

    record = RecordStorage(config.records_root).start_run(config, sn_value)
    stop = stop_event or threading.Event()
    flash_impl = flash_runner or run_flash
    serial_impl = serial_runner or collect_serial_log

    def emit(category: str, line: str) -> None:
        record.log(category, line)
        if line_callback is not None:
            line_callback(category, line)

    def progress(step: str, status: str, detail: str) -> None:
        record.log("STEP", f"{step}: {status} {detail}".rstrip())
        if progress_callback is not None:
            progress_callback(step, status, detail)

    try:
        progress("Flash", "RUN", config.flash_backend)
        flash = flash_impl(config, emit)
        record.log_metadata("flash_elapsed_ms", flash.elapsed_ms)
        record.log_metadata("flash_image_sha256", flash.image_sha256)
        if not flash.ok:
            progress("Flash", "NG", flash.message)
            record.finish("NG", f"flash failed: {flash.message}")
            elapsed_ms = int((time.monotonic() - started) * 1000)
            return FlowOutcome(False, "NG", f"flash failed: {flash.message}", str(record.path), flash, None, elapsed_ms)
        progress("Flash", "PASS", flash.message)

        wait_s = max(0.0, float(config.flash_after_wait_s or 0.0))
        if wait_s and not stop.is_set():
            progress("Wait", "RUN", f"{wait_s:.1f}s")
            time.sleep(wait_s)

        progress("Serial", "RUN", config.serial_port or "unconfigured")
        serial = serial_impl(config, emit, stop)
        progress("Serial", serial.result, serial.message)
        result = "PASS" if serial.ok else serial.result
        record.finish(result, serial.message)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        return FlowOutcome(serial.ok, result, serial.message, str(record.path), flash, serial, elapsed_ms)
    except Exception as exc:
        record.finish("NG", str(exc))
        elapsed_ms = int((time.monotonic() - started) * 1000)
        return FlowOutcome(False, "NG", str(exc), str(record.path), None, None, elapsed_ms)
    finally:
        try:
            record.close()
        except Exception:
            pass


def make_simulated_flash_runner(message: str = "simulated flash completed") -> FlashRunner:
    def _runner(config: BareBoardConfig, line_callback: LineCallback | None = None) -> FlashOutcome:
        from .flash_runner import FlashCommand

        started = time.monotonic()
        if line_callback is not None:
            line_callback("FLASH", "DRY-RUN: skip SWD flash")
        command = FlashCommand(
            backend="dry-run",
            argv=["dry-run"],
            cwd=str(Path.cwd()),
            image_path=str(config.flash_image_path or ""),
            image_sha256="",
            jlink_probe_id=str(config.jlink_probe_id or ""),
            env={},
        )
        elapsed_ms = int((time.monotonic() - started) * 1000)
        return FlashOutcome(True, "PASS", message, elapsed_ms, 0, command)

    return _runner


def make_simulated_serial_runner(lines: list[str] | None = None) -> SerialRunner:
    sample_lines = lines or ["BOOT bare-board-test", "SN READY", "RESULT:PASS"]

    def _runner(
        config: BareBoardConfig,
        line_callback: LineCallback | None = None,
        stop_event: threading.Event | None = None,
    ) -> SerialOutcome:
        from .serial_runner import collect_fake_serial_log

        return collect_fake_serial_log(config, sample_lines, line_callback=line_callback)

    return _runner
