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


def open_serial_port(port: str, baudrate: int) -> SerialLike:
    if not port:
        raise RuntimeError("serial_port is empty")
    try:
        import serial
    except ImportError as exc:
        raise RuntimeError("pyserial is required: pip install pyserial") from exc
    return serial.Serial(port, baudrate=int(baudrate), timeout=0.05)


def _open_serial(config: BareBoardConfig) -> SerialLike:
    return open_serial_port(config.serial_port, int(config.serial_baudrate))


class SerialMonitor:
    """Keep a serial port open and forward incoming lines to a callback."""

    def __init__(self) -> None:
        self._port: SerialLike | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._rx = bytearray()
        self.port_name = ""
        self.baudrate = 0

    def is_open(self) -> bool:
        return self._port is not None

    def open(self, port: str, baudrate: int, line_callback: SerialLogCallback | None = None) -> None:
        if self.is_open():
            raise RuntimeError("serial monitor already open")
        self._stop.clear()
        self._rx = bytearray()
        self.port_name = port
        self.baudrate = baudrate
        self._port = open_serial_port(port, baudrate)
        try:
            reset = getattr(self._port, "reset_input_buffer")
            reset()
        except Exception:
            pass
        self._thread = threading.Thread(
            target=self._reader_loop,
            args=(line_callback,),
            daemon=True,
            name="bare-board-serial-monitor",
        )
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        if self._port is not None:
            try:
                self._port.close()
            except Exception:
                pass
            self._port = None
        self.port_name = ""
        self.baudrate = 0

    def _reader_loop(self, line_callback: SerialLogCallback | None) -> None:
        port = self._port
        if port is None:
            return
        while not self._stop.is_set():
            line = _pop_buffered_line(self._rx)
            if line is None:
                try:
                    chunk = port.read(256)
                except Exception:
                    break
                if chunk:
                    self._rx.extend(chunk)
                    line = _pop_buffered_line(self._rx)
                else:
                    time.sleep(0.01)
                    continue
            if line is None:
                continue
            if line_callback is not None:
                line_callback("SERIAL_RX", line)


def _wait_for_start_prompt(
    config: BareBoardConfig,
    port: SerialLike,
    rx: bytearray,
    lines: list[str],
    line_callback: SerialLogCallback | None,
    stop: threading.Event,
    started: float,
) -> tuple[bool, str]:
    patterns = [str(p).strip() for p in config.start_prompt_patterns if str(p).strip()]
    if not patterns:
        return True, ""

    deadline = time.monotonic() + max(1.0, float(config.start_prompt_timeout_s or 30.0))
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
            raise RuntimeError(f"fail pattern matched before start prompt: {line} ({elapsed_ms}ms)")
        if _contains_any(line, patterns):
            return True, line

    if stop.is_set():
        return False, "serial collection cancelled"
    return False, f"start prompt timeout after {config.start_prompt_timeout_s:.1f}s"


def _outcome_for_line(
    config: BareBoardConfig,
    line: str,
    started: float,
    lines: list[str],
) -> SerialOutcome | None:
    if _contains_any(line, config.fail_patterns):
        elapsed_ms = int((time.monotonic() - started) * 1000)
        return SerialOutcome(False, "NG", f"fail pattern matched: {line}", elapsed_ms, lines)
    if _contains_any(line, config.pass_patterns):
        elapsed_ms = int((time.monotonic() - started) * 1000)
        return SerialOutcome(True, "PASS", f"pass pattern matched: {line}", elapsed_ms, lines)
    if _contains_any(line, config.end_patterns):
        elapsed_ms = int((time.monotonic() - started) * 1000)
        return SerialOutcome(False, "NG", f"test ended without pass pattern: {line}", elapsed_ms, lines)
    return None


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
        has_start_command = bool(str(config.test_start_command or "").strip())
        if not config.start_prompt_patterns or not has_start_command:
            try:
                reset = getattr(port, "reset_input_buffer")
                reset()
            except Exception:
                pass
        wait_s = max(0.0, float(config.serial_open_wait_s or 0.0))
        if wait_s:
            time.sleep(wait_s)

        prompt_ok, prompt_message = _wait_for_start_prompt(
            config, port, rx, lines, line_callback, stop, started
        )
        if not prompt_ok:
            if stop.is_set():
                elapsed_ms = int((time.monotonic() - started) * 1000)
                return SerialOutcome(False, "CANCELLED", prompt_message, elapsed_ms, lines)
            if config.require_start_prompt:
                elapsed_ms = int((time.monotonic() - started) * 1000)
                return SerialOutcome(False, "NG", prompt_message, elapsed_ms, lines)
            if line_callback is not None and prompt_message:
                line_callback("SERIAL", f"WARN: {prompt_message}; sending start command anyway")

        command = str(config.test_start_command or "").strip()
        if command:
            port.write((command.rstrip("\r\n") + "\r\n").encode("ascii", errors="replace"))
            port.flush()
            if line_callback is not None:
                line_callback("SERIAL_TX", command)

        for line in lines:
            outcome = _outcome_for_line(config, line, started, lines)
            if outcome is not None:
                return outcome

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
            outcome = _outcome_for_line(config, line, started, lines)
            if outcome is not None:
                return outcome

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
