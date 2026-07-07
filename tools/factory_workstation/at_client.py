from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Protocol

from .config import redact_sensitive_text


UART_READY_BANNER = "+AT:ready,transport=uart"
UART_DIAG_PREFIXES = ("[CHARGER][UART]", "+PWR:")


class ATTransport(Protocol):
    def write_line(self, command: str) -> None: ...
    def read_line(self, timeout_s: float) -> str | None: ...
    def close(self) -> None: ...


@dataclass
class CommandResult:
    command: str
    ok: bool
    lines: list[str]
    ignored_lines: int = 0
    elapsed_s: float = 0.0

    @property
    def status_text(self) -> str:
        return "PASS" if self.ok else "NG"


LineCallback = Callable[[str, str], None]


def expected_prefix(command: str) -> str | None:
    upper = command.upper().strip()
    if upper == "AT":
        return None
    if not upper.startswith("AT+"):
        return None
    head = upper[3:]
    if "=" in head:
        cmd_base, _ = head.split("=", 1)
        if cmd_base in ("UARTLOG", "FACTORY", "SN"):
            return None
        head = cmd_base
    if head.endswith("?"):
        head = head[:-1]
    if not head:
        return None
    return "+" + head + ":"


def is_noise_line(line: str, expected: str | None = None) -> bool:
    if not line:
        return True
    if line == UART_READY_BANNER:
        return True
    if line.startswith(UART_DIAG_PREFIXES):
        return True
    if line.startswith("+OTABUSY:") and expected != "+OTABUSY:":
        return True
    if line.startswith("status charging="):
        return True
    if line.startswith(("[PROBE][UART]", "[BOOT][UART]", "[INF]", "[WRN]", "[ERR]", "[DBG]")):
        return True
    if line.startswith("[00:") or line.startswith("*** Booting "):
        return True
    if "\ufffd" in line:
        return True
    return not all(32 <= ord(ch) < 127 for ch in line)


class ATClient:
    def __init__(self, transport: ATTransport, line_callback: LineCallback | None = None) -> None:
        self._transport = transport
        self._line_callback = line_callback
        self._lock = threading.Lock()

    def set_line_callback(self, callback: LineCallback | None) -> None:
        self._line_callback = callback

    def close(self) -> None:
        self._transport.close()

    def is_connected(self) -> bool:
        checker = getattr(self._transport, "is_connected", None)
        if callable(checker):
            return bool(checker())
        return True

    def replace_transport(self, transport: ATTransport) -> None:
        with self._lock:
            self._transport = transport

    def send_command(self, command: str, timeout_s: float) -> CommandResult:
        with self._lock:
            start = time.monotonic()
            if hasattr(self._transport, "clear_input"):
                try:
                    self._transport.clear_input()  # type: ignore[attr-defined]
                except Exception:
                    pass
            self._emit("TX", command)
            self._transport.write_line(command)
            lines: list[str] = []
            ignored = 0
            expected = expected_prefix(command)
            payload_seen = expected is None
            deadline = start + timeout_s
            while time.monotonic() < deadline:
                remaining = max(0.05, min(0.25, deadline - time.monotonic()))
                line = self._transport.read_line(remaining)
                if line is None:
                    continue
                self._emit("RX", line)
                if is_noise_line(line, expected):
                    ignored += 1
                    continue
                lines.append(line)
                if line.startswith("+CME ERROR:"):
                    return CommandResult(command, False, lines, ignored, time.monotonic() - start)
                if line.startswith("+"):
                    if expected is not None and line.startswith(expected):
                        payload_seen = True
                    continue
                if line == "OK" and payload_seen:
                    return CommandResult(command, True, lines, ignored, time.monotonic() - start)
            return CommandResult(command, False, lines, ignored, time.monotonic() - start)

    def observe_lines(self, duration_s: float) -> list[str]:
        deadline = time.monotonic() + duration_s
        lines: list[str] = []
        while time.monotonic() < deadline:
            line = self._transport.read_line(min(0.2, max(0.01, deadline - time.monotonic())))
            if line:
                self._emit("RX", line)
                lines.append(line)
        return lines

    def _emit(self, direction: str, line: str) -> None:
        if self._line_callback is not None:
            if direction == "TX":
                line = redact_sensitive_text(line)
            self._line_callback(direction, line)
