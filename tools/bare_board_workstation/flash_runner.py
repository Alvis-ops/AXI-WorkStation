from __future__ import annotations

import hashlib
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .config import BareBoardConfig


FlashLogCallback = Callable[[str, str], None]
JLINK_ERROR_256 = "JLinkARM.dll reported error -256"
# nrfjprog --family accepts Nordic CLI names (NRF51/52/53/54L/91, AUTO, UNKNOWN).
# NRF54L15_XXAA is a Zephyr/soc label and must not be passed to nrfjprog.
DEFAULT_NRFJPROG_FAMILY = ""
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


def _jlink_dll_args(config: BareBoardConfig, *, require_file: bool = True) -> list[str]:
    dll_path = str(config.jlink_dll_path or "").strip()
    if not dll_path:
        return []
    if require_file:
        dll_path = str(_require_file(dll_path, "J-Link DLL"))
    return ["--jdll", dll_path]


def build_flash_command(config: BareBoardConfig, *, probe_id_override: str = "") -> FlashCommand:
    backend = str(config.flash_backend or "nrfjprog").strip().lower()
    env = os.environ.copy()
    probe_id = str(probe_id_override or config.jlink_probe_id or "").strip()
    repo = str(config.firmware_repo or "").strip()
    cwd = repo if repo and Path(repo).exists() else str(Path.cwd())

    if backend == "nrfjprog":
        image = _require_file(config.flash_image_path, "flash image")
        tool = str(config.nrfjprog_path or "nrfjprog").strip() or "nrfjprog"
        argv = [tool, "--program", str(image), "--chiperase", *_jlink_dll_args(config)]
        family = str(config.nrfjprog_family or DEFAULT_NRFJPROG_FAMILY).strip()
        if family:
            argv.extend(["--family", family])
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
        if config.flash_image_path and Path(config.flash_image_path).exists() and Path(config.flash_image_path).is_file():
            image_path = str(Path(config.flash_image_path))
            image_hash = file_sha256(image_path)
        if image_path:
            env["BARE_BOARD_FLASH_IMAGE"] = image_path
        if probe_id:
            env["BARE_BOARD_JLINK_ID"] = probe_id
        family = str(config.nrfjprog_family or DEFAULT_NRFJPROG_FAMILY).strip()
        if family:
            env["BARE_BOARD_NRFJPROG_FAMILY"] = family
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


