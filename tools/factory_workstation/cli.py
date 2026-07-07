from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Callable


if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from factory_workstation.at_client import ATClient
    from factory_workstation.at_parser import is_capture_frame_line
    from factory_workstation.config import CONFIG_PATH, WorkstationConfig, get_factory_token, load_config
    from factory_workstation.flows import FlowOutcome, run_full_machine, run_half_machine
    from factory_workstation.storage import NullRunRecord, RunStorage
    from factory_workstation.transport_ble import BLENusTransport
    from factory_workstation.transport_uart import UARTTransport
else:
    from .at_client import ATClient
    from .at_parser import is_capture_frame_line
    from .config import CONFIG_PATH, WorkstationConfig, get_factory_token, load_config
    from .flows import FlowOutcome, run_full_machine, run_half_machine
    from .storage import NullRunRecord, RunStorage
    from .transport_ble import BLENusTransport
    from .transport_uart import UARTTransport


TransportFactory = Callable[[WorkstationConfig, argparse.Namespace, Callable[[str, str], None]], ATClient]


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="POC3A factory workstation CLI")
    parser.add_argument("flow", choices=("half", "full"), help="factory flow to run")
    parser.add_argument("--config", default=str(CONFIG_PATH), help="config JSON path")
    parser.add_argument("--transport", choices=("uart", "ble"), help="transport override")
    parser.add_argument("--port", help="UART port, for example COM18")
    parser.add_argument("--baudrate", type=int, help="UART baudrate")
    parser.add_argument("--ble-name", help="BLE advertising name")
    parser.add_argument("--ble-address", help="BLE address")
    parser.add_argument(
        "--ble-backend",
        choices=("bleak", "nrf_dongle"),
        default="nrf_dongle",
        help="BLE radio backend for scan+connect+NUS (default: nrf_dongle)",
    )
    parser.add_argument("--dongle-port", default="COM8", help="nRF Dongle CDC port for the nrf_dongle backend")
    parser.add_argument("--ble-scan-timeout", type=float, default=8.0, help="BLE scan timeout seconds")
    parser.add_argument("--sn", default="", help="DUT serial number")
    parser.add_argument("--token", default="", help="factory unlock token; env/.env is used when omitted")
    parser.add_argument("--records-root", help="record output root")
    parser.add_argument("--dut-alias", help="DUT alias written to records")
    parser.add_argument("--ota", action="store_true", help="enable OTA phase for full flow")
    parser.add_argument("--no-ota", action="store_true", help="disable OTA phase for full flow")
    parser.add_argument("--skip-momo", action="store_true", help="skip MOMO touch steps for temporary host/device bring-up")
    parser.add_argument(
        "--capture-output-mode",
        choices=("compact", "legacy"),
        help="capture AT output mode; default comes from config.json",
    )
    parser.add_argument("--verbose-frames", action="store_true", help="print every capture frame line")
    sn_group = parser.add_mutually_exclusive_group()
    sn_group.add_argument("--sn-record", action="store_true", help="enable SN validation and records")
    sn_group.add_argument(
        "--no-sn-record",
        action="store_true",
        help="disable SN validation, SN write/read and record files for temporary dry-run",
    )
    return parser.parse_args(argv)


def _apply_overrides(config: WorkstationConfig, args: argparse.Namespace) -> tuple[str, bool]:
    if args.transport:
        config.prefer_transport = args.transport.upper()
    if args.port:
        config.uart_port = args.port
    if args.baudrate:
        config.uart_baudrate = args.baudrate
    if args.ble_name:
        config.ble_name = args.ble_name
    if args.ble_address:
        config.ble_address_whitelist = [args.ble_address]
    if args.records_root:
        config.records_root = args.records_root
    if args.dut_alias is not None:
        config.dut_alias = args.dut_alias
    if args.ota:
        config.ota_enabled = True
    if args.no_ota:
        config.ota_enabled = False
    if args.capture_output_mode:
        config.capture_output_mode = args.capture_output_mode

    sn_enabled = config.sn_enabled
    if args.sn_record:
        sn_enabled = True
    if args.no_sn_record:
        sn_enabled = False
    return config.prefer_transport.upper(), sn_enabled


