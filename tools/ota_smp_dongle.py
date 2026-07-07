#!/usr/bin/env python3
"""Upload a Zephyr MCUboot image over BLE SMP using the nRF Dongle backend."""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import queue
import subprocess
import sys
import threading
import time
import traceback
import zipfile
from pathlib import Path
from typing import Any

from smp import header as smphdr
from smpclient import SMPClient
from smpclient.generics import error
from smpclient.requests.image_management import ImageStatesRead, ImageStatesWrite
from smpclient.requests.os_management import ResetWrite

from factory_workstation.transport_ble import (
    _find_nrf_connect_ble_exe,
    scan_nrf_dongle_devices,
)


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)


PROFILES = {
    "safe": {
        "smp_buffer_size": 244,
        "gatt_write_size": 20,
        "gatt_write_delay_ms": 8.0,
        "chunk_delay": 0.08,
        "timeout": 12.0,
    },
    "balanced": {
        "smp_buffer_size": 244,
        "gatt_write_size": 20,
        "gatt_write_delay_ms": 3.0,
        "chunk_delay": 0.02,
        "timeout": 10.0,
    },
}


def out(message: str) -> None:
    print(message, flush=True)


def _load_image(path: Path) -> bytes:
    if path.suffix.lower() != ".zip":
        return path.read_bytes()

    with zipfile.ZipFile(path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        image_name = manifest["files"][0]["file"]
        out(
            "DFU package: "
            f"{image_name}, version_MCUBOOT={manifest['files'][0].get('version_MCUBOOT')}"
        )
        return archive.read(image_name)


def _format_images(response) -> str:
    rows = []
    for image in response.images:
        hash_short = image.hash.hex()[:12] if image.hash else "-"
        flags = ",".join(
            flag
            for flag, enabled in (
                ("active", image.active),
                ("confirmed", image.confirmed),
                ("pending", image.pending),
                ("permanent", image.permanent),
                ("bootable", image.bootable),
            )
            if enabled
        )
        rows.append(
            f"slot={image.slot} image={image.image if image.image is not None else 0} "
            f"version={image.version} hash={hash_short} flags={flags or '-'}"
        )
    return "\n".join(rows)


def _find_image(response, *, slot: int | None = None, active: bool = False):
    for image in response.images:
        if slot is not None and image.slot != slot:
            continue
        if active and not image.active:
            continue
        if image.hash:
            return image
    return None


async def _read_image_state(client: SMPClient, *, timeout_s: float, label: str):
    state = await client.request(ImageStatesRead(), timeout_s=timeout_s)
    if error(state):
        raise RuntimeError(f"Image state read {label} failed: {state}")
    out(f"Image state {label}:\n" + _format_images(state))
    return state


class DongleSMPTransport:
    def __init__(
        self,
        *,
        dongle_port: str,
        nrf_connect_ble_path: str,
        sd_version: str,
        gatt_write_size: int,
        gatt_write_delay_ms: float,
    ) -> None:
        self._dongle_port = dongle_port
        self._nrf_connect_ble_path = nrf_connect_ble_path
        self._sd_version = sd_version
        self._gatt_write_size = max(20, min(gatt_write_size, 244))
        self._gatt_write_delay_s = max(0.0, gatt_write_delay_ms / 1000.0)
        self._event_q: queue.Queue[dict[str, Any]] = queue.Queue()
        self._notify_q: queue.Queue[bytes | None] = queue.Queue()
        self._buffer = bytearray()
        self._proc: subprocess.Popen[str] | None = None
        self._reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None
        self._closed = threading.Event()
        self._smp_server_transport_buffer_size: int | None = None

    @property
    def mtu(self) -> int:
        return self._gatt_write_size

    @property
    def max_unencoded_size(self) -> int:
        return self._smp_server_transport_buffer_size or self.mtu

    def initialize(self, smp_server_transport_buffer_size: int) -> None:
        self._smp_server_transport_buffer_size = smp_server_transport_buffer_size

    async def connect(self, address: str, timeout_s: float) -> None:
        self._start_helper()
        self._send({"cmd": "connect_smp", "address": address, "timeout": timeout_s})
        await self._wait_for_event({"connected"}, timeout_s + 10.0)

    async def disconnect(self) -> None:
        proc = self._proc
        if proc is None:
            return
        try:
            self._send({"cmd": "close"})
            await asyncio.to_thread(proc.wait, 3.0)
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass
        finally:
            self._closed.set()
            self._notify_q.put(None)

    async def send(self, data: bytes) -> None:
        for offset in range(0, len(data), self.mtu):
            chunk = data[offset : offset + self.mtu]
            is_last_chunk = offset + self.mtu >= len(data)
            self._send(
                {
                    "cmd": "write_smp",
                    "data": base64.b64encode(chunk).decode("ascii"),
                }
            )
            await self._wait_for_event({"write_ok"}, 10.0)
            if not is_last_chunk and self._gatt_write_delay_s > 0.0:
                await asyncio.sleep(self._gatt_write_delay_s)

    async def receive(self) -> bytes:
        while len(self._buffer) < smphdr.Header.SIZE:
            await self._append_notify_chunk()

        header = smphdr.Header.loads(self._buffer[: smphdr.Header.SIZE])
        message_length = header.length + header.SIZE
        while len(self._buffer) < message_length:
            await self._append_notify_chunk()

        if len(self._buffer) > message_length:
            out(
                "WARN: SMP notification buffer contains extra bytes; "
                f"keeping {len(self._buffer) - message_length} for next response"
            )
        frame = bytes(self._buffer[:message_length])
        del self._buffer[:message_length]
        return frame

    async def send_and_receive(self, data: bytes) -> bytes:
        await self.send(data)
        return await self.receive()

    def _start_helper(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            return

        exe = _find_nrf_connect_ble_exe(self._nrf_connect_ble_path)
        script = Path(__file__).resolve().parent / "factory_workstation" / "nrf_dongle_nus.js"
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
            self._sd_version,
        ]
        self._proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(Path(__file__).resolve().parents[1]),
            env=env,
            creationflags=creationflags,
        )
        self._reader = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader.start()
        self._stderr_reader = threading.Thread(target=self._stderr_loop, daemon=True)
        self._stderr_reader.start()

    def _reader_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        try:
            for raw in proc.stdout:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    self._event_q.put({"event": "log", "msg": raw})
                    continue
                kind = event.get("event")
                if kind == "smp_notify":
                    payload = base64.b64decode(str(event.get("data", "")))
                    self._notify_q.put(payload)
                else:
                    self._event_q.put(event)
                    if kind in {"disconnected", "error"}:
                        self._notify_q.put(None)
        finally:
            self._closed.set()
            self._notify_q.put(None)

    def _stderr_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        for raw in proc.stderr:
            raw = raw.strip()
            if raw:
                self._event_q.put({"event": "log", "msg": raw})

    def _send(self, obj: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None or proc.poll() is not None:
            raise RuntimeError("nRF Dongle helper is not running")
        proc.stdin.write(json.dumps(obj, separators=(",", ":")) + "\n")
        proc.stdin.flush()

    async def _wait_for_event(self, expected: set[str], timeout_s: float) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"Timed out waiting for {sorted(expected)}")
            try:
                event = await asyncio.to_thread(self._event_q.get, True, remaining)
            except queue.Empty as exc:
                raise TimeoutError(f"Timed out waiting for {sorted(expected)}") from exc
            kind = str(event.get("event", ""))
            if kind == "log":
                msg = str(event.get("msg", ""))
                if msg:
                    out(f"DONGLE: {msg}")
                continue
            if kind == "error":
                raise RuntimeError(str(event.get("msg", "nRF Dongle helper error")))
            if kind == "disconnected":
                raise RuntimeError("nRF Dongle disconnected")
            if kind in expected:
                return event

    async def _append_notify_chunk(self) -> None:
        while True:
            try:
                chunk = await asyncio.to_thread(self._notify_q.get, True, 1.0)
            except queue.Empty:
                if self._closed.is_set():
                    raise RuntimeError("nRF Dongle helper exited while waiting for SMP response")
                continue
            if chunk is None:
                raise RuntimeError("nRF Dongle disconnected while waiting for SMP response")
            self._buffer.extend(chunk)
            return


