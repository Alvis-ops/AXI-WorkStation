from __future__ import annotations

import asyncio
import concurrent.futures
import ctypes
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


NUS_TX_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"
NUS_RX_UUID = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
NRF_CONNECT_BLE_EXE_NAME = "nRF Connect for Desktop Bluetooth Low Energy.exe"
NRF_DONGLE_SCAN_SCRIPT = "nrf_dongle_scan.js"
NRF_DONGLE_NUS_SCRIPT = "nrf_dongle_nus.js"


@dataclass
class BLEDeviceInfo:
    name: str
    address: str
    source: str = "Bleak"
    device: Any = None
    rssi: int | None = None


def _patch_bleak_winrt() -> None:
    try:
        from bleak.backends.winrt import scanner as winrt_scanner
    except Exception:
        return
    watcher_cls = getattr(winrt_scanner, "BluetoothLEAdvertisementWatcher", None)
    if watcher_cls is None or getattr(watcher_cls, "_poc3a_factory_patch", False):
        return

    class SafeWatcher:
        _poc3a_factory_patch = True

        def __init__(self) -> None:
            object.__setattr__(self, "_real", watcher_cls())

        def __getattr__(self, name):
            return getattr(object.__getattribute__(self, "_real"), name)

        def __setattr__(self, name, value) -> None:
            if name == "allow_extended_advertisements":
                try:
                    setattr(object.__getattribute__(self, "_real"), name, value)
                except AttributeError:
                    pass
                return
            setattr(object.__getattribute__(self, "_real"), name, value)

    winrt_scanner.BluetoothLEAdvertisementWatcher = SafeWatcher


def _int_to_mac(addr_int: int) -> str:
    return ":".join(f"{(addr_int >> (i * 8)) & 0xFF:02X}" for i in range(5, -1, -1))


def _same_address(left: str, right: str) -> bool:
    return left.replace(":", "").replace("-", "").upper() == right.replace(":", "").replace("-", "").upper()


def _clean_ble_name(value: str) -> str:
    return str(value or "").strip()


def _matches_ble_name(name_filter: str, local_name: str) -> bool:
    needle = _clean_ble_name(name_filter).lower()
    if not needle:
        return True
    return needle in _clean_ble_name(local_name).lower()


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


async def _scan_ble_async(name_filter: str, timeout_s: float) -> list[BLEDeviceInfo]:
    _patch_bleak_winrt()
    try:
        from bleak import BleakScanner
    except ImportError as exc:
        raise RuntimeError("bleak is required for BLE: pip install bleak") from exc

    seen: dict[str, BLEDeviceInfo] = {}

    def on_detect(device, adv_data) -> None:
        dev_name = getattr(device, "name", None) or ""
        adv_name = getattr(adv_data, "local_name", None) or ""
        local_name = _clean_ble_name(adv_name or dev_name)
        address = getattr(device, "address", None) or ""
        if not address:
            return
        if not _matches_ble_name(name_filter, local_name):
            return
        rssi = _safe_int(getattr(adv_data, "rssi", None) or getattr(device, "rssi", None))
        seen[address] = BLEDeviceInfo(local_name or "(no name)", address, "Bleak", device, rssi)

    scanner = BleakScanner(detection_callback=on_detect)
    await scanner.start()
    try:
        await asyncio.sleep(timeout_s)
    finally:
        await scanner.stop()
    return sorted(seen.values(), key=lambda item: (item.name, item.address))


