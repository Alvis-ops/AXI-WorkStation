from __future__ import annotations

import hashlib
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import WorkstationConfig


FlashLogCallback = Callable[[str, str], None]
JLINK_ERROR_256 = "JLinkARM.dll reported error -256"


def _to_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


@dataclass
class FlashCommand:
    backend: str
    argv: list[str]
    cwd: str
    image_path: str
    image_sha256: str
    jlink_probe_id: str
    env: dict[str, str]

    @property
    def display(self) -> str:
        return subprocess.list2cmdline(self.argv)


@dataclass
class FlashOutcome:
    ok: bool
    result: str
    message: str
    elapsed_ms: int
    exit_code: int | None
    command: FlashCommand | None = None

    @property
    def image_sha256(self) -> str:
        return self.command.image_sha256 if self.command is not None else ""

    @property
    def image_path(self) -> str:
        return self.command.image_path if self.command is not None else ""

    @property
    def backend(self) -> str:
        return self.command.backend if self.command is not None else ""

    @property
    def jlink_probe_id(self) -> str:
        return self.command.jlink_probe_id if self.command is not None else ""


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_file(path_text: str, label: str) -> Path:
    if not path_text:
        raise ValueError(f"{label} is empty")
    path = Path(path_text)
    if not path.exists():
        raise FileNotFoundError(f"{label} not found: {path}")
    if not path.is_file():
        raise ValueError(f"{label} is not a file: {path}")
    return path


def build_flash_command(config: WorkstationConfig) -> FlashCommand:
    backend = str(config.flash_backend or "nrfjprog").strip().lower()
    env = os.environ.copy()
    probe_id = str(config.jlink_probe_id or "").strip()
    repo = str(config.firmware_repo or "").strip()
    cwd = repo if repo and Path(repo).exists() else str(Path.cwd())

    if backend == "nrfjprog":
        image = _require_file(config.flash_image_path, "flash image")
        tool = str(config.nrfjprog_path or "nrfjprog").strip() or "nrfjprog"
        argv = [tool, "--program", str(image), "--chiperase"]
        if config.flash_verify:
            argv.append("--verify")
        argv.append("--reset")
        if probe_id:
            argv.extend(["--snr", probe_id])
        return FlashCommand(
            backend=backend,
            argv=argv,
            cwd=cwd,
            image_path=str(image),
            image_sha256=file_sha256(image),
            jlink_probe_id=probe_id,
            env=env,
        )

    if backend == "script":
        script = _require_file(config.flash_script_path, "flash script")
        image_hash = ""
        image_path = ""
        if config.flash_image_path and Path(config.flash_image_path).exists():
            image_path = str(Path(config.flash_image_path))
            image_hash = file_sha256(image_path)
        if probe_id:
            env["POC3A_JLINK_ID"] = probe_id
        argv = [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
        ]
        return FlashCommand(
            backend=backend,
            argv=argv,
            cwd=str(script.parent),
            image_path=image_path,
            image_sha256=image_hash,
            jlink_probe_id=probe_id,
            env=env,
        )

    raise ValueError(f"unsupported flash backend: {backend}")


def run_flash(config: WorkstationConfig, line_callback: FlashLogCallback | None = None) -> FlashOutcome:
    started = time.monotonic()
    command: FlashCommand | None = None
    try:
        command = build_flash_command(config)
        if line_callback is not None:
            line_callback("FLASH", command.display)
        proc = subprocess.Popen(
            command.argv,
            cwd=command.cwd,
            env=command.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        timeout_s = max(1.0, float(getattr(config, "flash_timeout_s", 180.0) or 180.0))
        try:
            output, _ = proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired as exc:
            partial = _to_text(exc.output)
            proc.terminate()
            try:
                rest, _ = proc.communicate(timeout=5.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                rest, _ = proc.communicate()
            output = f"{partial}{_to_text(rest)}"
            elapsed_ms = int((time.monotonic() - started) * 1000)
            if line_callback is not None:
                for raw in output.splitlines():
                    line = raw.rstrip("\r\n")
                    if line:
                        line_callback("FLASH", line)
                line_callback("FLASH", f"ERROR: flash timeout after {timeout_s:.1f}s")
            return FlashOutcome(False, "NG", f"flash timeout after {timeout_s:.1f}s", elapsed_ms, proc.returncode, command)

        exit_code = proc.returncode
        if line_callback is not None:
            for raw in (output or "").splitlines():
                line = raw.rstrip("\r\n")
                if not line:
                    continue
                level = "FLASH_WARN" if exit_code == 0 and JLINK_ERROR_256 in line else "FLASH"
                line_callback(level, line)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        if exit_code == 0:
            return FlashOutcome(True, "PASS", "flash completed", elapsed_ms, exit_code, command)
        return FlashOutcome(False, "NG", f"flash command failed with exit code {exit_code}", elapsed_ms, exit_code, command)
    except Exception as exc:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        if line_callback is not None:
            line_callback("FLASH", f"ERROR: {exc}")
        return FlashOutcome(False, "NG", str(exc), elapsed_ms, None, command)