def _default_transport_factory(
    config: WorkstationConfig,
    args: argparse.Namespace,
    line_callback: Callable[[str, str], None],
) -> ATClient:
    mode = config.prefer_transport.upper()
    if mode == "BLE":
        address = args.ble_address or (config.ble_address_whitelist[0] if config.ble_address_whitelist else "")
        transport = BLENusTransport(
            config.ble_name or "AXI-P1-T",
            address,
            args.ble_scan_timeout,
            backend=args.ble_backend,
            dongle_port=args.dongle_port,
        )
        return ATClient(transport, line_callback)

    if not config.uart_port:
        raise RuntimeError("UART port is empty; pass --port COMx or set uart_port in config.json")
    return ATClient(UARTTransport(config.uart_port, config.uart_baudrate), line_callback)


def _line_callback(direction: str, line: str, verbose_frames: bool = False) -> None:
    if direction == "RX" and not verbose_frames and is_capture_frame_line(line):
        return
    print(f"[{direction}] {line}", flush=True)


def _progress(index: int, label: str, status: str, detail: str) -> None:
    print(f"[STEP] {index:02d} {label}: {status} {detail}", flush=True)


def _exit_code(outcome: FlowOutcome) -> int:
    if outcome.ok:
        return 0
    if outcome.result == "PENDING-HW":
        return 4
    return 2


def run(argv: list[str] | None = None, transport_factory: TransportFactory | None = None) -> int:
    args = _parse_args(argv)
    config = load_config(Path(args.config))
    mode, sn_enabled = _apply_overrides(config, args)
    sn = args.sn.strip() if sn_enabled else ""
    token = get_factory_token(args.token)
    factory = transport_factory or _default_transport_factory

    print(
        f"[INFO] flow={args.flow} transport={mode} sn_record={'on' if sn_enabled else 'off'} "
        f"capture={config.capture_output_mode}",
        flush=True,
    )
    if args.skip_momo:
        if args.flow == "half":
            print("[INFO] 跳过半机 MOMO 人工触摸步骤", flush=True)
        else:
            print("[INFO] 整机 MOMO 为空采集，保留采集步骤", flush=True)
    if not sn_enabled:
        print("[INFO] 空跑模式：跳过 SN 校验、SN 写入和文件记录", flush=True)
        if not token:
            print("[INFO] 空跑模式：未填 token，将跳过 Factory unlock/lock", flush=True)

    record = None
    client: ATClient | None = None
    try:
        client = factory(config, args, _line_callback)
        if sn_enabled:
            station = "HALF" if args.flow == "half" else "FULL"
            record = RunStorage(config.records_root).start_run(station, sn, config.dut_alias)
        else:
            record = NullRunRecord()

        def record_line(direction: str, line: str) -> None:
            record.log_at(direction, line)
            _line_callback(direction, line, args.verbose_frames)

        client.set_line_callback(record_line)
        if args.flow == "half":
            outcome = run_half_machine(
                client,
                config,
                sn,
                token,
                record,
                _progress,
                sn_enabled=sn_enabled,
                skip_momo=args.skip_momo,
            )
        else:
            outcome = run_full_machine(
                client,
                config,
                sn,
                token,
                record,
                _progress,
                sn_enabled=sn_enabled,
                skip_momo=args.skip_momo,
            )
        print(f"[RESULT] {outcome.result} {outcome.message}", flush=True)
        return _exit_code(outcome)
    except KeyboardInterrupt:
        print("[ERR] interrupted", flush=True)
        return 130
    except Exception as exc:
        if record is not None:
            try:
                record.finish("NG", str(exc))
            except Exception:
                pass
        print(f"[ERR] {exc}", flush=True)
        return 1
    finally:
        if client is not None:
            try:
                client.close()
            except Exception as exc:
                print(f"[WARN] close failed: {exc}", flush=True)


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