def _candidate_nrf_connect_ble_paths(explicit_path: str = "") -> list[Path]:
    candidates: list[Path] = []

    def add_candidate(value: str) -> None:
        if not value:
            return
        path = Path(value).expanduser()
        if path.is_dir():
            candidates.append(path / NRF_CONNECT_BLE_EXE_NAME)
        else:
            candidates.append(path)

    add_candidate(explicit_path)
    add_candidate(os.environ.get("AXI_NRF_CONNECT_BLE_EXE", ""))
    add_candidate(os.environ.get("AXI_NRF_CONNECT_BLE_DIR", ""))
    local_appdata = os.environ.get("LOCALAPPDATA", "")
    program_files = os.environ.get("ProgramFiles", "")
    program_files_x86 = os.environ.get("ProgramFiles(x86)", "")
    add_candidate(str(Path(local_appdata) / "Programs" / "nrfconnect-bluetooth-low-energy") if local_appdata else "")
    add_candidate(str(Path(program_files) / "nrfconnect-bluetooth-low-energy") if program_files else "")
    add_candidate(str(Path(program_files_x86) / "nrfconnect-bluetooth-low-energy") if program_files_x86 else "")

    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate).lower()
        if key not in seen:
            unique.append(candidate)
            seen.add(key)
    return unique


def _find_nrf_connect_ble_exe(explicit_path: str = "") -> Path:
    for candidate in _candidate_nrf_connect_ble_paths(explicit_path):
        if candidate.is_file():
            return candidate
    checked = ", ".join(str(item) for item in _candidate_nrf_connect_ble_paths(explicit_path))
    raise RuntimeError(f"nRF Connect BLE app not found. Checked: {checked}")


def _find_nrf_dongle_scan_script() -> Path:
    candidates = [
        Path(__file__).resolve().with_name(NRF_DONGLE_SCAN_SCRIPT),
        Path(sys.executable).resolve().parent / NRF_DONGLE_SCAN_SCRIPT,
        Path(sys.executable).resolve().parent / "factory_workstation" / NRF_DONGLE_SCAN_SCRIPT,
    ]
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        root = Path(meipass)
        candidates.extend(
            [
                root / "factory_workstation" / NRF_DONGLE_SCAN_SCRIPT,
                root / "tools" / "factory_workstation" / NRF_DONGLE_SCAN_SCRIPT,
                root / NRF_DONGLE_SCAN_SCRIPT,
            ]
        )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    checked = ", ".join(str(item) for item in candidates)
    raise RuntimeError(f"{NRF_DONGLE_SCAN_SCRIPT} not found. Checked: {checked}")


def _find_nrf_dongle_nus_script() -> Path:
    candidates = [
        Path(__file__).resolve().with_name(NRF_DONGLE_NUS_SCRIPT),
        Path(sys.executable).resolve().parent / NRF_DONGLE_NUS_SCRIPT,
        Path(sys.executable).resolve().parent / "factory_workstation" / NRF_DONGLE_NUS_SCRIPT,
    ]
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        root = Path(meipass)
        candidates.extend(
            [
                root / "factory_workstation" / NRF_DONGLE_NUS_SCRIPT,
                root / "tools" / "factory_workstation" / NRF_DONGLE_NUS_SCRIPT,
                root / NRF_DONGLE_NUS_SCRIPT,
            ]
        )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    checked = ", ".join(str(item) for item in candidates)
    raise RuntimeError(f"{NRF_DONGLE_NUS_SCRIPT} not found. Checked: {checked}")


def _tail_text(path: Path, max_chars: int = 1800) -> str:
    if not path.exists():
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[-max_chars:]


def _kill_process_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return
    proc.kill()


def _cleanup_nrf_scan_helpers(markers: list[str]) -> None:
    if os.name != "nt":
        return
    env = os.environ.copy()
    env["AXI_NRF_SCAN_MARKERS"] = "||".join(marker for marker in markers if marker)
    script = r"""
$markers = $env:AXI_NRF_SCAN_MARKERS -split '\|\|'
foreach ($process in Get-CimInstance Win32_Process -Filter "Name = 'nRF Connect for Desktop Bluetooth Low Energy.exe'") {
    $commandLine = [string]$process.CommandLine
    $matched = $false
    foreach ($marker in $markers) {
        if ($marker -and $commandLine.Contains($marker)) {
            $matched = $true
        }
    }
    if ($matched) {
        Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
    }
}
"""
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            check=False,
            timeout=5.0,
        )
    except Exception:
        pass


