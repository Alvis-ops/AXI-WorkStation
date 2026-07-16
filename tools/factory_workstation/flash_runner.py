from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .config import WorkstationConfig


FlashLogCallback = Callable[[str, str], None]
JLINK_ERROR_256 = "JLinkARM.dll reported error -256"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


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
    output: str = ""

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


@dataclass
class JLinkDetectResult:
    ok: bool
    level: str
    message: str
    probe_ids: list[str] = field(default_factory=list)
    nrfjprog_path: str = ""
    nrfjprog_version: str = ""
    exit_code: int | None = None
    raw_output: str = ""


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


def _jlink_dll_args(config: WorkstationConfig, *, require_file: bool = True) -> list[str]:
    dll_path = str(config.jlink_dll_path or "").strip()
    if not dll_path:
        raise ValueError("J-Link DLL is not configured; run the complete offline installer again")
    if require_file:
        dll_path = str(_require_file(dll_path, "J-Link DLL"))
    return ["--jdll", dll_path]


def build_flash_command(config: WorkstationConfig, *, probe_id_override: str = "") -> FlashCommand:
    backend = str(config.flash_backend or "nrfjprog").strip().lower()
    env = os.environ.copy()
    probe_id = str(probe_id_override or config.jlink_probe_id or "").strip()
    repo = str(config.firmware_repo or "").strip()
    cwd = repo if repo and Path(repo).exists() else str(Path.cwd())

    if backend == "nrfjprog":
        image = _require_file(config.flash_image_path, "flash image")
        tool = str(config.nrfjprog_path or "nrfjprog").strip() or "nrfjprog"
        argv = [tool, "--program", str(image), "--chiperase", *_jlink_dll_args(config)]
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


def _parse_probe_ids(output: str) -> list[str]:
    probe_ids: list[str] = []
    for raw in output.splitlines():
        line = raw.strip()
        if line.isdigit() and 6 <= len(line) <= 16:
            probe_ids.append(line)
    return probe_ids