def detect_jlink_probes(config: BareBoardConfig) -> JLinkDetectResult:
    backend = str(config.flash_backend or "nrfjprog").strip().lower()
    if backend == "script":
        return JLinkDetectResult(
            ok=True,
            level="WARN",
            message="当前为 script 烧录方式，请由脚本自行选择 J-Link 探针",
        )
    if backend != "nrfjprog":
        return JLinkDetectResult(False, "ERR", f"unsupported flash backend: {backend}")

    tool = str(config.nrfjprog_path or "nrfjprog").strip() or "nrfjprog"
    configured_probe = str(config.jlink_probe_id or "").strip()
    try:
        jlink_dll_args = _jlink_dll_args(config)
    except (FileNotFoundError, ValueError) as exc:
        return JLinkDetectResult(False, "ERR", str(exc), nrfjprog_path=tool)
    try:
        version_proc = _run_nrfjprog_tool(tool, ["--version", *jlink_dll_args])
        ids_proc = _run_nrfjprog_tool(tool, ["--ids", *jlink_dll_args])
    except FileNotFoundError:
        return JLinkDetectResult(
            False,
            "ERR",
            f"未找到 nrfjprog: {tool}。新电脑请先以管理员身份运行 install_offline_win10.cmd",
            nrfjprog_path=tool,
        )
    except subprocess.TimeoutExpired:
        return JLinkDetectResult(False, "ERR", "nrfjprog 检测超时", nrfjprog_path=tool)
    except Exception as exc:
        return JLinkDetectResult(False, "ERR", f"nrfjprog 检测失败: {exc}", nrfjprog_path=tool)

    version_output = "\n".join(part for part in (version_proc.stdout, version_proc.stderr) if part).strip()
    ids_output = "\n".join(part for part in (ids_proc.stdout, ids_proc.stderr) if part).strip()
    raw_output = "\n".join(part for part in (version_output, ids_output) if part)
    probe_ids = _parse_probe_ids(ids_output)
    version_line = next(
        (line.strip() for line in version_output.splitlines() if line.strip().lower().startswith("nrfjprog version:")),
        version_output,
    )

    if version_proc.returncode != 0 or JLINK_ERROR_256 in raw_output:
        dll_hint = str(config.jlink_dll_path or "自动选择的 J-Link DLL")
        return JLinkDetectResult(
            False,
            "ERR",
            f"J-Link DLL 与 nrfjprog 不兼容: {dll_hint}。请重新运行 r13 离线安装包",
            probe_ids=probe_ids,
            nrfjprog_path=tool,
            nrfjprog_version=version_line,
            exit_code=version_proc.returncode,
            raw_output=raw_output,
        )

    if configured_probe:
        if probe_ids and configured_probe not in probe_ids:
            return JLinkDetectResult(
                False,
                "ERR",
                f"配置的 J-Link SN {configured_probe} 未找到；当前检测到: {', '.join(probe_ids)}",
                probe_ids=probe_ids,
                nrfjprog_path=tool,
                nrfjprog_version=version_line,
                exit_code=ids_proc.returncode,
                raw_output=raw_output,
            )
        if not probe_ids:
            message = (
                "未检测到 J-Link 探针。请连接 J-Link USB，确认设备管理器中有 SEGGER，"
                "必要时以管理员身份重新运行 install_offline_win10.cmd 后重新插拔探针"
            )
            if ids_proc.returncode != 0 and ids_output:
                message = f"{message}（exit={ids_proc.returncode}）"
            return JLinkDetectResult(
                False,
                "ERR",
                message,
                probe_ids=probe_ids,
                nrfjprog_path=tool,
                nrfjprog_version=version_line,
                exit_code=ids_proc.returncode,
                raw_output=raw_output,
            )
        return JLinkDetectResult(
            True,
            "OK",
            f"已找到配置的 J-Link SN {configured_probe}",
            probe_ids=probe_ids,
            nrfjprog_path=tool,
            nrfjprog_version=version_line,
            exit_code=ids_proc.returncode,
            raw_output=raw_output,
        )

    if not probe_ids:
        message = (
            "未检测到 J-Link 探针。请连接 J-Link USB，确认设备管理器中有 SEGGER，"
            "必要时以管理员身份重新运行 install_offline_win10.cmd 后重新插拔探针"
        )
        if ids_proc.returncode != 0 and ids_output:
            message = f"{message}（exit={ids_proc.returncode}）"
        return JLinkDetectResult(
            False,
            "ERR",
            message,
            probe_ids=probe_ids,
            nrfjprog_path=tool,
            nrfjprog_version=version_line,
            exit_code=ids_proc.returncode,
            raw_output=raw_output,
        )
    if len(probe_ids) > 1:
        return JLinkDetectResult(
            True,
            "WARN",
            f"检测到多个 J-Link 探针: {', '.join(probe_ids)}；请在 J-Link ID 中指定 SN",
            probe_ids=probe_ids,
            nrfjprog_path=tool,
            nrfjprog_version=version_line,
            exit_code=ids_proc.returncode,
            raw_output=raw_output,
        )
    return JLinkDetectResult(
        True,
        "OK",
        f"检测到 J-Link SN {probe_ids[0]}",
        probe_ids=probe_ids,
        nrfjprog_path=tool,
        nrfjprog_version=version_line,
        exit_code=ids_proc.returncode,
        raw_output=raw_output,
    )


def precheck_nrfjprog_probe(config: BareBoardConfig) -> tuple[bool, str, list[str]]:
    result = detect_jlink_probes(config)
    if result.ok:
        if result.level == "WARN" and len(result.probe_ids) > 1:
            return False, result.message, result.probe_ids
        return True, result.message if result.level == "WARN" else "", result.probe_ids
    return False, result.message, result.probe_ids


def run_flash(config: BareBoardConfig, line_callback: FlashLogCallback | None = None) -> FlashOutcome:
    started = time.monotonic()
    command: FlashCommand | None = None
    try:
        ok, message, probe_ids = precheck_nrfjprog_probe(config)
        if not ok:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            if line_callback is not None:
                line_callback("FLASH", f"ERROR: {message}")
            return FlashOutcome(False, "NG", message, elapsed_ms, 41, command)
        if line_callback is not None and probe_ids:
            line_callback("FLASH", f"J-Link probe detected: {', '.join(probe_ids)}")

        probe_id_override = ""
        if not str(config.jlink_probe_id or "").strip() and len(probe_ids) == 1:
            probe_id_override = probe_ids[0]

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
        timeout_s = max(1.0, float(config.flash_timeout_s or 180.0))
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
        hint = ""
        combined = output or ""
        if exit_code == 33 or "error -102" in combined.lower() or "unable to connect to a debugger" in combined.lower():
            family = str(config.nrfjprog_family or "").strip() or "AUTO"
            hint = (
                f" 提示: 检查 SWD 接线/板子上电；nrfjprog family 留空(AUTO)或填 NRF54L，"
                "不要用 Zephyr 名 NRF54L15_XXAA"
            )
        message = f"flash command failed with exit code {exit_code}{hint}"
        return FlashOutcome(False, "NG", message, elapsed_ms, exit_code, command)
    except Exception as exc:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        if line_callback is not None:
            line_callback("FLASH", f"ERROR: {exc}")
        return FlashOutcome(False, "NG", str(exc), elapsed_ms, None, command)