async def _find_address(args: argparse.Namespace) -> str:
    if args.addr:
        return args.addr
    out(f"Scanning with nRF Dongle {args.dongle_port} for {args.name}...")
    devices = await asyncio.to_thread(
        scan_nrf_dongle_devices,
        args.name,
        args.scan_timeout,
        args.dongle_port,
        nrf_connect_ble_path=args.nrf_connect_ble_path,
        sd_version=args.sd_version,
    )
    for device in devices:
        if device.name == args.name:
            out(f"Found {device.name} {device.address}")
            return device.address
    raise RuntimeError(f"Device {args.name!r} not found via nRF Dongle")


async def _verify_after_reset(args: argparse.Namespace, expected_hash: bytes) -> None:
    out(f"Waiting {args.post_reset_delay:.1f}s before post-reset verification...")
    await asyncio.sleep(args.post_reset_delay)
    address = await _find_address(args)
    transport = DongleSMPTransport(
        dongle_port=args.dongle_port,
        nrf_connect_ble_path=args.nrf_connect_ble_path,
        sd_version=args.sd_version,
        gatt_write_size=args.gatt_write_size,
        gatt_write_delay_ms=args.gatt_write_delay_ms,
    )
    client = SMPClient(transport, address, timeout_s=args.post_reset_timeout)
    await client.connect()
    try:
        if args.smp_buffer_size > 0:
            client._transport.initialize(args.smp_buffer_size)
        state = await _read_image_state(client, timeout_s=args.post_reset_timeout, label="after reset")
        active = _find_image(state, active=True)
        if active is None or active.hash is None:
            raise RuntimeError("No active image hash returned after reset")
        if bytes(active.hash) != expected_hash:
            raise RuntimeError(
                "Post-reset active hash did not match uploaded image: "
                f"active={active.hash.hex()[:12]} expected={expected_hash.hex()[:12]}"
            )
        out(f"Post-reset active hash verified: {active.hash.hex()[:12]}")
    finally:
        await client.disconnect()