def _wait_for_json_result(path: Path, deadline: float) -> dict[str, Any]:
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if path.exists() and path.stat().st_size > 0:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
            except Exception as exc:
                last_error = exc
        time.sleep(0.2)
    if last_error is not None:
        raise TimeoutError(f"nRF dongle scan result was not valid JSON: {last_error}") from last_error
    raise TimeoutError("nRF dongle scan timed out without a result")


def scan_nrf_dongle_devices(
    name_filter: str = "AXI-P1-T",
    timeout_s: float = 8.0,
    port: str = "COM8",
    nrf_connect_ble_path: str = "",
    sd_version: str = "auto",
) -> list[BLEDeviceInfo]:
    exe = _find_nrf_connect_ble_exe(nrf_connect_ble_path)
    script = _find_nrf_dongle_scan_script()
    fd, out_name = tempfile.mkstemp(prefix="poc3a_nrf_scan_", suffix=".json")
    os.close(fd)
    out_path = Path(out_name)
    debug_path = out_path.with_suffix(".log")
    out_path.unlink(missing_ok=True)
    debug_path.unlink(missing_ok=True)

    env = os.environ.copy()
    env["ELECTRON_RUN_AS_NODE"] = "1"
    env["AXI_NRF_CONNECT_BLE_DIR"] = str(exe.parent)
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    command = [
        str(exe),
        str(script),
        "--port",
        port.strip() or "COM8",
        "--filter",
        _clean_ble_name(name_filter),
        "--timeout",
        str(max(1, int(timeout_s))),
        "--sd-version",
        sd_version.strip() or "auto",
        "--out",
        str(out_path),
        "--debug-log",
        str(debug_path),
    ]
    proc = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
        creationflags=creationflags,
    )
    try:
        data = _wait_for_json_result(out_path, time.monotonic() + timeout_s + 16.0)
    except Exception as exc:
        debug_tail = _tail_text(debug_path)
        message = str(exc)
        if debug_tail:
            message = f"{message}; debug: {debug_tail}"
        raise RuntimeError(message) from exc
    finally:
        _kill_process_tree(proc)
        _cleanup_nrf_scan_helpers([NRF_DONGLE_SCAN_SCRIPT, str(out_path)])
        try:
            out_path.unlink(missing_ok=True)
            debug_path.unlink(missing_ok=True)
        except OSError:
            pass

    devices = [
        BLEDeviceInfo(
            name=_clean_ble_name(item.get("name", "")) or "(no name)",
            address=_clean_ble_name(item.get("address", "")),
            source=_clean_ble_name(item.get("source", "")) or f"nRF dongle {port}",
            device=None,
            rssi=_safe_int(item.get("rssi")),
        )
        for item in data.get("devices", [])
        if isinstance(item, dict) and _clean_ble_name(item.get("address", ""))
    ]
    if not data.get("ok", False):
        error = data.get("error", "nRF dongle scan failed")
        raise RuntimeError(str(error))
    return devices


def scan_ble_devices(
    name_filter: str = "AXI-P1-T",
    timeout_s: float = 8.0,
    backend: str = "windows",
    dongle_port: str = "COM8",
    nrf_connect_ble_path: str = "",
    dongle_sd_version: str = "auto",
) -> list[BLEDeviceInfo]:
    normalized_backend = (backend or "windows").strip().lower().replace("-", "_")
    if normalized_backend in {"nrf", "nrf_dongle", "dongle", "pc_ble_driver"}:
        return scan_nrf_dongle_devices(
            name_filter,
            timeout_s,
            port=dongle_port,
            nrf_connect_ble_path=nrf_connect_ble_path,
            sd_version=dongle_sd_version,
        )
    if normalized_backend == "auto":
        try:
            return scan_nrf_dongle_devices(
                name_filter,
                timeout_s,
                port=dongle_port,
                nrf_connect_ble_path=nrf_connect_ble_path,
                sd_version=dongle_sd_version,
            )
        except Exception:
            return asyncio.run(_scan_ble_async(name_filter, timeout_s))
    return asyncio.run(_scan_ble_async(name_filter, timeout_s))


