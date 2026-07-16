from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Callable


if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from factory_workstation.at_client import ATClient
    from factory_workstation.at_parser import is_capture_frame_line
    from factory_workstation.config import CONFIG_PATH, WorkstationConfig, get_factory_token, load_config
    from factory_workstation.flash_flow import probe_at_client, record_flash_step
    from factory_workstation.flash_runner import FlashOutcome, run_flash
    from factory_workstation.flows import FlowOutcome, run_full_machine, run_half_machine
    from factory_workstation.mes_service import MesService, build_sample_post_payload, write_pending_request
    from factory_workstation.storage import NullRunRecord, RunStorage, verify_half_sn_pass_record
    from factory_workstation.transport_ble import BLENusTransport
    from factory_workstation.transport_uart import UARTTransport
else:
    from .at_client import ATClient
    from .at_parser import is_capture_frame_line
    from .config import CONFIG_PATH, WorkstationConfig, get_factory_token, load_config
    from .flash_flow import probe_at_client, record_flash_step
    from .flash_runner import FlashOutcome, run_flash
    from .flows import FlowOutcome, run_full_machine, run_half_machine
    from .mes_service import MesService, build_sample_post_payload, write_pending_request
    from .storage import NullRunRecord, RunStorage, verify_half_sn_pass_record
    from .transport_ble import BLENusTransport
    from .transport_uart import UARTTransport


TransportFactory = Callable[[WorkstationConfig, argparse.Namespace, Callable[[str, str], None]], ATClient]
FlashRunner = Callable[[WorkstationConfig, Callable[[str, str], None] | None], FlashOutcome]


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="POC3A factory workstation CLI")
    parser.add_argument(
        "flow",
        choices=("half", "full", "mes-checkroute", "mes-post"),
        help="factory flow or MES diagnostic command",
    )
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
    parser.add_argument(
        "--record-output-mode",
        choices=("unified", "split"),
        help="record file mode; unified writes only unified_log.csv, split also writes compatibility files",
    )
    flash_group = parser.add_mutually_exclusive_group()
    flash_group.add_argument("--flash-before-test", action="store_true", help="run configured chip flash before half flow")
    flash_group.add_argument("--no-flash-before-test", action="store_true", help="disable chip flash before half flow")
    parser.add_argument("--flash-image", help="half-flow hex image for nrfjprog flashing")
    parser.add_argument("--flash-backend", choices=("nrfjprog", "script"), help="chip flash backend")
    parser.add_argument("--nrfjprog-path", help="nrfjprog executable path; defaults to PATH lookup")
    parser.add_argument("--jlink-probe-id", help="J-Link serial number for flashing")
    parser.add_argument("--flash-after-wait-s", type=float, help="seconds to wait after a successful flash")
    parser.add_argument("--flash-timeout-s", type=float, help="chip flash subprocess timeout seconds")
    parser.add_argument("--no-flash-verify", action="store_true", help="skip nrfjprog --verify")
    parser.add_argument("--verbose-frames", action="store_true", help="print every capture frame line")
    parser.add_argument(
        "--mes-station",
        choices=("half", "full"),
        default="half",
        help="MES station type for diagnostic commands",
    )
    parser.add_argument("--mes-payload", help="JSON payload file for mes-post")
    parser.add_argument(
        "--mes-send",
        action="store_true",
        help="actually send mes-post; without this flag the payload is previewed only",
    )
    parser.add_argument(
        "--mes-result",
        choices=("PASS", "FAIL"),
        default="PASS",
        help="result used by the generated mes-post sample payload",
    )
    parser.add_argument(
        "--mes-http-2xx-is-success",
        action="store_true",
        help="diagnostic override: treat HTTP 2xx as MES business success",
    )
    parser.add_argument(
        "--mes-success-field",
        help="diagnostic override: dot-separated JSON response field used as MES success flag",
    )
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
    if args.record_output_mode:
        config.record_output_mode = args.record_output_mode
    if args.flash_before_test:
        config.half_flash_before_test = True
    if args.no_flash_before_test:
        config.half_flash_before_test = False
    if args.flash_image:
        config.half_flash_image_path = args.flash_image
    if args.flash_backend:
        config.flash_backend = args.flash_backend
    if args.nrfjprog_path:
        config.nrfjprog_path = args.nrfjprog_path
    if args.jlink_probe_id:
        config.jlink_probe_id = args.jlink_probe_id
    if args.flash_after_wait_s is not None:
        config.flash_after_wait_s = args.flash_after_wait_s
    if args.flash_timeout_s is not None:
        config.flash_timeout_s = args.flash_timeout_s
    if args.no_flash_verify:
        config.flash_verify = False
    if args.mes_http_2xx_is_success:
        config.mes.http_2xx_is_success = True
    if args.mes_success_field is not None:
        config.mes.response_success_field = args.mes_success_field

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


def _run_flash_step(
    config: WorkstationConfig,
    record,
    progress: Callable[[int, str, str, str], None],
    flash_runner: FlashRunner,
) -> FlashOutcome:
    def flash_log(direction: str, line: str) -> None:
        print(f"[{direction}] {line}", flush=True)
    return record_flash_step(config, record, progress, flash_runner, step_index=1, line_callback=flash_log)


def _half_flash_config(config: WorkstationConfig) -> WorkstationConfig:
    return replace(config, flash_image_path=config.half_flash_image_path)


def _print_json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True), flush=True)


