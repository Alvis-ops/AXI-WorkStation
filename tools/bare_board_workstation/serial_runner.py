from __future__ import annotations

import threading
import time
import re
from dataclasses import dataclass, field
from typing import Callable, Iterable, Protocol

from .config import BareBoardConfig


SerialLogCallback = Callable[[str, str], None]


@dataclass
class SerialPortInfo:
    device: str
    description: str
    hwid: str = ""


@dataclass
class SerialOutcome:
    ok: bool
    result: str
    message: str
    elapsed_ms: int
    lines: list[str] = field(default_factory=list)


class SerialLike(Protocol):
    def write(self, data: bytes) -> int | None:
        ...

    def flush(self) -> None:
        ...

    def read(self, size: int) -> bytes:
        ...

    def close(self) -> None:
        ...


def list_serial_ports() -> list[SerialPortInfo]:
    try:
        from serial.tools import list_ports
    except ImportError:
        return []
    ports = []
    for port in list_ports.comports():
        ports.append(SerialPortInfo(port.device, port.description or port.device, port.hwid or ""))
    return ports


def _pattern_matches(line: str, pattern: str) -> bool:
    value = pattern.strip()
    if not value:
        return False
    if value.startswith("re:"):
        return re.search(value[3:], line, flags=re.IGNORECASE) is not None
    upper_line = line.upper()
    upper_pattern = value.upper()
    if re.fullmatch(r"[A-Z0-9_]+", upper_pattern):
        expr = rf"(?<![A-Z0-9_]){re.escape(upper_pattern)}(?![A-Z0-9_])"
        return re.search(expr, upper_line) is not None
    return upper_pattern in upper_line


def _contains_any(line: str, patterns: Iterable[str]) -> bool:
    return any(_pattern_matches(line, str(pattern)) for pattern in patterns)


def _pop_buffered_line(rx: bytearray) -> str | None:
    while b"\n" in rx:
        raw, _, rest = rx.partition(b"\n")
        rx[:] = rest
        line = raw.decode("utf-8", errors="replace").strip()
        if line:
            return line
    return None


def _open_serial(config: BareBoardConfig) -> SerialLike:
    if not config.serial_port:
        raise RuntimeError("serial_port is empty")
    try:
        import serial
    except ImportError as exc:
        raise RuntimeError("pyserial is required: pip install pyserial") from exc
    return serial.Serial(config.serial_port, baudrate=int(config.serial_baudrate), timeout=0.05)


def collect_serial_log(
    config: BareBoardConfig,
    line_callback: SerialLogCallback | None = None,
    stop_event: threading.Event | None = None,
    serial_factory: Callable[[BareBoardConfig], SerialLike] | None = None,
) -> SerialOutcome:
    started = time.monotonic()
    lines: list[str] = []
    rx = bytearray()
    port: SerialLike | None = None
    stop = stop_event or threading.Event()

    try:
        port = (serial_factory or _open_serial)(config)
        try:
            reset = getattr(port, "reset_input_buffer")
            reset()
        except Exception:
            pass
        wait_s = max(0.0, float(config.serial_open_wait_s or 0.0))
        if wait_s:
            time.sleep(wait_s)

        command = str(config.test_start_command or "").strip()
        if command:
            port.write((command.rstrip("\r\n") + "\r\n").encode("ascii", errors="replace"))
            port.flush()
            if line_callback is not None:
                line_callback("SERIAL_TX", command)

        deadline = time.monotonic() + max(1.0, float(config.serial_timeout_s or 60.0))
        while time.monotonic() < deadline and not stop.is_set():
            line = _pop_buffered_line(rx)
            if line is None:
                chunk = port.read(256)
                if chunk:
                    rx.extend(chunk)
                    line = _pop_buffered_line(rx)
                else:
                    time.sleep(0.01)
                    continue
            if line is None:
                continue

            lines.append(line)
            if line_callback is not None:
                line_callback("SERIAL_RX", line)
            if _contains_any(line, config.fail_patterns):
                elapsed_ms = int((time.monotonic() - started) * 1000)
                return SerialOutcome(False, "NG", f"fail pattern matched: {line}", elapsed_ms, lines)
            if _contains_any(line, config.pass_patterns):
                elapsed_ms = int((time.monotonic() - started) * 1000)
                return SerialOutcome(True, "PASS", f"pass pattern matched: {line}", elapsed_ms, lines)
            if _contains_any(line, config.end_patterns):
                elapsed_ms = int((time.monotonic() - started) * 1000)
                return SerialOutcome(False, "NG", f"test ended without pass pattern: {line}", elapsed_ms, lines)

        elapsed_ms = int((time.monotonic() - started) * 1000)
        if stop.is_set():
            return SerialOutcome(False, "CANCELLED", "serial collection cancelled", elapsed_ms, lines)
        return SerialOutcome(False, "NG", f"serial timeout after {config.serial_timeout_s:.1f}s", elapsed_ms, lines)
    except Exception as exc:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        if line_callback is not None:
            line_callback("SERIAL", f"ERROR: {exc}")
        return SerialOutcome(False, "NG", str(exc), elapsed_ms, lines)
    finally:
        if port is not None:
            try:
                port.close()
            except Exception:
                pass


class FakeSerial:
    def __init__(self, lines: Iterable[str]) -> None:
        payload = "".join(line if line.endswith("\n") else f"{line}\n" for line in lines)
        self._data = bytearray(payload.encode("utf-8"))
        self.writes: list[str] = []

    def write(self, data: bytes) -> int:
        self.writes.append(data.decode("ascii", errors="replace").strip())
        return len(data)

    def flush(self) -> None:
        return None

    def read(self, size: int) -> bytes:
        if not self._data:
            time.sleep(0.01)
            return b""
        chunk = bytes(self._data[:size])
        del self._data[:size]
        return chunk

    def close(self) -> None:
        return None

    def reset_input_buffer(self) -> None:
        return None


def collect_fake_serial_log(
    config: BareBoardConfig,
    lines: Iterable[str],
    line_callback: SerialLogCallback | None = None,
) -> SerialOutcome:
    return collect_serial_log(config, line_callback=line_callback, serial_factory=lambda _config: FakeSerial(lines))