def _run_nrfjprog_tool(tool: str, args: list[str], timeout_s: float = 15.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [tool, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_s,
        creationflags=CREATE_NO_WINDOW,
    )


def _detect_jlink_probes(config: WorkstationConfig, configured_probe: str) -> JLinkDetectResult:
    tool = str(config.nrfjprog_path or "nrfjprog").strip() or "nrfjprog"
    try:
        jlink_dll_args = _jlink_dll_args(config)
    except (FileNotFoundError, ValueError) as exc:
        return JLinkDetectResult(False, "ERR", str(exc), nrfjprog_path=tool)

    try:
        version_proc = _run_nrfjprog_tool(tool, ["--version", *jlink_dll_args])
        ids_proc = _run_nrfjprog_tool(tool, ["--ids", *jlink_dll_args])
    except FileNotFoundError:
        return JLinkDetectResult(False, "ERR", f"nrfjprog not found: {tool}", nrfjprog_path=tool)
    except subprocess.TimeoutExpired:
        return JLinkDetectResult(False, "ERR", "nrfjprog probe precheck timed out", nrfjprog_path=tool)
    except Exception as exc:
        return JLinkDetectResult(False, "ERR", f"nrfjprog probe precheck failed: {exc}", nrfjprog_path=tool)

    version_output = "\n".join(part for part in (version_proc.stdout, version_proc.stderr) if part).strip()
    ids_output = "\n".join(part for part in (ids_proc.stdout, ids_proc.stderr) if part).strip()
    raw_output = "\n".join(part for part in (version_output, ids_output) if part)
    probe_ids = _parse_probe_ids(ids_output)
    version_line = next(
        (line.strip() for line in version_output.splitlines() if line.strip().lower().startswith("nrfjprog version:")),
        version_output,
    )

    if version_proc.returncode != 0 or JLINK_ERROR_256 in raw_output:
        return JLinkDetectResult(
            False,
            "ERR",
            f"Pinned J-Link DLL is incompatible with nrfjprog: {config.jlink_dll_path}",
            probe_ids=probe_ids,
            nrfjprog_path=tool,
            nrfjprog_version=version_line,
            exit_code=version_proc.returncode,
            raw_output=raw_output,
        )
    if ids_proc.returncode != 0:
        return JLinkDetectResult(
            False,
            "ERR",
            translate_flash_failure(ids_proc.returncode, ids_output),
            probe_ids=probe_ids,
            nrfjprog_path=tool,
            nrfjprog_version=version_line,
            exit_code=ids_proc.returncode,
            raw_output=raw_output,
        )
    if not probe_ids:
        return JLinkDetectResult(
            False,
            "ERR",
            "未检测到 J-Link 探头。请检查 J-Link USB 连接、驱动和供电后重试。",
            nrfjprog_path=tool,
            nrfjprog_version=version_line,
            exit_code=41,
            raw_output=raw_output,
        )
    if configured_probe:
        if configured_probe not in probe_ids:
            return JLinkDetectResult(
                False,
                "ERR",
                f"已配置的 J-Link SNR {configured_probe} 未检测到；当前探头: {', '.join(probe_ids)}",
                probe_ids=probe_ids,
                nrfjprog_path=tool,
                nrfjprog_version=version_line,
                exit_code=41,
                raw_output=raw_output,
            )
        return JLinkDetectResult(
            True,
            "OK",
            f"已检测到配置的 J-Link SNR {configured_probe}",
            probe_ids=probe_ids,
            nrfjprog_path=tool,
            nrfjprog_version=version_line,
            exit_code=0,
            raw_output=raw_output,
        )
    if len(probe_ids) > 1:
        return JLinkDetectResult(
            False,
            "ERR",
            f"检测到多个 J-Link 探头: {', '.join(probe_ids)}；请在设置中指定 J-Link SNR",
            probe_ids=probe_ids,
            nrfjprog_path=tool,
            nrfjprog_version=version_line,
            exit_code=41,
            raw_output=raw_output,
        )
    return JLinkDetectResult(
        True,
        "OK",
        f"检测到单个 J-Link SNR {probe_ids[0]}，将自动选用",
        probe_ids=probe_ids,
        nrfjprog_path=tool,
        nrfjprog_version=version_line,
        exit_code=0,
        raw_output=raw_output,
    )


def scan_jlink_probes(config: WorkstationConfig) -> JLinkDetectResult:
    """Enumerate attached probes without rejecting a stale configured SNR."""
    return _detect_jlink_probes(config, "")


def detect_jlink_probes(config: WorkstationConfig) -> JLinkDetectResult:
    """Enumerate probes and validate the SNR used by either flash backend."""
    backend = str(config.flash_backend or "nrfjprog").strip().lower()
    if backend not in {"nrfjprog", "script"}:
        return JLinkDetectResult(False, "ERR", f"unsupported flash backend: {backend}")
    return _detect_jlink_probes(config, str(config.jlink_probe_id or "").strip())


def precheck_nrfjprog_probe(config: WorkstationConfig) -> tuple[bool, str, list[str]]:
    result = detect_jlink_probes(config)
    return result.ok, result.message if not result.ok or result.level == "WARN" else "", result.probe_ids


def translate_flash_failure(exit_code: int | None, output: str = "") -> str:
    if exit_code == 41:
        return "未检测到 J-Link 探头（nrfjprog exit 41）。请检查 J-Link USB 连接、驱动和供电后重试。"
    lowered = (output or "").lower()
    if exit_code == 33 or "error -102" in lowered or "unable to connect to a debugger" in lowered:
        return "无法连接目标芯片（nrfjprog exit 33）。请检查 SWD 接线、目标板供电和调试口占用。"
    if exit_code is None:
        return "flash command failed before an exit code was returned"
    return f"flash command failed with exit code {exit_code}"


def run_flash(config: WorkstationConfig, line_callback: FlashLogCallback | None = None) -> FlashOutcome:
    started = time.monotonic()
    command: FlashCommand | None = None
    try:
        detect_result = detect_jlink_probes(config)
        if line_callback is not None:
            line_callback("FLASH", f"J-Link precheck: {detect_result.message}")
            for raw in detect_result.raw_output.splitlines():
                line = raw.rstrip("\r\n")
                if line:
                    line_callback("PREFLIGHT", line)
        if not detect_result.ok:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            return FlashOutcome(
                False,
                "NG",
                detect_result.message,
                elapsed_ms,
                detect_result.exit_code if detect_result.exit_code is not None else 41,
                command,
                detect_result.raw_output,
            )

        probe_id_override = ""
        if not str(config.jlink_probe_id or "").strip() and len(detect_result.probe_ids) == 1:
            probe_id_override = detect_result.probe_ids[0]
        command = build_flash_command(config, probe_id_override=probe_id_override)
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
            creationflags=CREATE_NO_WINDOW,
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
            return FlashOutcome(False, "NG", f"flash timeout after {timeout_s:.1f}s", elapsed_ms, proc.returncode, command, output)

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
            return FlashOutcome(True, "PASS", "flash completed", elapsed_ms, exit_code, command, output or "")
        return FlashOutcome(False, "NG", translate_flash_failure(exit_code, output or ""), elapsed_ms, exit_code, command, output or "")
    except Exception as exc:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        if line_callback is not None:
            line_callback("FLASH", f"ERROR: {exc}")
        return FlashOutcome(False, "NG", str(exc), elapsed_ms, None, command)