async def _run(args: argparse.Namespace) -> int:
    image = _load_image(Path(args.image))
    address = await _find_address(args)
    started = time.monotonic()
    last_offset = 0
    active_hash_before: bytes | None = None
    secondary_hash: bytes | None = None
    printed_profile = False

    for attempt in range(1, args.upload_attempts + 1):
        if attempt > 1:
            out(
                f"Retrying upload after disconnect "
                f"({attempt}/{args.upload_attempts}), last_offset={last_offset}"
            )
            await asyncio.sleep(args.reconnect_delay)
            address = await _find_address(args)

        transport = DongleSMPTransport(
            dongle_port=args.dongle_port,
            nrf_connect_ble_path=args.nrf_connect_ble_path,
            sd_version=args.sd_version,
            gatt_write_size=args.gatt_write_size,
            gatt_write_delay_ms=args.gatt_write_delay_ms,
        )
        try:
            client = SMPClient(transport, address, timeout_s=args.timeout)
            await client.connect()
            try:
                if args.smp_buffer_size > 0:
                    client._transport.initialize(args.smp_buffer_size)
                    if not printed_profile:
                        out(f"Using SMP request size limit: {args.smp_buffer_size} bytes")
                if not printed_profile:
                    out(
                        f"nRF Dongle OTA profile: {args.profile}, "
                        f"gatt_write_size={args.gatt_write_size}, "
                        f"gatt_write_delay={args.gatt_write_delay_ms:.1f}ms, "
                        f"chunk_delay={args.chunk_delay:.3f}s"
                    )
                    printed_profile = True

                state_before = await _read_image_state(client, timeout_s=args.timeout, label="before upload")
                active_before = _find_image(state_before, active=True)
                if active_hash_before is None and active_before and active_before.hash:
                    active_hash_before = bytes(active_before.hash)

                if last_offset == 0:
                    out(f"Uploading {len(image)} bytes...")
                else:
                    out(f"Resuming upload at host-seen offset {last_offset}/{len(image)} bytes...")
                async for offset in client.upload(
                    image,
                    upgrade=False,
                    first_timeout_s=args.first_timeout,
                    subsequent_timeout_s=args.timeout,
                    use_sha=not args.no_sha,
                ):
                    if offset == last_offset:
                        continue
                    last_offset = offset
                    pct = offset * 100.0 / len(image)
                    out(f"  {offset}/{len(image)} bytes ({pct:.1f}%)")
                    if args.chunk_delay > 0:
                        await asyncio.sleep(args.chunk_delay)

                state = await _read_image_state(client, timeout_s=args.timeout, label="after upload")
                secondary = _find_image(state, slot=1)
                if secondary is None or secondary.hash is None:
                    raise RuntimeError("No secondary-slot image hash returned after upload")
                secondary_hash = bytes(secondary.hash)
                same_as_active = active_hash_before is not None and secondary_hash == active_hash_before
                if same_as_active:
                    raise RuntimeError(
                        "Uploaded image hash matches the active image; rebuild a different image before OTA"
                    )

                write_state = await client.request(
                    ImageStatesWrite(hash=secondary_hash, confirm=False), timeout_s=args.timeout
                )
                if error(write_state):
                    raise RuntimeError(f"Marking image test-pending failed: {write_state}")
                out("Marked uploaded image as test-pending")

                if args.no_reset:
                    out("Upload complete; reset skipped by --no-reset")
                else:
                    out("Requesting reset...")
                    try:
                        await client.request(ResetWrite(), timeout_s=1.0)
                    except Exception as exc:
                        out(f"Reset request ended with disconnect/timeout: {exc}")
                break
            finally:
                await client.disconnect()
        except RuntimeError as exc:
            message = str(exc)
            if "nRF Dongle disconnected" in message and attempt < args.upload_attempts:
                continue
            raise
    else:
        raise RuntimeError(f"Upload did not complete after {args.upload_attempts} attempts")

    if args.verify_after_reset and not args.no_reset:
        if secondary_hash is None:
            raise RuntimeError("No secondary image hash available for post-reset verification")
        await _verify_after_reset(args, secondary_hash)

    elapsed = time.monotonic() - started
    kib_s = (len(image) / 1024.0) / elapsed if elapsed > 0 else 0.0
    out(f"Done in {elapsed:.1f}s ({kib_s:.2f} KiB/s)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="BLE SMP OTA helper using the nRF Dongle backend")
    parser.add_argument("image", help="dfu_application.zip or signed .bin")
    parser.add_argument("--addr", help="BLE address, for example C8:B9:CA:AC:85:74")
    parser.add_argument("--name", default="AXI-P1-T", help="BLE advertised name")
    parser.add_argument("--dongle-port", default="COM8", help="nRF Dongle CDC port")
    parser.add_argument("--sd-version", default="auto", help="pc-ble-driver SoftDevice API version")
    parser.add_argument("--nrf-connect-ble-path", default="", help="nRF Connect BLE executable or install dir")
    parser.add_argument("--scan-timeout", type=float, default=20.0)
    parser.add_argument("--timeout", type=float, default=8.0)
    parser.add_argument("--first-timeout", type=float, default=60.0)
    parser.add_argument("--profile", choices=tuple(PROFILES), default="balanced")
    parser.add_argument("--smp-buffer-size", type=int, default=None)
    parser.add_argument("--gatt-write-size", type=int, default=None)
    parser.add_argument("--gatt-write-delay-ms", type=float, default=None)
    parser.add_argument("--chunk-delay", type=float, default=None)
    parser.add_argument("--upload-attempts", type=int, default=24)
    parser.add_argument("--reconnect-delay", type=float, default=4.0)
    parser.add_argument("--no-sha", action="store_true")
    parser.add_argument("--no-reset", action="store_true")
    parser.add_argument("--verify-after-reset", action="store_true")
    parser.add_argument("--post-reset-delay", type=float, default=12.0)
    parser.add_argument("--post-reset-timeout", type=float, default=20.0)
    args = parser.parse_args()

    profile = PROFILES[args.profile]
    if args.smp_buffer_size is None:
        args.smp_buffer_size = profile["smp_buffer_size"]
    if args.gatt_write_size is None:
        args.gatt_write_size = profile["gatt_write_size"]
    if args.gatt_write_delay_ms is None:
        args.gatt_write_delay_ms = profile["gatt_write_delay_ms"]
    if args.chunk_delay is None:
        args.chunk_delay = profile["chunk_delay"]
    if args.timeout == parser.get_default("timeout"):
        args.timeout = profile["timeout"]

    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        out("Interrupted")
        return 130
    except Exception as exc:
        out(f"ERROR: {type(exc).__name__}: {exc!r}")
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
