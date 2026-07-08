from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .at_client import ATClient, CommandResult
from .config import WorkstationConfig
from .flash_runner import FlashOutcome


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
    }
    if outcome is not None:
        payload.update(
            {
                "result": outcome.result,
                "message": outcome.message,
                "elapsed_ms": outcome.elapsed_ms,
                "exit_code": outcome.exit_code,
                "image_sha256": outcome.image_sha256,
            }
        )
    return payload


def _parse_probe_ids(output: str) -> list[str]:
    probe_ids: list[str] = []
    for raw in output.splitlines():
        line = raw.strip()
        if line and any(ch.isdigit() for ch in line):
            probe_ids.append(line)
    return probe_ids


def precheck_flash_request(config: WorkstationConfig, *, sn_enabled: bool, dry_run: bool = False) -> FlashPrecheckResult:
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
        if not str(config.jlink_probe_id or "").strip():
            return FlashPrecheckResult(True, "WARN", "script backend will use the script's probe selection")
        return FlashPrecheckResult(True, "OK", "script backend precheck passed")

    if backend != "nrfjprog":
        return FlashPrecheckResult(False, "ERR", f"unsupported flash backend: {backend}")

    image_text = str(config.flash_image_path or "").strip()
    if not image_text:
        return FlashPrecheckResult(False, "ERR", "flash image is empty")
    image = Path(image_text)
    if not image.exists():
        return FlashPrecheckResult(False, "ERR", f"flash image not found: {image}")
    if not image.is_file():
        return FlashPrecheckResult(False, "ERR", f"flash image is not a file: {image}")

    probe_id = str(config.jlink_probe_id or "").strip()
    if probe_id:
        return FlashPrecheckResult(True, "OK", "J-Link probe ID configured")

    tool = str(config.nrfjprog_path or "nrfjprog").strip() or "nrfjprog"
    try:
        proc = subprocess.run(
            [tool, "--ids"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15.0,
        )
    except FileNotFoundError:
        return FlashPrecheckResult(False, "ERR", f"nrfjprog not found: {tool}")
    except subprocess.TimeoutExpired:
        return FlashPrecheckResult(False, "ERR", "nrfjprog --ids timeout")
    except Exception as exc:
        return FlashPrecheckResult(False, "ERR", f"nrfjprog --ids failed: {exc}")

    output = "\n".join(part for part in (proc.stdout, proc.stderr) if part)
    if proc.returncode != 0:
        return FlashPrecheckResult(False, "ERR", f"nrfjprog --ids exit={proc.returncode}: {output.strip()}")

    probe_ids = _parse_probe_ids(output)
    if len(probe_ids) > 1:
        message = "multiple J-Link probes detected; configure jlink_probe_id"
        if sn_enabled and not dry_run:
            return FlashPrecheckResult(False, "ERR", message, probe_ids)
        return FlashPrecheckResult(True, "WARN", message, probe_ids)

    if not probe_ids:
        return FlashPrecheckResult(True, "WARN", "nrfjprog --ids returned no probe ID")
    return FlashPrecheckResult(True, "OK", f"single J-Link probe detected: {probe_ids[0]}", probe_ids)


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
        f"{outcome.backend} exit_code={outcome.exit_code} sha256={outcome.image_sha256}",
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