class BLENusTransport:
    def __init__(
        self,
        name: str,
        address: str = "",
        scan_timeout_s: float = 12.0,
        backend: str = "bleak",
        pair: bool = False,
        dongle_port: str = "COM8",
        nrf_connect_ble_path: str = "",
        dongle_sd_version: str = "auto",
    ) -> None:
        self.name = name
        self.address = address
        self.scan_timeout_s = scan_timeout_s
        self._backend = (backend or "bleak").strip().lower().replace("-", "_")
        self._pair = bool(pair)
        self._dongle_port = dongle_port or "COM8"
        self._nrf_connect_ble_path = nrf_connect_ble_path
        self._dongle_sd_version = dongle_sd_version or "auto"
        self._line_q: queue.Queue[str] = queue.Queue()
        self._ready = threading.Event()
        self._closed = threading.Event()
        self._error: Exception | None = None
        # Bleak backend state
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client = None
        self._rx = bytearray()
        self._close_lock = threading.Lock()
        self._disconnecting = False
        # Dongle backend state
        self._dongle_proc: subprocess.Popen | None = None
        self._dongle_reader: threading.Thread | None = None
        self._dongle_lock = threading.Lock()
        self._dongle_write_ack_q = queue.Queue()
        self._dongle_waiting_write = False
        self._dongle_addr = ""

        if self._backend in {"nrf", "nrf_dongle", "dongle", "pc_ble_driver"}:
            self._init_dongle()
        else:
            self._init_bleak()

    def _init_bleak(self) -> None:
        self._thread = threading.Thread(target=self._thread_main, daemon=True)
        self._thread.start()
        pairing_allowance_s = 70.0 if self._pair else 20.0
        if not self._ready.wait(self.scan_timeout_s + pairing_allowance_s):
            self.close()
            raise TimeoutError("BLE connect timed out")
        if self._error is not None:
            raise RuntimeError(f"BLE connect failed: {self._error}") from self._error

    def _init_dongle(self) -> None:
        if self._pair:
            raise RuntimeError("BLE authentication/pairing requires the Windows BLE backend")
        if not self.address:
            raise RuntimeError("nRF dongle backend requires a BLE address; scan first")
        exe = _find_nrf_connect_ble_exe(self._nrf_connect_ble_path)
        script = _find_nrf_dongle_nus_script()
        env = os.environ.copy()
        env["ELECTRON_RUN_AS_NODE"] = "1"
        env["AXI_NRF_CONNECT_BLE_DIR"] = str(exe.parent)
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        command = [
            str(exe),
            str(script),
            "--port",
            self._dongle_port,
            "--sd-version",
            self._dongle_sd_version,
        ]
        try:
            proc = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=env,
                creationflags=creationflags,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            raise RuntimeError(f"failed to start nRF dongle helper: {exc}") from exc
        self._dongle_proc = proc
        self._dongle_reader = threading.Thread(target=self._dongle_reader_loop, daemon=True)
        self._dongle_reader.start()
        self._dongle_send(
            {
                "cmd": "connect",
                "address": self.address,
                "name": self.name,
                "timeout": int(max(1.0, self.scan_timeout_s)),
            }
        )
        if not self._ready.wait(self.scan_timeout_s + 20.0):
            self.close()
            raise TimeoutError("BLE connect timed out")
        if self._error is not None:
            self.close()
            raise RuntimeError(f"BLE connect failed: {self._error}") from self._error

    def _dongle_send(self, obj: dict[str, Any]) -> None:
        proc = self._dongle_proc
        if proc is None or proc.stdin is None or proc.poll() is not None:
            return
        line = json.dumps(obj, separators=(",", ":")) + "\n"
        with self._dongle_lock:
            try:
                proc.stdin.write(line)
                proc.stdin.flush()
            except (OSError, ValueError):
                pass

    def _dongle_reader_loop(self) -> None:
        proc = self._dongle_proc
        if proc is None or proc.stdout is None:
            return
        try:
            for raw in proc.stdout:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    evt = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                if not isinstance(evt, dict):
                    continue
                kind = evt.get("event")
                if kind == "notify":
                    line = str(evt.get("line", "")).strip()
                    if line:
                        self._line_q.put(line)
                elif kind == "connected":
                    self._dongle_addr = str(evt.get("address", "") or self.address)
                    self._ready.set()
                elif kind == "write_ok":
                    self._dongle_write_ack_q.put(None)
                elif kind == "error":
                    self._error = RuntimeError(str(evt.get("msg", "nRF dongle error")))
                    if self._dongle_waiting_write:
                        self._closed.set()
                        self._dongle_write_ack_q.put(self._error)
                    self._ready.set()
                elif kind == "disconnected":
                    self._closed.set()
                    if self._dongle_waiting_write:
                        self._dongle_write_ack_q.put(RuntimeError("BLE is not connected"))
                elif kind == "log":
                    pass
        except Exception:
            pass
        if not self._ready.is_set():
            self._error = RuntimeError("nRF dongle helper exited before connect")
            self._ready.set()
        self._closed.set()
        if self._dongle_waiting_write:
            self._dongle_write_ack_q.put(RuntimeError("BLE helper exited"))

    def _dongle_close(self) -> None:
        proc = self._dongle_proc
        if proc is None:
            return
        self._dongle_send({"cmd": "close"})
        try:
            proc.wait(timeout=4.0)
        except Exception:
            _kill_process_tree(proc)
        self._dongle_proc = None

    @property
    def backend(self) -> str:
        return self._backend

    @property
    def dongle_kwargs(self) -> dict[str, Any]:
        return {
            "backend": self._backend,
            "dongle_port": self._dongle_port,
            "nrf_connect_ble_path": self._nrf_connect_ble_path,
            "dongle_sd_version": self._dongle_sd_version,
        }

    @property
    def pairing_enabled(self) -> bool:
        return self._pair

    def _thread_main(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._connect_main())
            self._ready.set()
            self._loop.run_until_complete(self._wait_until_closed())
        except Exception as exc:
            self._error = exc
            self._ready.set()
        finally:
            try:
                if self._loop is not None and not self._loop.is_closed():
                    self._loop.run_until_complete(self._disconnect_client())
            except Exception:
                pass
            self._closed.set()
            if self._loop is not None and not self._loop.is_closed():
                self._loop.close()
            self._loop = None

    async def _connect_main(self) -> None:
        _patch_bleak_winrt()
        try:
            from bleak import BleakClient, BleakScanner
        except ImportError as exc:
            raise RuntimeError("bleak is required for BLE: pip install bleak") from exc
        target: Any = None
        if self.address:
            target = self.address
            try:
                devices = await _scan_ble_async("", min(self.scan_timeout_s, 6.0))
                for item in devices:
                    if _same_address(item.address, self.address):
                        target = item.device or item.address
                        break
            except Exception:
                pass
        else:
            devices = await _scan_ble_async(self.name, self.scan_timeout_s)
            for item in devices:
                if item.name == self.name:
                    target = item.device or item.address
                    break
            if target is None and devices:
                target = devices[0].device or devices[0].address
        if target is None:
            raise TimeoutError(f"BLE target not found: {self.name}")
        connect_timeout = max(60.0, self.scan_timeout_s + 20.0) if self._pair else 15.0
        self._client = BleakClient(target, timeout=connect_timeout, pair=self._pair)
        if self._pair:
            await self._client.connect(protection_level=3)
        else:
            await self._client.connect()
        await asyncio.sleep(0.5)
        await self._client.start_notify(NUS_TX_UUID, self._on_notify)
        await asyncio.sleep(1.5)

    async def _wait_until_closed(self) -> None:
        while not self._closed.is_set():
            await asyncio.sleep(0.2)

    async def _disconnect_client(self) -> None:
        with self._close_lock:
            if self._disconnecting or self._client is None:
                return
            self._disconnecting = True
            client = self._client
        try:
            await client.disconnect()
        except Exception:
            pass
        finally:
            with self._close_lock:
                if self._client is client:
                    self._client = None
                self._disconnecting = False

    def _on_notify(self, _sender, data: bytearray) -> None:
        self._rx.extend(bytes(data))
        while b"\n" in self._rx:
            raw, _, rest = self._rx.partition(b"\n")
            self._rx = bytearray(rest)
            line = raw.decode("utf-8", errors="replace").strip()
            if line:
                self._line_q.put(line)

    def close(self) -> None:
        self._closed.set()
        if self._backend in {"nrf", "nrf_dongle", "dongle", "pc_ble_driver"}:
            self._dongle_close()
            return
        loop = self._loop
        if loop is None or loop.is_closed() or not loop.is_running():
            return
        try:
            fut = asyncio.run_coroutine_threadsafe(self._disconnect_client(), loop)
            fut.result(timeout=3.0)
        except concurrent.futures.TimeoutError:
            fut.cancel()
        except Exception:
            pass

    def wait_closed(self, timeout_s: float = 5.0) -> bool:
        if self._backend in {"nrf", "nrf_dongle", "dongle", "pc_ble_driver"}:
            proc = self._dongle_proc
            return proc is None or proc.poll() is not None
        thread = getattr(self, "_thread", None)
        if thread is None or thread is threading.current_thread():
            return thread is None
        thread.join(max(0.0, timeout_s))
        return not thread.is_alive()

    def clear_input(self) -> None:
        while True:
            try:
                self._line_q.get_nowait()
            except queue.Empty:
                break

    def write_line(self, command: str) -> None:
        if self._closed.is_set():
            raise RuntimeError("BLE is not connected")
        payload = (command.rstrip("\r\n") + "\r\n")
        if self._backend in {"nrf", "nrf_dongle", "dongle", "pc_ble_driver"}:
            if self._dongle_proc is None or self._dongle_proc.poll() is not None:
                raise RuntimeError("BLE is not connected")
            while True:
                try:
                    self._dongle_write_ack_q.get_nowait()
                except queue.Empty:
                    break
            self._dongle_waiting_write = True
            self._dongle_send({"cmd": "write", "data": payload})
            try:
                ack = self._dongle_write_ack_q.get(timeout=5.0)
            except queue.Empty as exc:
                if self._closed.is_set():
                    raise RuntimeError("BLE is not connected") from exc
                if self._dongle_proc is None or self._dongle_proc.poll() is not None:
                    raise RuntimeError("BLE helper exited") from exc
                raise TimeoutError("BLE write acknowledgement timed out") from exc
            finally:
                self._dongle_waiting_write = False
            if isinstance(ack, Exception):
                raise ack
            return
        if self._loop is None or self._client is None:
            raise RuntimeError("BLE is not connected")
        fut = asyncio.run_coroutine_threadsafe(
            self._client.write_gatt_char(NUS_RX_UUID, payload.encode("ascii"), response=False),
            self._loop,
        )
        fut.result(timeout=5.0)

    def read_line(self, timeout_s: float) -> str | None:
        try:
            return self._line_q.get(timeout=timeout_s)
        except queue.Empty:
            if self._closed.is_set():
                raise RuntimeError("BLE is not connected")
            if self._backend in {"nrf", "nrf_dongle", "dongle", "pc_ble_driver"}:
                if self._dongle_proc is None or self._dongle_proc.poll() is not None:
                    self._closed.set()
                    raise RuntimeError("BLE helper exited")
            return None

    def is_connected(self) -> bool:
        if self._closed.is_set():
            return False
        if self._backend in {"nrf", "nrf_dongle", "dongle", "pc_ble_driver"}:
            return self._dongle_proc is not None and self._dongle_proc.poll() is None and self._ready.is_set()
        client = self._client
        if client is None:
            return False
        return bool(getattr(client, "is_connected", True))
