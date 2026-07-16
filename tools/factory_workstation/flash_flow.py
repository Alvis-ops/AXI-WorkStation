from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .at_client import ATClient, CommandResult
from .config import WorkstationConfig
from .flash_runner import FlashOutcome, detect_jlink_probes


FlashRunner = Callable[[WorkstationConfig, Callable[[str, str], None] | None], FlashOutcome]
ProgressCallback = Callable[[int, str, str, str], None]
FlashLogCallback = Callable[[str, str], None]


@dataclass
class FlashPrecheckResult:
    ok: bool
    level: str
    message: str
    probe_ids: list[str] = field(default_factory=list)


def flash_payload(config: WorkstationConfig, outcome: FlashOutcome | None = None) -> dict:
    payload = {
        "flash_backend": config.flash_backend,
        "flash_image_path": config.flash_image_path,
        "jlink_probe_id": config.jlink_probe_id,
        "jlink_dll_path": config.jlink_dll_path,
    }
    if outcome is not None:
        payload.update(
            {
                "result": outcome.result,
                "message": outcome.message,
                "elapsed_ms": outcome.elapsed_ms,
                "exit_code": outcome.exit_code,
                "image_sha256": outcome.image_sha256,
                "selected_jlink_probe_id": outcome.jlink_probe_id,
                "flash_command": outcome.command.display if outcome.command is not None else "",
                "flash_output": outcome.output,
            }
        )
    return payload


def precheck_flash_request(config: WorkstationConfig, *, sn_enabled: bool, dry_run: bool = False) -> FlashPrecheckResult:
    """Validate input files and use the exact same J-Link policy as flashing."""
    backend = str(config.flash_backend or "nrfjprog").strip().lower()
    if backend == "script":
        script_text = str(config.flash_script_path or "").strip()
        if not script_text:
            return FlashPrecheckResult(False, "ERR", "flash script is empty")
        script = Path(script_text)
        if not script.exists():
            return FlashPrecheckResult(False, "ERR", f"flash script not found: {script}")
        if not script.is_file():
            return FlashPrecheckResult(False, "ERR", f"flash script is not a file: {script}")
    elif backend == "nrfjprog":
        image_text = str(config.flash_image_path or "").strip()
        if not image_text:
            return FlashPrecheckResult(False, "ERR", "flash image is empty")
        image = Path(image_text)
        if not image.exists():
            return FlashPrecheckResult(False, "ERR", f"flash image not found: {image}")
        if not image.is_file():
            return FlashPrecheckResult(False, "ERR", f"flash image is not a file: {image}")
    else:
        return FlashPrecheckResult(False, "ERR", f"unsupported flash backend: {backend}")

    # SN recording does not affect probe safety. Every flash requires a
    # configured SNR or exactly one detected probe.
    _ = sn_enabled, dry_run
    result = detect_jlink_probes(config)
    return FlashPrecheckResult(result.ok, result.level, result.message, result.probe_ids)


def record_flash_step(
    config: WorkstationConfig,
    record,
    progress: ProgressCallback,
    flash_runner: FlashRunner,
    *,
    step_index: int = 1,
    station_type: str = "half",
    line_callback: FlashLogCallback | None = None,
) -> FlashOutcome:
    label = "Firmware flash"
    record.start_step(step_index, label, "flash")
    record.log_event("flash_start", flash_payload(config))
    progress(step_index, label, "RUN", f"{config.flash_backend} {Path(config.flash_image_path).name}")

    def flash_log(direction: str, line: str) -> None:
        if line_callback is not None:
            line_callback(direction, line)
        record.log_event("flash_log", {"direction": direction, "line": line}, line)

    outcome = flash_runner(config, flash_log)
    record.log_event("flash_end", flash_payload(config, outcome))
    record.log_item(
        station_type,
        label,
        "flash",
        outcome.result,
        outcome.elapsed_ms,
        "" if outcome.ok else outcome.message,
        (
            f"{outcome.backend} exit_code={outcome.exit_code} snr={outcome.jlink_probe_id} "
            f"sha256={outcome.image_sha256} command="
            f"{outcome.command.display if outcome.command is not None else ''}"
        ),
    )
    progress(step_index, label, outcome.result, f"{outcome.elapsed_ms / 1000:.1f}s | {outcome.message}")
    return outcome


def probe_at_client(client: ATClient, *, at_timeout_s: float = 8.0, ver_timeout_s: float = 8.0) -> tuple[bool, int, str, str, list[CommandResult]]:
    started = time.monotonic()
    results: list[CommandResult] = []
    at = client.send_command("AT", at_timeout_s)
    results.append(at)
    ver = client.send_command("AT+VER?", ver_timeout_s) if at.ok else None
    if ver is not None:
        results.append(ver)
    elapsed_ms = int((time.monotonic() - started) * 1000)
    ok = bool(at.ok and ver is not None and ver.ok)
    lines = list(at.lines)
    if ver is not None:
        lines.extend(ver.lines)
    detail = " ; ".join(lines[-4:])
    reason = "" if ok else "probe failed"
    return ok, elapsed_ms, detail, reason, results
