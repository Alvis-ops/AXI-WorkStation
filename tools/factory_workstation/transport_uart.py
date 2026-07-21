from __future__ import annotations

import time
import threading
from dataclasses import dataclass


@dataclass
class SerialPortInfo:
    device: str
    description: str
    hwid: str = ""


def list_serial_ports() -> list[SerialPortInfo]:
    try:
        from serial.tools import list_ports
    except ImportError:
        return []
    ports = []
    for port in list_ports.comports():
        ports.append(SerialPortInfo(port.device, port.description or port.device, port.hwid or ""))
    return ports


class UARTTransport:
    def __init__(self, port: str, baudrate: int = 460800) -> None:
        try:
            import serial
        except ImportError as exc:
            raise RuntimeError("pyserial is required: pip install pyserial") from exc
        self._serial = serial.Serial(port, baudrate=baudrate, timeout=0.05)
        self._rx = bytearray()
        self._io_lock = threading.RLock()
        self._closed = False

    def close(self) -> None:
        with self._io_lock:
            if self._closed:
                return
            self._closed = True
            self._rx.clear()
            self._serial.close()

    def is_connected(self) -> bool:
        with self._io_lock:
            return not self._closed and bool(self._serial.is_open)

    def clear_input(self) -> None:
        with self._io_lock:
            if self._closed:
                return
            try:
                self._serial.reset_input_buffer()
            except Exception:
                pass
            self._rx.clear()

    def write_line(self, command: str) -> None:
        with self._io_lock:
            if self._closed:
                raise RuntimeError("UART port is closed")
            self._serial.write((command.rstrip("\r\n") + "\r\n").encode("ascii"))
            self._serial.flush()

    def _pop_buffered_line(self) -> str | None:
        while b"\n" in self._rx:
            raw, _, rest = self._rx.partition(b"\n")
            self._rx = bytearray(rest)
            line = raw.decode("utf-8", errors="replace").strip()
            if line:
                return line
        return None

    def read_line(self, timeout_s: float) -> str | None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            line = self._pop_buffered_line()
            if line is not None:
                return line
            with self._io_lock:
                if self._closed:
                    return None
                chunk = self._serial.read(256)
            if chunk:
                self._rx.extend(chunk)
                line = self._pop_buffered_line()
                if line is not None:
                    return line
            else:
                time.sleep(0.01)
        return None