def _load_json_object(path: str | Path) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError(f"MES payload must be a JSON object: {path}")
    return data


def _run_mes_command(config: WorkstationConfig, args: argparse.Namespace) -> int:
    station_type = args.mes_station.upper()
    service = MesService(config.mes)
    if args.flow == "mes-checkroute":
        sn = args.sn.strip()
        if not sn:
            raise ValueError("mes-checkroute requires --sn")
        ok, reason = config.validate_sn(sn)
        if not ok:
            raise ValueError(reason)
        payload, result = service.checkroute(sn, station_type)
        _print_json({"operation": "checkroute", "request": payload, "result": result.to_dict()})
        return 0 if result.confirmed else 3

    if args.mes_payload:
        payload = _load_json_object(args.mes_payload)
    else:
        sn = args.sn.strip()
        if not sn:
            raise ValueError("mes-post without --mes-payload requires --sn")
        payload = build_sample_post_payload(
            config.mes,
            sn=sn,
            station_type=station_type,
            result=args.mes_result,
        )
    if not args.mes_send:
        _print_json({"operation": "postxtdata-preview", "request": payload})
        return 0

    service.validate(station_type)
    result = service.postxtdata(payload)
    output: dict[str, object] = {
        "operation": "postxtdata",
        "request": payload,
        "result": result.to_dict(),
    }
    if not result.confirmed:
        data = payload.get("Data", {})
        run_id = str(data.get("run_id", "")) if isinstance(data, dict) else ""
        if not run_id:
            run_id = f"MES_CLI_{int(time.time())}_{payload.get('SN', 'UNKNOWN')}"
        pending_path = write_pending_request(
            config.records_root,
            run_id=run_id,
            url=config.mes.postxtdata_url,
            payload=payload,
            operation="postxtdata",
            last_error=result.message,
        )
        output["pending_path"] = str(pending_path)
    _print_json(output)
    return 0 if result.confirmed else 3


def run(
    argv: list[str] | None = None,
    transport_factory: TransportFactory | None = None,
    flash_runner: FlashRunner | None = None,
) -> int:
    args = _parse_args(argv)
    config = load_config(Path(args.config))
    mode, sn_enabled = _apply_overrides(config, args)
    if args.flow.startswith("mes-"):
        try:
            return _run_mes_command(config, args)
        except Exception as exc:
            print(f"[ERR] {exc}", flush=True)
            return 1
    sn = args.sn.strip() if sn_enabled else ""
    token = get_factory_token(args.token)
    factory = transport_factory or _default_transport_factory
    flash_runner_impl = flash_runner or run_flash

    print(
        f"[INFO] flow={args.flow} transport={mode} sn_record={'on' if sn_enabled else 'off'} "
        f"capture={config.capture_output_mode} records={config.record_output_mode}",
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

    if sn_enabled and args.flow == "full":
        half_sn_check = verify_half_sn_pass_record(config.records_root, sn)
        if not half_sn_check.ok:
            print(f"[RESULT] NG half SN record check failed: {half_sn_check.message}", flush=True)
            return 2
        print(f"[INFO] half SN record check PASS: {half_sn_check.record_path}", flush=True)

    record = None
    client: ATClient | None = None
    flash_before_half = args.flow == "half" and config.half_flash_before_test
    flow_start_index = 1
    try:
        if sn_enabled:
            station = "HALF" if args.flow == "half" else "FULL"
            record = RunStorage(
                config.records_root,
                write_extra_files=config.write_extra_record_files(),
            ).start_run(station, sn, config.dut_alias)
        else:
            record = NullRunRecord()

        if flash_before_half:
            flash_config = _half_flash_config(config)
            # GUI/CLI no longer auto-run the slow J-Link precheck before flash.
            # Operators can use the GUI "烧录检测" button when they want it.
            outcome = _run_flash_step(flash_config, record, _progress, flash_runner_impl)
            if not outcome.ok:
                record.finish("NG", f"flash failed: {outcome.message}")
                print(f"[RESULT] NG flash failed: {outcome.message}", flush=True)
                return 2
            wait_s = max(0.0, float(config.flash_after_wait_s))
            if wait_s > 0:
                print(f"[INFO] waiting {wait_s:.1f}s after flash", flush=True)
                time.sleep(wait_s)
            flow_start_index = 3
        elif args.flow != "half" and args.flash_before_test:
            print("[WARN] --flash-before-test is only applied to half flow; ignoring for full flow", flush=True)

        client = factory(config, args, _line_callback)

        def record_line(direction: str, line: str) -> None:
            record.log_at(direction, line)
            _line_callback(direction, line, args.verbose_frames)

        client.set_line_callback(record_line)
        if flash_before_half:
            record.start_step(2, "Flash reconnect", "AT;AT+VER?")
            _progress(2, "Flash reconnect", "RUN", mode)
            ok, elapsed_ms, detail, reason, _probe_results = probe_at_client(client)
            record.log_item("half", "Flash reconnect", "AT;AT+VER?", "PASS" if ok else "NG", elapsed_ms, reason, detail)
            _progress(2, "Flash reconnect", "PASS" if ok else "NG", detail or f"{elapsed_ms / 1000:.1f}s")
            if not ok:
                record.finish("NG", "flash reconnect failed")
                print("[RESULT] NG flash reconnect failed", flush=True)
                return 2
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
                start_index=flow_start_index,
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
                start_index=flow_start_index,
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
